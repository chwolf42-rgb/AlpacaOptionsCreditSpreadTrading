"""Replay credit model, filters, bootstrap, and a short engine smoke test."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from alpaca_options_credit.models import Bar, Side
from alpaca_options_credit.replay.credit import (
    ReplayLimits,
    bs_delta,
    bs_price,
    friday_expiration,
    half_spread,
    leg_bid_ask,
    modeled_proposal,
    simulate_exit,
    spread_mid,
    strike_increment,
)
from alpaca_options_credit.replay.marks import (
    census_stops,
    classify_stop,
    quote_path_verdict,
)
from alpaca_options_credit.replay.engine import StructureParams, bar_end, replay_symbol
from alpaca_options_credit.replay.filters import (
    bar_overlaps_skip_window,
    ema_last,
    relative_strength_allows,
    trend_allows,
    two_hvn_allows,
    volume_allows,
)
from alpaca_options_credit.replay.stats import (
    ReplayTrade,
    bootstrap_diff,
    bootstrap_mean,
    select_risk_book,
)
from alpaca_options_credit.replay.study import adoption_reason
from alpaca_options_credit.rth import ET


UTC = timezone.utc


def _bar(ts: datetime, price: float, *, high=None, low=None, volume=1_000_000.0) -> Bar:
    return Bar(
        ts=ts,
        open=price,
        high=price if high is None else high,
        low=price if low is None else low,
        close=price,
        volume=volume,
    )


def test_bs_call_and_put_call_parity():
    call = bs_price(100, 100, 1.0, 0.20, "call")
    put = bs_price(100, 100, 1.0, 0.20, "put")
    # Call ≈ 9.92 at r=0.04, q=0, σ=0.20.
    assert abs(call - 9.925) < 0.02
    forward_gap = 100 - 100 * math.exp(-0.04)
    assert abs((call - put) - forward_gap) < 1e-8


def test_natural_credit_is_inside_the_mid_and_the_width_gate():
    limits = ReplayLimits()
    when = datetime(2026, 6, 15, 18, 0, tzinfo=UTC)  # 14:00 ET
    from alpaca_options_credit.replay.credit import year_fraction

    priced = modeled_proposal(
        symbol="SPY",
        side=Side.BULLISH,
        invalidation=103.0,
        spot=105.0,
        when=when,
        iv=0.25,
        limits=limits,
    )
    assert priced is not None
    proposal = priced.proposal
    assert proposal.skip is False, proposal.skip_reason
    assert proposal.credit >= 0.20 * limits.width - 1e-9
    mid = spread_mid(
        105.0,
        proposal.short.strike,
        proposal.long.strike,
        year_fraction(when, priced.expiration),
        0.25,
        "put",
    )
    assert proposal.credit < mid


def test_half_spread_is_bounded():
    assert half_spread(0.10) == 0.05
    assert abs(half_spread(2.0) - 0.12) < 1e-12
    assert half_spread(10.0) == 0.25
    bid, ask = leg_bid_ask(0.02)
    assert bid == 0.0
    assert ask > 0


def test_strike_grid_and_friday():
    assert strike_increment("SPY", 700) == 1.0
    assert strike_increment("NVDA", 250) == 2.5
    assert strike_increment("AAPL", 80) == 1.0
    expiry = friday_expiration(date(2026, 1, 5))
    assert expiry is not None and expiry.weekday() == 4
    assert 30 <= (expiry - date(2026, 1, 5)).days <= 45


def test_stop_is_preferred_when_the_bar_touches_both():
    limits = ReplayLimits()
    start = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    bars = [
        _bar(start, 110),
        _bar(start + timedelta(hours=1), 100, high=140, low=70),
    ]

    def _end(bar: Bar) -> datetime:
        return bar.ts + timedelta(hours=1)

    fill = simulate_exit(
        side=Side.BULLISH,
        short_k=100,
        long_k=95,
        credit=1.20,
        iv=0.30,
        expiration=date(2026, 4, 17),
        invalidation=101,
        bars=bars,
        start_index=0,
        bar_end_fn=_end,
        daily_close_at=lambda _when: None,
        limits=limits,
    )
    assert fill.reason == "stop_credit"
    assert fill.debit + 1e-9 >= 1.5 * 1.20


def test_take_profit_fills_at_half_credit_not_the_wick():
    limits = ReplayLimits()
    start = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
    # Rally hard enough that the put spread's mid is under half the credit.
    bars = [_bar(start, 110)]
    bars.append(_bar(start + timedelta(days=1), 160))
    fill = simulate_exit(
        side=Side.BULLISH,
        short_k=100,
        long_k=95,
        credit=1.00,
        iv=0.25,
        expiration=date(2026, 4, 17),
        invalidation=101,
        bars=bars,
        start_index=0,
        bar_end_fn=lambda bar: bar.ts + timedelta(hours=1),
        daily_close_at=lambda _when: None,
        limits=limits,
    )
    assert fill.reason == "take_profit"
    assert abs(fill.debit - 0.50) < 1e-9
    quarter = simulate_exit(
        side=Side.BULLISH,
        short_k=100,
        long_k=95,
        credit=1.00,
        iv=0.25,
        expiration=date(2026, 4, 17),
        invalidation=101,
        bars=bars,
        start_index=0,
        bar_end_fn=lambda bar: bar.ts + timedelta(hours=1),
        daily_close_at=lambda _when: None,
        limits=ReplayLimits(tp_frac=0.25),
    )
    assert quarter.reason == "take_profit"
    assert abs(quarter.debit - 0.75) < 1e-9


def test_gap_stop_uses_the_open():
    limits = ReplayLimits()
    start = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
    bars = [
        _bar(start, 110),
        Bar(
            ts=start + timedelta(days=1),
            open=70,
            high=72,
            low=68,
            close=71,
            volume=1.0,
        ),
    ]
    fill = simulate_exit(
        side=Side.BULLISH,
        short_k=100,
        long_k=95,
        credit=1.00,
        iv=0.30,
        expiration=date(2026, 4, 17),
        invalidation=102,
        bars=bars,
        start_index=0,
        bar_end_fn=lambda bar: bar.ts + timedelta(hours=1),
        daily_close_at=lambda _when: None,
        limits=limits,
    )
    assert fill.reason == "stop_credit"
    assert fill.debit > 1.5 * 1.00


def test_close_stop_ignores_a_wick_that_recovers():
    start = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
    bars = [
        _bar(start, 110),
        _bar(start + timedelta(hours=1), 110, high=112, low=70),
    ]
    common = dict(
        side=Side.BULLISH,
        short_k=100,
        long_k=95,
        credit=1.20,
        iv=0.30,
        expiration=date(2026, 4, 17),
        invalidation=101,
        bars=bars,
        start_index=0,
        bar_end_fn=lambda bar: bar.ts + timedelta(hours=1),
        daily_close_at=lambda _when: None,
    )
    intrabar = simulate_exit(**common, limits=ReplayLimits(stop_check="intrabar"))
    on_close = simulate_exit(**common, limits=ReplayLimits(stop_check="close"))
    assert intrabar.reason == "stop_credit"
    assert on_close.reason != "stop_credit"
    assert classify_stop(
        ReplayTrade(
            symbol="X",
            side="bullish",
            variant="baseline",
            entry_time=start,
            exit_time=start,
            exit_reason="stop_credit",
            credit=1.20,
            debit=intrabar.debit,
            width=5,
            qty=1,
            max_loss=100,
            pnl=-50,
            short_strike=100,
            iv=0.3,
            open_mid=intrabar.open_mid,
            adverse_mid=intrabar.adverse_mid,
            close_mid=intrabar.close_mid,
            close_natural=intrabar.close_natural,
        )
    ) == "wick"


def test_structure_only_does_not_take_the_price_stop():
    start = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
    bars = [
        _bar(start, 110),
        _bar(start + timedelta(hours=1), 110, high=112, low=70),
    ]
    fill = simulate_exit(
        side=Side.BULLISH,
        short_k=100,
        long_k=95,
        credit=1.20,
        iv=0.30,
        expiration=date(2026, 4, 17),
        invalidation=101,
        bars=bars,
        start_index=0,
        bar_end_fn=lambda bar: bar.ts + timedelta(hours=1),
        daily_close_at=lambda _when: None,
        limits=ReplayLimits(stop_check="none"),
    )
    assert fill.reason != "stop_credit"


def test_delta_short_is_further_out_than_the_nearest_strike():
    when = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
    nearest = modeled_proposal(
        symbol="SPY",
        side=Side.BULLISH,
        invalidation=100,
        spot=101,
        when=when,
        iv=0.25,
        limits=ReplayLimits(),
    )
    further = modeled_proposal(
        symbol="SPY",
        side=Side.BULLISH,
        invalidation=100,
        spot=101,
        when=when,
        iv=0.25,
        limits=ReplayLimits(target_abs_delta=0.20),
    )
    assert nearest is not None and further is not None
    assert not nearest.proposal.skip
    assert further.proposal.short.strike < nearest.proposal.short.strike
    if not further.proposal.skip:
        assert abs(further.short_delta) < abs(nearest.short_delta)
    call = bs_delta(100, 100, 30 / 365, 0.20, "call")
    put = bs_delta(100, 100, 30 / 365, 0.20, "put")
    assert 0.45 < call < 0.65
    assert -0.55 < put < -0.35


def test_quote_path_and_stop_census():
    assert quote_path_verdict(1.0, [1.2, 1.6], [1.4, 1.8]) == "confirmed"
    assert quote_path_verdict(1.0, [1.2, 1.4], [1.4, 1.7]) == "natural_only"
    assert quote_path_verdict(1.0, [1.0], [1.2]) == "absent"
    trade = ReplayTrade(
        symbol="X",
        side="bullish",
        variant="baseline",
        entry_time=datetime(2026, 7, 6, tzinfo=UTC),
        exit_time=datetime(2026, 7, 7, tzinfo=UTC),
        exit_reason="stop_credit",
        credit=1.0,
        debit=1.6,
        width=5,
        qty=1,
        max_loss=400,
        pnl=-60,
        short_strike=100,
        iv=0.2,
        open_mid=1.1,
        adverse_mid=1.8,
        close_mid=1.2,
        close_natural=1.4,
    )
    assert census_stops([trade]).wick_only == 1
    assert census_stops([trade]).close_confirmed == 0


def test_session_windows():
    def span(hour, minute, hours=1):
        start = datetime(2026, 7, 6, hour, minute, tzinfo=ET)
        return start, start + timedelta(hours=hours)

    assert bar_overlaps_skip_window(*span(9, 30))
    assert not bar_overlaps_skip_window(*span(10, 30))
    assert bar_overlaps_skip_window(*span(11, 30))
    assert bar_overlaps_skip_window(*span(12, 30))
    assert not bar_overlaps_skip_window(*span(13, 30))
    # A 15-minute bar inside the first half hour.
    start = datetime(2026, 7, 6, 9, 45, tzinfo=ET)
    assert bar_overlaps_skip_window(start, start + timedelta(minutes=15))


def test_relative_strength_and_trend_and_volume():
    assert relative_strength_allows(Side.BULLISH, 0.04, 0.01)
    assert not relative_strength_allows(Side.BULLISH, 0.01, 0.04)
    assert relative_strength_allows(Side.BEARISH, -0.03, 0.01)
    assert not relative_strength_allows(Side.BEARISH, 0.02, 0.01)
    assert not relative_strength_allows(Side.BULLISH, 0.02, 0.02)
    assert trend_allows(Side.BULLISH, 110, 100)
    assert not trend_allows(Side.BEARISH, 110, 100)
    assert trend_allows(Side.BEARISH, 90, 100)
    assert volume_allows(1_500, 1_000)
    assert not volume_allows(1_000, 1_000)
    ema = ema_last(list(range(1, 60)), 50)
    assert ema is not None and 25 < ema < 59


def test_two_shelves_near_the_short():
    bars = []
    start = datetime(2026, 1, 2, tzinfo=UTC)
    for i in range(16):
        price = 100.0 if i % 2 == 0 else 102.0
        bars.append(_bar(start + timedelta(days=i), price, volume=5_000_000))
    assert two_hvn_allows(bars, short_strike=101.0, atr=1.5)
    one = [_bar(start + timedelta(days=i), 100.0, volume=5_000_000) for i in range(15)]
    assert not two_hvn_allows(one, short_strike=101.0, atr=1.5)


def test_bootstrap_constant_and_difference():
    point = bootstrap_mean([2.0, 2.0, 2.0, 2.0])
    assert point is not None
    assert point.low == point.high == 2.0
    diff = bootstrap_diff([3.0, 3.0, 3.0, 3.0], [1.0, 1.0, 1.0, 1.0])
    assert diff is not None and diff.low > 0 and abs(diff.point - 2.0) < 1e-9


def test_risk_book_keeps_one_slot_and_realizes_cash():
    t0 = datetime(2026, 7, 6, 16, 0, tzinfo=UTC)
    def trade(symbol, day, pnl_credit=1.0):
        entry = t0 + timedelta(days=day)
        return ReplayTrade(
            symbol=symbol,
            side="bullish",
            variant="baseline",
            entry_time=entry,
            exit_time=entry + timedelta(days=5),
            exit_reason="take_profit",
            credit=pnl_credit,
            debit=0.5,
            width=5.0,
            qty=1,
            max_loss=400.0,
            pnl=(pnl_credit - 0.5) * 100,
            short_strike=100.0,
            iv=0.2,
        )

    # Two names enter together. Cap 1 keeps the first symbol only.
    kept = select_risk_book(
        [trade("AAA", 0), trade("BBB", 0)],
        max_concurrent=1,
    )
    assert [t.symbol for t in kept] == ["AAA"]
    # After AAA exits, BBB-on-a-later-day is allowed.
    later = select_risk_book(
        [trade("AAA", 0), trade("BBB", 6)],
        max_concurrent=1,
    )
    assert [t.symbol for t in later] == ["AAA", "BBB"]


def test_hourly_bar_ending_at_the_cash_close():
    bar = Bar(
        ts=datetime(2026, 7, 6, 19, 30, tzinfo=UTC),  # 15:30 ET
        open=1,
        high=1,
        low=1,
        close=1,
        volume=1,
    )
    end = bar_end(bar, 60)
    assert end.astimezone(ET).hour == 16
    assert end.astimezone(ET).minute == 0


def test_replay_symbol_on_a_short_tape_does_not_crash():
    daily = []
    timing = []
    start = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    for i in range(40):
        price = 100 + i * 0.4
        daily.append(_bar(start + timedelta(days=i), price, volume=2_000_000))
        for hour in range(7):
            timing.append(
                _bar(start + timedelta(days=i, hours=hour), price, volume=100_000 + hour)
            )
    trades, diags = replay_symbol(
        "TEST",
        daily,
        timing,
        daily,
        {"baseline": lambda f: True},
        minutes=60,
        limits=ReplayLimits(),
        structure=StructureParams(timing_lookback=40, daily_lookback=30),
    )
    assert isinstance(trades, list)
    assert diags["baseline"].opened == len(trades)


def test_adoption_rejects_a_noisier_or_thinner_filter():
    def book(label, n, win, exp, r):
        from alpaca_options_credit.replay.stats import BookStats, Interval

        return BookStats(
            label=label,
            n=n,
            wins=int(n * win),
            losses=n - int(n * win),
            open_mtm=0,
            trades_per_week=1,
            weeks=10,
            win_rate=Interval(win, win - 0.05, win + 0.05),
            expectancy=Interval(exp, exp - 1, exp + 1),
            expectancy_r=Interval(r, r - 0.01, r + 0.01),
            avg_win=100,
            avg_loss=-80,
            exit_counts={},
        )

    base = book("baseline", 80, 0.48, 20, 0.04)
    thin = book("skip_open_and_midday", 10, 0.7, 40, 0.1)
    ok, reason = adoption_reason("skip_open_and_midday", base, thin, None, base, thin, None)
    assert ok is False and "30" in reason

    from alpaca_options_credit.replay.stats import Interval

    worse = book("rs_vs_spy", 80, 0.45, 10, 0.01)
    diff = bootstrap_diff([0.01] * 40, [0.04] * 40)
    # Point-estimate win rate can tick up while the interval still covers zero.
    noisy = book("confirm_volume", 80, 0.50, 21, 0.041)
    inside = Interval(0.001, -0.02, 0.02)
    ok, reason = adoption_reason(
        "confirm_volume", base, noisy, inside, base, noisy, inside, wr_diff=inside
    )
    assert ok is False
    assert "covers zero" in reason

    ok, reason = adoption_reason("rs_vs_spy", base, worse, diff, base, worse, diff)
    assert ok is False

    cleared = Interval(0.05, 0.01, 0.09)
    better = book("htf_4h_ema50", 80, 0.62, 30, 0.09)
    recent_base = book("baseline", 40, 0.48, 20, 0.04)
    recent_better = book("htf_4h_ema50", 40, 0.60, 28, 0.08)
    ok, reason = adoption_reason(
        "htf_4h_ema50",
        base,
        better,
        cleared,
        recent_base,
        recent_better,
        cleared,
        wr_diff=cleared,
    )
    assert ok is True and "above zero" in reason
