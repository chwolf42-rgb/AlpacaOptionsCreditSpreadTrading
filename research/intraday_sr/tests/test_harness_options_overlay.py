"""Scaffold tests for the dual-expiry overlay (SPEC v1.3.2 O1). Synthetic bars only; no grid run."""

import itertools
import math
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from research.intraday_sr.harness import options as O
from research.intraday_sr.harness import options_calendar as cal
from research.intraday_sr.harness import options_overlay as ov
from research.intraday_sr.harness import options_pricing as px
from research.intraday_sr.harness import s0grids

ET = ZoneInfo("America/New_York")
# Pinned while O1 rows stay outside the hashed document. Developer 2's grids bump owns the next hash.
_HASH_BEFORE_O1 = "5c1d3dd3bb50df61056bf0737601749a3117727c8278a6d49f48c6603d9b355a"
_HASH_KEYS = {"fixed", "formations", "options", "options_0dte", "test_a", "test_b", "primary_guardrail",
              "comparison_guardrails", "spec_version"}


def nyse():
    return cal.study_calendar()


def _at(day, hh, mm):
    return datetime.combine(day, time(hh, mm), tzinfo=ET)


def _day(day, n_bars, price=450.0, whip=None):
    """RTH bars of 5 minutes starting 09:30. ``whip`` is (index, low, high) on one bar."""
    rows = []
    for i in range(n_bars):
        ts = _at(day, 9, 30) + timedelta(minutes=5 * i)
        low = high = price
        if whip is not None and i == whip[0]:
            low, high = whip[1], whip[2]
        rows.append(dict(ts=ts, open=price, high=max(high, price), low=min(low, price), close=price, volume=1e5))
    return pd.DataFrame(rows)


class _Bars:
    def __init__(self, frames):
        self.frames = frames

    def session_frame(self, symbol, day):
        return self.frames[(symbol, day)]


def _trade(symbol, day, entry, exit_clock, price=450.0, side=1, reason="forced_eod", target=None):
    entry_ts = entry if isinstance(entry, datetime) else _at(day, *entry)
    exit_ts = exit_clock if isinstance(exit_clock, datetime) else _at(day, *exit_clock)
    if target is None:
        target = price + side * 5
    signal = SimpleNamespace(stop=price - side, targets={"zone": target})
    entry_fill = SimpleNamespace(symbol=symbol, ts=entry_ts, price=price, side=side, reason="entry")
    exit_fill = SimpleNamespace(symbol=symbol, ts=exit_ts, price=price, side=-side, reason=reason)
    return SimpleNamespace(entry=entry_fill, exit=exit_fill, signal=signal)


def _ctx(days, rv=None):
    ny = nyse()
    vix9 = {d: 15.0 for d in days}
    vix = {d: 16.0 for d in days}
    return O.IVContext(calendar=ny.trading, vix9d_prev=vix9, vix_prev=vix, rv20=rv or {},
                       early_closes=ny.early_closes)


def _a2(book, stop, take):
    return next(s for s in ov.load_scenarios()
                if s.kind == "a2" and s.book == book and s.stop_pct == stop / 100 and s.take_profit_pct == take / 100)


# ---------------------------------------------------------------- grid constants stay unhashed
def test_o1_rows_are_24_and_do_not_change_the_hashed_grid():
    from research.intraday_sr.tests import _grids_standin as standin

    assert standin.N_TRIALS == 450
    assert standin.GRID_SHA256 == _HASH_BEFORE_O1
    assert standin.grid_sha256() == _HASH_BEFORE_O1
    assert set(standin.grid_document()) == _HASH_KEYS
    base, a2, n = s0grids.overlay_scaffold()
    assert len(base) == 6 and len(a2) == 18 and n == 456
    assert 192 + 192 + 48 + 24 == 456
    assert {row["book"] for row in base} == {"daily", "weekly"}
    assert {row["structure"] for row in base} == {"long_atm", "long_otm_1", "debit_vertical"}
    assert {row["premium_pct"] for row in base} == {0.005}
    assert {row["time_exit_et"] for row in base} == {"15:55"}
    assert {(row["stop_pct"], row["take_profit_pct"], row["book"]) for row in a2} == set(
        itertools.product((-30, -40, -50), (50, 65, 80), ("daily", "weekly")))
    assert {row["premium_pct"] for row in a2} == {0.02}
    assert {row["premium_dollars_at_100k"] for row in a2} == {2000}
    assert {row["time_exit_et"] for row in a2} == {"15:45"}
    scenarios = ov.load_scenarios()
    assert len(scenarios) == 24
    pairs = ov.paired_scenarios(scenarios)
    assert len(pairs) == 12
    for daily, weekly in pairs:
        assert daily.book == "daily" and weekly.book == "weekly"
        assert (daily.kind, daily.structure, daily.stop_pct, daily.take_profit_pct) == (
            weekly.kind, weekly.structure, weekly.stop_pct, weekly.take_profit_pct)
    for stop, take in itertools.product((-30, -40, -50), (50, 65, 80)):
        pair = [_a2("daily", stop, take), _a2("weekly", stop, take)]
        assert pair[0].structure == pair[1].structure == "long_atm"
        assert pair[0].time_exit == pair[1].time_exit == time(15, 45)
        assert pair[0].premium_pct == pair[1].premium_pct == 0.02


def test_overlay_scaffold_refuses_a_grids_module_without_the_o1_constants(monkeypatch):
    monkeypatch.delattr(s0grids.grids(), "N_PROGRAM_O1", raising=False)
    with pytest.raises(s0grids.GridsUnavailable, match="N_PROGRAM_O1"):
        s0grids.overlay_scaffold()


# ---------------------------------------------------------------- exchange calendar
_GOOD_FRIDAYS = {
    2016: date(2016, 3, 25), 2017: date(2017, 4, 14), 2018: date(2018, 3, 30), 2019: date(2019, 4, 19),
    2020: date(2020, 4, 10), 2021: date(2021, 4, 2), 2022: date(2022, 4, 15), 2023: date(2023, 4, 7),
    2024: date(2024, 3, 29), 2025: date(2025, 4, 18), 2026: date(2026, 4, 3), 2027: date(2027, 3, 26),
}


@pytest.mark.parametrize("year,day", sorted(_GOOD_FRIDAYS.items()))
def test_good_friday(year, day):
    assert cal.good_friday(year) == day


def test_nyse_holidays_and_early_closes_for_known_dates():
    ny = nyse()
    holidays_2024 = {
        date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29), date(2024, 5, 27),
        date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2), date(2024, 11, 28), date(2024, 12, 25),
    }
    assert holidays_2024 <= ny.holidays
    for day in holidays_2024:
        assert not ny.is_open(day)
    assert date(2024, 7, 3) in ny.early_closes and ny.is_open(date(2024, 7, 3))
    assert date(2024, 11, 29) in ny.early_closes
    assert date(2024, 12, 24) in ny.early_closes
    # July 4 2020 was a Saturday: Friday is the full closure, not an early close.
    assert not ny.is_open(date(2020, 7, 3)) and date(2020, 7, 3) not in ny.early_closes
    # July 4 2021 was a Sunday: Monday closed, the Friday before is the early close.
    assert not ny.is_open(date(2021, 7, 5)) and date(2021, 7, 2) in ny.early_closes and ny.is_open(date(2021, 7, 2))
    # July 4 2022 was a Monday. The Friday before was a full session.
    assert not ny.is_open(date(2022, 7, 4))
    assert date(2022, 7, 1) not in ny.early_closes and ny.is_open(date(2022, 7, 1))
    # Christmas 2021 was a Saturday: Friday is a full close.
    assert not ny.is_open(date(2021, 12, 24)) and date(2021, 12, 24) not in ny.early_closes
    # Juneteenth: not an NYSE holiday in 2021; first observed close is Monday 2022-06-20.
    assert ny.is_open(date(2021, 6, 18))
    assert not ny.is_open(date(2022, 6, 20))
    # Mourning days.
    assert not ny.is_open(date(2018, 12, 5))
    assert not ny.is_open(date(2025, 1, 9))
    # New Year's Day 2017 was a Sunday, observed Monday.
    assert not ny.is_open(date(2017, 1, 2))
    assert date(2024, 6, 15).weekday() == 5 and not ny.is_open(date(2024, 6, 15))


def test_friday_holiday_settles_thursday_and_weekly_book_uses_it():
    c = nyse().trading
    assert not c.is_open(date(2026, 4, 3))                     # Good Friday
    assert cal.friday_weekly_settlement("AAPL", date(2026, 4, 3), c) == date(2026, 4, 2)
    assert cal.is_friday_weekly_settlement("AAPL", date(2026, 4, 2), c)
    assert O.expiry_on("AAPL", date(2026, 4, 3), c) is False
    assert O.expiry_on("AAPL", date(2026, 4, 2), c) is True
    # Monday of that week: the weekly book takes the shifted Thursday, not the Friday after next.
    assert cal.weekly_book_expiry("AAPL", date(2026, 3, 30), c) == date(2026, 4, 2)
    assert cal.trading_dte(date(2026, 3, 30), date(2026, 4, 2), c) == 3
    # Single names have no Monday listing, and Tuesday is not listed either, so the daily book skips.
    assert cal.daily_book_expiry("AAPL", date(2026, 3, 30), c) is None


def test_listings_before_dailies_existed_and_dte_le_1():
    c = nyse().trading
    # QQQ Monday 2020-06-01: Monday listings start 2021-10-05, Tuesday listings 2022-11-14.
    assert cal.daily_book_expiry("QQQ", date(2020, 6, 1), c) is None
    assert O.expiry_on("QQQ", date(2020, 6, 1), c) is False
    assert O.expiry_on("QQQ", date(2020, 6, 2), c) is False
    assert cal.weekly_book_expiry("QQQ", date(2020, 6, 1), c) == date(2020, 6, 5)
    # SPY Tuesday 2022-11-08 is before Tuesday dailies (2022-11-14). Wednesday weeklies already
    # exist, and Wednesday is calendar DTE 1, so the daily book takes it. The Tuesday itself is not listed.
    assert O.expiry_on("SPY", date(2022, 11, 8), c) is False
    assert cal.daily_book_expiry("SPY", date(2022, 11, 8), c) == date(2022, 11, 9)
    assert O.expiry_on("SPY", date(2022, 11, 15), c) is True
    assert cal.daily_book_expiry("SPY", date(2022, 11, 15), c) == date(2022, 11, 15)
    # IWM Thursday 2024-04-11 is before Thursday dailies (2024-04-18). Friday is calendar DTE 1.
    assert O.expiry_on("IWM", date(2024, 4, 11), c) is False
    assert O.expiry_on("IWM", date(2024, 4, 16), c) is True          # Tuesday dailies start 2024-04-16
    assert cal.daily_book_expiry("IWM", date(2024, 4, 11), c) == date(2024, 4, 12)
    # A Monday holiday moves a SPY Monday expiry to Tuesday, including before Tuesday dailies existed.
    assert not c.is_open(date(2022, 5, 30))
    assert O.expiry_on("SPY", date(2022, 5, 31), c) is True
    assert O.expiry_on("AAPL", date(2022, 5, 31), c) is False
    assert cal.daily_book_expiry("SPY", date(2022, 5, 31), c) == date(2022, 5, 31)


def test_weekly_book_prefers_the_nearest_friday_inside_the_window():
    c = nyse().trading
    monday = date(2024, 4, 15)
    assert cal.weekly_book_expiry("SPY", monday, c) == date(2024, 4, 19)
    assert not cal.is_friday_weekly_settlement("SPY", date(2024, 4, 18), c)   # native Thursday daily
    assert cal.trading_dte(monday, date(2024, 4, 19), c) == 4
    assert cal.trading_dte(monday, date(2024, 4, 26), c) == 9
    # Thursday: tomorrow's Friday is trading DTE 1, so it belongs to the daily book. Weekly takes the next Friday.
    thursday = date(2024, 4, 18)
    assert cal.daily_book_expiry("AAPL", thursday, c) == date(2024, 4, 19)
    assert cal.weekly_book_expiry("AAPL", thursday, c) == date(2024, 4, 26)
    assert cal.calendar_dte(thursday, date(2024, 4, 19)) == 1
    # The window ends are parameters. A max of 3 drops the Friday that is 4 sessions away.
    assert cal.weekly_book_expiry("SPY", monday, c, dte_max=3) is None
    assert cal.daily_book_expiry("AAPL", thursday, c, max_calendar_dte=0) is None
    # A single-name Wednesday has nothing at DTE 0 or 1. Friday is trading DTE 2, the near end of the weekly window.
    wednesday = date(2024, 4, 17)
    assert cal.daily_book_expiry("AAPL", wednesday, c) is None
    assert cal.weekly_book_expiry("AAPL", wednesday, c) == date(2024, 4, 19)
    assert cal.trading_dte(wednesday, date(2024, 4, 19), c) == 2


# ---------------------------------------------------------------- Black-Scholes, spread, fees
def test_put_call_parity_and_t_to_zero():
    rate = 0.04
    for spot, strike, tenor, sigma in ((100, 100, 10 / 252, 0.2), (450, 445, 3 / 252, 0.18),
                                       (80, 100, 30 / 252, 0.5), (120, 100, 5 / 252, 0.15)):
        call = px.bs_price(spot, strike, tenor, sigma, "call", rate)
        put = px.bs_price(spot, strike, tenor, sigma, "put", rate)
        assert call - put == pytest.approx(spot - strike * math.exp(-rate * tenor), abs=1e-8)
    assert px.bs_price(100, 90, 0.0, 0.2, "call") == pytest.approx(10.0)
    assert px.bs_price(100, 110, 0.0, 0.2, "call") == pytest.approx(0.0)
    assert px.bs_price(100, 110, 1e-12, 0.2, "put") == pytest.approx(10.0)
    assert px.bs_price(100, 90, 1e-12, 0.2, "put") == pytest.approx(0.0)


def test_bs_increases_in_tenor_and_vol_for_atm():
    tenors = [5 / 252, 10 / 252, 21 / 252, 40 / 252]
    for right in ("call", "put"):
        by_t = [px.bs_price(100, 100, t, 0.25, right) for t in tenors]
        assert by_t == sorted(by_t) and len(set(by_t)) == len(by_t)
        assert px.bs_price(100, 100, 30 / 252, 0.40, right) > px.bs_price(100, 100, 30 / 252, 0.20, right)


def test_half_spread_bid_ask_and_commission():
    assert px.half_spread("SPY", 1.0, False, False) == pytest.approx(0.02)
    assert px.half_spread("SPY", 1.0, False, True) == pytest.approx(0.03)          # 0.02 × 1.5 after the clamp
    assert px.half_spread("SPY", 10.0, False, True) == pytest.approx(0.15)         # clamp 0.10, then × 1.5
    assert px.half_spread("SPY", 10.0, False, True, late_mult_after_clamp=False) == pytest.approx(0.10)
    assert px.half_spread("AAPL", 1.0, False, True) == pytest.approx(0.06)         # single names: no ×1.5
    assert px.zero_dte_late(date(2024, 4, 15), date(2024, 4, 15), _at(date(2024, 4, 15), 14, 0)) is True
    assert px.zero_dte_late(date(2024, 4, 19), date(2024, 4, 15), _at(date(2024, 4, 15), 14, 30)) is False
    assert px.zero_dte_late(date(2024, 4, 15), date(2024, 4, 15), _at(date(2024, 4, 15), 13, 59)) is False

    legs = px.structure_legs("long_atm", "SPY", 1, 450.0, 455.0)
    tenor = 5 / 252
    ask = px.package_price(legs, "SPY", 450.0, tenor, 0.18, True, False)
    bid = px.package_price(legs, "SPY", 450.0, tenor, 0.18, False, False)
    assert ask == pytest.approx(O.price_legs(legs, "SPY", 450.0, tenor, 0.18, True, False))
    assert bid == pytest.approx(O.price_legs(legs, "SPY", 450.0, tenor, 0.18, False, False))
    assert ask > bid >= 0
    debit = px.per_contract_cash(ask, 1, 0.05, opening=True)
    credit = px.per_contract_cash(bid, 1, 0.05, opening=False)
    assert debit - credit == pytest.approx((ask - bid) * 100 + 0.10)
    # Two-leg vertical: the fee is per contract, so both legs pay $0.05 on each side.
    vertical = px.structure_legs("debit_vertical", "SPY", 1, 450.0, 455.0)
    assert [leg.qty for leg in vertical] == [1, -1] and vertical[1].strike == 455
    v_ask = px.package_price(vertical, "SPY", 450.0, tenor, 0.18, True, False)
    v_debit = px.per_contract_cash(v_ask, 2, 0.05, opening=True)
    assert v_debit == pytest.approx(v_ask * 100 + 0.10)


def test_structure_legs_match_the_existing_helper_and_the_width_parameter():
    for structure, target, side in itertools.product(("long_atm", "long_otm_1", "debit_vertical"), (450.0, 455.0), (1, -1)):
        got = px.structure_legs(structure, "SPY", side, 450.0, target)
        old = O.legs_for(structure, "SPY", side, 450.0, target)
        assert [(leg.strike, leg.right, leg.qty, leg.otm) for leg in got] == [
            (leg.strike, leg.right, leg.qty, leg.otm) for leg in old]
    wider = px.structure_legs("debit_vertical", "SPY", 1, 450.0, 451.0, min_width_increments=2)
    assert wider[1].strike == 452


# ---------------------------------------------------------------- exit scenarios
def _mark(hh, mm, open_bid, adverse, favorable, time_exit=False, day=date(2024, 4, 15)):
    return px.BarMark(_at(day, hh, mm), open_bid, adverse, favorable, time_exit)


@pytest.mark.parametrize("stop,take", list(itertools.product((-0.30, -0.40, -0.50), (0.50, 0.65, 0.80))))
def test_every_exit_scenario_stop_target_and_time(stop, take):
    ask = 2.0
    stop_px = ask * (1 + stop)
    target_px = ask * (1 + take)
    both = _mark(10, 5, ask, stop_px - 0.05, target_px + 0.05)
    stopped = px.resolve_option_exit([both], ask, stop, take)
    assert stopped.reason == "stop" and stopped.bid == pytest.approx(stop_px)

    only_target = _mark(10, 5, ask, stop_px + 0.05, target_px + 0.05)
    target = px.resolve_option_exit([only_target], ask, stop, take)
    assert target.reason == "take_profit" and target.bid == pytest.approx(target_px)

    only_stop = _mark(10, 5, ask, stop_px - 0.05, target_px - 0.05)
    assert px.resolve_option_exit([only_stop], ask, stop, take).reason == "stop"

    # Open gaps through the target, and the low still reaches the stop: the stop wins.
    gap_and_stop = _mark(10, 5, target_px + 0.25, stop_px - 0.10, target_px + 1)
    gapped = px.resolve_option_exit([gap_and_stop], ask, stop, take)
    assert gapped.reason == "stop" and gapped.bid == pytest.approx(stop_px)

    quiet = _mark(10, 5, ask * 0.99, ask * 0.97, ask * 1.02)
    flat_at = _mark(15, 45, 1.7, 0.01, 9.0, time_exit=True)
    timed = px.resolve_option_exit([quiet, flat_at], ask, stop, take)
    assert timed.reason == "time_exit" and timed.ts.time() == time(15, 45) and timed.bid == pytest.approx(1.7)


def test_time_exit_bar_does_not_use_its_range_and_open_gap_is_a_parameter():
    ask = 2.0
    # The 15:45 bar would stop and take profit on its range; the position is already flat at the open.
    mark = _mark(15, 45, 1.8, 0.1, 8.0, time_exit=True)
    exit_fill = px.resolve_option_exit([mark], ask, -0.30, 0.50)
    assert exit_fill.reason == "time_exit" and exit_fill.bid == pytest.approx(1.8)
    with pytest.raises(NotImplementedError, match="TODO_TIME_EXIT_BAR_RANGE"):
        px.resolve_option_exit([mark], ask, -0.30, 0.50, time_exit_at_bar_open=False)
    # Open is through the stop, the adverse print is not. The open is what makes it a stop.
    gap = _mark(10, 5, 1.0, 1.9, 2.1)
    assert px.resolve_option_exit([gap], ask, -0.30, 0.50).reason == "stop"
    assert px.resolve_option_exit([gap], ask, -0.30, 0.50, gap_uses_open=False).reason == "time_exit"


# ---------------------------------------------------------------- books, path, guardrail
def test_books_share_the_signal_and_differ_by_expiry_and_a_bar_that_touches_both_is_a_stop():
    day = date(2024, 4, 15)                                 # Monday: SPY has a Monday expiry and a Friday weekly
    whip = _day(day, 78, whip=(1, 1.0, 900.0))             # 09:35 bar trades 1 and 900
    bars = _Bars({("SPY", day): whip})
    trade = _trade("SPY", day, (9, 30), (15, 55))
    result = ov.run_overlay([trade], bars, _ctx([day]), [day])
    daily_exp = cal.daily_book_expiry("SPY", day, nyse().trading)
    weekly_exp = cal.weekly_book_expiry("SPY", day, nyse().trading)
    assert daily_exp == day and weekly_exp == date(2024, 4, 19) and daily_exp != weekly_exp
    for stop, take in itertools.product((-30, -40, -50), (50, 65, 80)):
        for book, expiry in (("daily", daily_exp), ("weekly", weekly_exp)):
            scenario = _a2(book, stop, take)
            portfolio = result[scenario.variant_id]
            assert portfolio.skips["no_expiry"] == 0 and len(portfolio.priced) == 1
            record = portfolio.priced[0]
            assert record["reason"] == "stop" and record["expiry"] == expiry and record["book"] == book
            ask = (record["debit_pc"] - 0.05) / 100
            assert record["credit_pc"] == pytest.approx(ask * (1 + stop / 100) * 100 - 0.05)
            assert list(portfolio.trades["symbol"]) == ["SPY"]
    # Baseline follows the equity exit (15:55), on both books, at 0.5% rather than the A2 2%.
    for structure in ("long_atm", "long_otm_1", "debit_vertical"):
        ids = [s.variant_id for s in ov.load_scenarios() if s.kind == "baseline" and s.structure == structure]
        books = [result[i] for i in ids]
        assert {p.scenario.book for p in books} == {"daily", "weekly"}
        assert {p.priced[0]["expiry"] for p in books} == {daily_exp, weekly_exp}
        assert {p.priced[0]["reason"] for p in books} == {"forced_eod"}
        assert {p.priced[0]["exit_ts"] for p in books} == {_at(day, 15, 55)}
        assert {p.scenario.premium_pct for p in books} == {0.005}


def test_flat_weekly_exits_at_1545_and_early_close_uses_1245_and_nothing_is_held_overnight():
    day = date(2024, 4, 15)
    bars = _Bars({("SPY", day): _day(day, 78)})
    trade = _trade("SPY", day, (10, 0), (15, 55))
    result = ov.run_overlay([trade], bars, _ctx([day]), [day])
    for stop, take in itertools.product((-30, -40, -50), (50, 65, 80)):
        record = result[_a2("weekly", stop, take).variant_id].priced[0]
        assert record["reason"] == "time_exit" and record["exit_ts"].time() == time(15, 45)
        assert record["exit_ts"].date() == day
    # A next-morning equity exit is clamped to the entry session. A2 never looks at it.
    overnight = _trade("SPY", day, (10, 0), _at(date(2024, 4, 16), 10, 0))
    clamped = ov.run_overlay([overnight], bars, _ctx([day]), [day])
    baseline = next(s for s in ov.load_scenarios() if s.kind == "baseline" and s.book == "weekly" and s.structure == "long_atm")
    assert clamped[baseline.variant_id].priced[0]["reason"] == "time_exit"
    assert clamped[baseline.variant_id].priced[0]["exit_ts"] == _at(day, 15, 55)
    assert clamped[_a2("weekly", -30, 50).variant_id].priced[0]["exit_ts"].time() == time(15, 45)

    early = date(2024, 7, 3)
    early_bars = _Bars({("SPY", early): _day(early, 42)})
    early_trade = _trade("SPY", early, (10, 0), (12, 55), reason="forced_eod")
    early_result = ov.run_overlay([early_trade], early_bars, _ctx([early]), [early])
    assert early_result[_a2("weekly", -50, 80).variant_id].priced[0]["exit_ts"].time() == time(12, 45)
    assert early_result[_a2("weekly", -50, 80).variant_id].priced[0]["expiry"] == date(2024, 7, 12)


def test_missing_expiry_is_a_skip_and_not_a_loss():
    day = date(2024, 4, 17)                                 # Wednesday: AAPL has no Wed/Thu listing
    bars = _Bars({("AAPL", day): _day(day, 78, price=500.0)})
    trade = _trade("AAPL", day, (10, 0), (15, 55), price=500.0)
    rv = {("AAPL", day): 0.25, ("SPY", day): 0.16}
    result = ov.run_overlay([trade], bars, _ctx([day], rv), [day])
    daily = _a2("daily", -30, 50)
    weekly = _a2("weekly", -30, 50)
    assert result[daily.variant_id].skips["no_expiry"] == 1 and result[daily.variant_id].priced == []
    assert result[daily.variant_id].trades.empty
    assert int(result[daily.variant_id].sessions.iloc[0].day_losses) == 0
    assert result[weekly.variant_id].skips["no_expiry"] == 0
    assert result[weekly.variant_id].priced[0]["expiry"] == date(2024, 4, 19)
    assert len(result[weekly.variant_id].trades) == 1


def test_two_thousand_dollar_budget_is_two_percent_of_day_start_equity():
    day = date(2024, 4, 22)
    scenario = _a2("daily", -30, 50)
    record = dict(symbol="SPY", session=day, entry_ts=_at(day, 10, 0), exit_ts=_at(day, 15, 45),
                  debit_pc=100.0, credit_pc=80.0)
    result = ov.run_portfolios({scenario.variant_id: [record]}, [day], scenarios=[scenario])
    taken = result[scenario.variant_id].trades.iloc[0]
    assert taken.contracts == 20 and taken.premium == pytest.approx(2000.0)   # floor($2,000 / $100)
    with pytest.raises(NotImplementedError, match="TODO_PREMIUM_BUDGET_MODE"):
        ov.run_portfolios({scenario.variant_id: [record]}, [day], scenarios=[scenario],
                          params=ov.OverlayParams(premium_budget_mode="fixed_dollars"))


def _cash(day, symbol, hour, win):
    return dict(symbol=symbol, session=day, entry_ts=_at(day, hour, 0), exit_ts=_at(day, hour, 30),
                debit_pc=100.0, credit_pc=180.0 if win else 40.0)


def test_guardrail_comes_from_grids_and_books_do_not_share_losses(monkeypatch):
    """A daily-book loss must not block the weekly book. Limits are grids.PRIMARY_GUARDRAIL, read at the call."""
    g = s0grids.grids()
    monkeypatch.setattr(g, "PRIMARY_GUARDRAIL", {"daily_losses": 1, "weekly_losses": 1})
    assert s0grids.primary_guardrail() == (1, 1)
    monday, tuesday = date(2024, 4, 22), date(2024, 4, 23)
    daily, weekly = _a2("daily", -30, 50), _a2("weekly", -30, 50)
    priced = {
        daily.variant_id: [_cash(monday, "A", 10, False), _cash(monday, "B", 11, False), _cash(tuesday, "C", 10, True)],
        weekly.variant_id: [_cash(monday, "A", 10, True), _cash(tuesday, "C", 10, True)],
    }
    result = ov.run_portfolios(priced, [monday, tuesday], scenarios=[daily, weekly])
    assert list(result[daily.variant_id].trades["symbol"]) == ["A"]
    assert list(result[weekly.variant_id].trades["symbol"]) == ["A", "C"]
    assert result[daily.variant_id].counters["signals_arrived_blocked"] == 2
    assert result[weekly.variant_id].counters["signals_arrived_blocked"] == 0
    # The default grids constant is still d2+w5; the patch above is what this call observed.
    monkeypatch.undo()
    assert s0grids.primary_guardrail() == (2, 5)


def test_open_todos_are_named_parameters():
    import inspect
    blob = inspect.getsource(ov) + inspect.getsource(px) + inspect.getsource(cal)
    for name in ov.OPEN_TODOS:
        assert name in blob
    params = ov.OverlayParams()
    assert params.daily_calendar_dte_max == 1
    assert (params.weekly_trading_dte_min, params.weekly_trading_dte_max) == (2, 10)
    assert params.time_exit_early == time(12, 45)
    assert params.premium_budget_mode == "pct_of_day_start"
    assert params.stop_basis == "entry_ask"
    assert params.vertical_min_width_increments == 1
