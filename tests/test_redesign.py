"""Redesign pricing rules: delta tolerance, 21 DTE, shelf/short stops, extension."""

from datetime import date, datetime, timedelta, timezone

from alpaca_options_credit.models import Bar, Side
from alpaca_options_credit.replay.credit import (
    ReplayLimits,
    calendar_dte,
    earnings_near,
    modeled_proposal,
    resolved_width,
    simulate_exit,
)
from alpaca_options_credit.replay.redesign import passes, select_name
from alpaca_options_credit.replay.regime import extension_touch, range_anchors
from alpaca_options_credit.replay.stats import BookStats, Interval

UTC = timezone.utc


def _bar(ts, price, *, high=None, low=None):
    return Bar(
        ts=ts,
        open=price,
        high=price if high is None else high,
        low=price if low is None else low,
        close=price,
        volume=1_000_000,
    )


def _book(label, n, exp, r, exp_low, r_low):
    return BookStats(
        label=label,
        n=n,
        wins=n // 2,
        losses=n // 2,
        open_mtm=0,
        trades_per_week=1,
        weeks=10,
        win_rate=Interval(0.5, 0.4, 0.6),
        expectancy=Interval(exp, exp_low, exp + 1),
        expectancy_r=Interval(r, r_low, r + 0.01),
        avg_win=100,
        avg_loss=-80,
        exit_counts={},
    )


def test_width_pct_snaps_to_the_listed_step():
    spy = resolved_width("SPY", 500, ReplayLimits(width_pct=0.02))
    assert spy == 10
    # 2% of a $40 stock is $0.80, two $0.50 steps.
    assert resolved_width("AAPL", 40, ReplayLimits(width_pct=0.02)) == 1.0


def test_delta_tolerance_skips_a_target_the_grid_cannot_reach():
    when = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
    # 1-delta is further than the 20% scan on this spot, so a tight tolerance skips.
    missed = modeled_proposal(
        symbol="SPY",
        side=Side.BULLISH,
        invalidation=100,
        spot=101,
        when=when,
        iv=0.25,
        limits=ReplayLimits(target_abs_delta=0.01, delta_tol=0.005, min_credit_pct=0.01),
    )
    assert missed is not None and missed.proposal.skip


def test_close_at_21_dte_exits_before_expiration():
    start = datetime(2026, 3, 2, 19, 30, tzinfo=UTC)
    expiration = date(2026, 4, 17)
    bars = [_bar(start + timedelta(days=i), 100) for i in range(40)]
    fill = simulate_exit(
        side=Side.BULLISH,
        short_k=100,
        long_k=95,
        credit=1.0,
        iv=0.30,
        expiration=expiration,
        invalidation=90,
        bars=bars,
        start_index=0,
        bar_end_fn=lambda bar: bar.ts,
        daily_close_at=lambda _when: None,
        limits=ReplayLimits(stop_check="none", spot_stop="none", close_dte=21, tp_frac=0.99),
    )
    assert fill.reason == "dte_exit"
    assert calendar_dte(fill.when, expiration) <= 21


def test_daily_close_through_the_short_is_an_underlying_stop():
    start = datetime(2026, 3, 2, 20, 0, tzinfo=UTC)
    bars = [
        _bar(start, 110),
        _bar(start + timedelta(days=1), 110),
    ]
    fill = simulate_exit(
        side=Side.BULLISH,
        short_k=100,
        long_k=95,
        credit=1.2,
        iv=0.25,
        expiration=date(2026, 4, 17),
        invalidation=80,
        bars=bars,
        start_index=0,
        bar_end_fn=lambda bar: bar.ts,
        daily_close_at=lambda _when: 99.0,
        limits=ReplayLimits(stop_check="none", spot_stop="short", tp_frac=0.99),
    )
    assert fill.reason == "underlying_stop"


def test_shelf_stop_uses_the_shelf_not_the_short():
    start = datetime(2026, 3, 2, 20, 0, tzinfo=UTC)
    bars = [_bar(start, 110), _bar(start + timedelta(days=1), 110)]
    held = simulate_exit(
        side=Side.BULLISH,
        short_k=90,
        long_k=85,
        credit=0.4,
        iv=0.25,
        expiration=date(2026, 4, 17),
        invalidation=70,
        bars=bars,
        start_index=0,
        bar_end_fn=lambda bar: bar.ts,
        daily_close_at=lambda _when: 100.0,
        limits=ReplayLimits(stop_check="none", spot_stop="shelf", tp_frac=0.99),
        shelf=95.0,
    )
    # Close 100 is still above the shelf at 95, so the shelf stop does not fire.
    assert held.reason != "underlying_stop"
    broken = simulate_exit(
        side=Side.BULLISH,
        short_k=90,
        long_k=85,
        credit=0.4,
        iv=0.25,
        expiration=date(2026, 4, 17),
        invalidation=70,
        bars=bars,
        start_index=0,
        bar_end_fn=lambda bar: bar.ts,
        daily_close_at=lambda _when: 94.0,
        limits=ReplayLimits(stop_check="none", spot_stop="shelf", tp_frac=0.99),
        shelf=95.0,
    )
    assert broken.reason == "underlying_stop"


def test_legacy_invalidation_stays_a_structure_break():
    start = datetime(2026, 3, 2, 20, 0, tzinfo=UTC)
    bars = [_bar(start, 110), _bar(start + timedelta(days=1), 110)]
    fill = simulate_exit(
        side=Side.BULLISH,
        short_k=100,
        long_k=95,
        credit=1.0,
        iv=0.20,
        expiration=date(2026, 4, 17),
        invalidation=101,
        bars=bars,
        start_index=0,
        bar_end_fn=lambda bar: bar.ts,
        daily_close_at=lambda _when: 100.0,
        limits=ReplayLimits(stop_check="none", tp_frac=0.99),
    )
    assert fill.reason == "structure_break"


def test_extension_requires_a_one_atr_push_that_holds_the_shelf():
    shelves = [(100.0, 101.0)]
    bear = extension_touch(
        prior_close=98,
        close_5=90,
        atr_value=4,
        bar_high=100.5,
        bar_low=97.5,
        bar_close=100.2,
        shelves=shelves,
    )
    assert bear is not None and bear.side is Side.BEARISH and bear.anchor == 101
    # Close through the shelf is a breakout, not a touch.
    assert (
        extension_touch(
            prior_close=98,
            close_5=90,
            atr_value=4,
            bar_high=103,
            bar_low=99,
            bar_close=102,
            shelves=shelves,
        )
        is None
    )
    bull = extension_touch(
        prior_close=104,
        close_5=112,
        atr_value=4,
        bar_high=104.2,
        bar_low=100.2,
        bar_close=100.8,
        shelves=shelves,
    )
    assert bull is not None and bull.side is Side.BULLISH and bull.anchor == 100
    # A small drift does not count as the extension.
    assert (
        extension_touch(
            prior_close=98,
            close_5=97,
            atr_value=4,
            bar_high=100.2,
            bar_low=97.5,
            bar_close=100.0,
            shelves=shelves,
        )
        is None
    )


def test_range_anchors_require_the_middle_of_the_box():
    start = datetime(2026, 1, 2, tzinfo=UTC)
    prior = [_bar(start + timedelta(days=i), 100, high=110, low=90) for i in range(20)]
    assert range_anchors(prior, 100, 100, 5) == (90, 110)
    assert range_anchors(prior, 108, 100, 5) is None  # top of the box
    assert range_anchors(prior, 100, 90, 2) is None  # ten points from the EMA, ATR is 2


def test_earnings_blackout_is_a_calendar_window():
    events = [date(2026, 4, 10)]
    assert earnings_near(events, date(2026, 4, 12), 3)
    assert not earnings_near(events, date(2026, 4, 14), 3)
    assert not earnings_near(events, date(2026, 4, 12), 0)


def test_selection_requires_the_interval_above_zero():
    good = _book("good", 80, 20, 0.05, 5, 0.01)
    noise = _book("noise", 80, 20, 0.05, -1, -0.01)
    thin = _book("thin", 10, 40, 0.10, 10, 0.02)
    assert passes(good, 50)
    assert not passes(noise, 50)
    assert not passes(thin, 50)
    assert select_name([("noise", noise), ("good", good), ("thin", thin)]) == "good"
    assert select_name([("noise", noise), ("thin", thin)]) is None
    # The higher expectancy-per-risk wins when both clear the bar.
    better = _book("better", 80, 30, 0.08, 10, 0.02)
    assert select_name([("good", good), ("better", better)]) == "better"
