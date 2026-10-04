"""Protocol, credit-gate surface, fees, and the train-only selector."""

from __future__ import annotations

from datetime import date, datetime, timezone

from alpaca_options_credit.replay.stats import ReplayTrade
from alpaca_options_credit.research_3pct.accounting import (
    bootstrap_mean,
    build_book,
    month_pnl,
    report_book,
)
from alpaca_options_credit.research_3pct.assignment import assignment_debit
from alpaca_options_credit.research_3pct.candidates import candidates
from alpaca_options_credit.research_3pct.chains import parse_option_symbol
from alpaca_options_credit.research_3pct.protocol import (
    BOOT_SEED,
    TEST_START,
    TRAIN_END,
    Interval,
    TrainRow,
    monthly_ceiling,
    months_between,
    regulatory_fee,
    select_name,
    win_r_multiple,
)
from alpaca_options_credit.research_3pct.surface import quote_vertical


UTC = timezone.utc


def _trade(pnl: float, entry: datetime, exit_at: datetime, symbol: str = "SPY") -> ReplayTrade:
    return ReplayTrade(
        symbol=symbol,
        side="bullish",
        variant="base",
        entry_time=entry,
        exit_time=exit_at,
        exit_reason="take_profit",
        credit=1.0,
        debit=0.5,
        width=5.0,
        qty=1,
        max_loss=400.0,
        pnl=pnl,
        short_strike=100.0,
        iv=0.2,
        long_strike=95.0,
    )


def test_split_is_contiguous_and_test_is_after_train():
    assert TRAIN_END < TEST_START
    assert (TEST_START - TRAIN_END).days == 1
    months = months_between(TRAIN_END, TEST_START)
    assert months == [(2025, 6), (2025, 7)]


def test_selector_cannot_see_a_test_window():
    rich = Interval(0.03, 0.01, 0.05)
    poor = Interval(-0.01, -0.02, -0.001)
    rows = [
        TrainRow("base", True, 80, poor, poor),
        TrainRow("d16_c10", False, 200, rich, rich),
        TrainRow("d30_c20", True, 40, rich, rich),
    ]
    assert select_name(rows) == "d30_c20"
    assert select_name([TrainRow("base", True, 80, poor, poor)]) is None
    assert select_name([TrainRow("d30_c20", True, 10, rich, rich)]) is None


def test_twenty_percent_credit_cannot_make_three_percent_at_locked_size():
    assert abs(win_r_multiple(0.20) - 0.125) < 1e-12
    # 20 perfect wins at a 20% credit is 1.25% of the account, not 3%.
    assert monthly_ceiling(0.20, 20) == 0.0125
    # 48 perfect wins is the identity that lands on 3%.
    assert abs(monthly_ceiling(0.20, 48) - 0.03) < 1e-12
    # Eight wins at a 20% credit add 0.5% of the account, which is one full loss.
    assert abs(monthly_ceiling(0.20, 8) - 0.005) < 1e-12
    assert monthly_ceiling(0.20, 7) < 0.005


def test_fee_is_a_few_dimes_and_rounds_up():
    fee = regulatory_fee(1, credit=1.12, debit=0.56)
    assert 0.10 < fee < 0.50
    assert abs(fee * 100 - round(fee * 100)) < 1e-9
    assert regulatory_fee(0, 1.0, 0.5) == 0.0


def test_low_delta_five_wide_misses_the_gate_and_a_rich_delta_can_clear():
    far = quote_vertical(spot=500, iv=0.18, dte=30, width=5, target_delta=0.16)
    assert far is not None
    assert abs(far.short_delta - 0.16) < 0.01
    assert far.natural < far.mid
    assert not far.clears_20
    near = quote_vertical(spot=100, iv=0.80, dte=30, width=5, target_delta=0.40)
    assert near is not None
    assert near.clears_20


def test_quiet_months_stay_in_the_monthly_series():
    entry = datetime(2025, 7, 2, 20, 0, tzinfo=UTC)
    exit_at = datetime(2025, 7, 10, 20, 0, tzinfo=UTC)
    trades = [_trade(50.0, entry, exit_at)]
    start = date(2025, 7, 1)
    end = date(2025, 9, 30)
    pnls = month_pnl(trades, start, end)
    assert pnls == [50.0, 0.0, 0.0]
    book = report_book(trades, "base", start, end)
    assert book.months == 3
    assert book.trades_per_month == 1 / 3
    assert book.monthly is not None
    assert abs(book.monthly.point - (50.0 / 100_000) / 3) < 1e-12


def test_empty_book_is_not_a_zero_return_claim():
    book = report_book([], "base", date(2025, 7, 1), date(2025, 9, 30))
    assert book.n == 0
    assert book.monthly is None


def test_five_spread_cap_and_fees():
    start = datetime(2025, 7, 2, 18, 0, tzinfo=UTC)
    end = datetime(2025, 7, 20, 20, 0, tzinfo=UTC)
    trades = [
        _trade(80.0, start, end, symbol=symbol)
        for symbol in ("SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA")
    ]
    book = build_book(trades)
    assert len(book) == 5
    assert all(trade.pnl < 80.0 for trade in book)


def test_bootstrap_seed_is_stable():
    first = bootstrap_mean([1.0, 2.0, 3.0, 4.0], n_boot=200, seed=BOOT_SEED)
    second = bootstrap_mean([1.0, 2.0, 3.0, 4.0], n_boot=200, seed=BOOT_SEED)
    assert first == second
    assert first.low <= first.point <= first.high


def test_assignment_fires_only_when_the_short_is_deep_in_the_money():
    deep = assignment_debit(
        spot=100,
        short_k=130,
        long_k=125,
        t_years=1 / 365,
        iv=0.15,
        right="put",
    )
    assert deep is not None
    assert abs(deep - 5.0) < 1e-9
    atm = assignment_debit(
        spot=100,
        short_k=100,
        long_k=95,
        t_years=30 / 365,
        iv=0.20,
        right="put",
    )
    assert atm is None


def test_option_symbol_parser_and_candidate_split():
    parsed = parse_option_symbol("SPY261030P00740000")
    assert parsed is not None
    expiration, right, strike = parsed
    assert expiration.isoformat() == "2026-10-30"
    assert right == "put"
    assert strike == 740
    rows = candidates()
    names = [row.name for row in rows]
    assert names.count("base") == 1
    assert "d16_c20" in names and "d16_c10" in names
    by_name = {row.name: row for row in rows}
    assert by_name["d16_c20"].searchable
    assert not by_name["d16_c10"].searchable
    assert by_name["base"].limits.tp_frac == 0.50
    assert by_name["base"].limits.min_credit_pct == 0.20
    assert by_name["base_tp25"].limits.tp_frac == 0.25
    assert not by_name["base_tp25"].searchable
