import math
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from research.intraday_sr.harness import options as O
from research.intraday_sr.harness.config import GUARDRAILS

ET = ZoneInfo("America/New_York")


def cal(a=date(2016, 1, 1), b=date(2026, 12, 31)):
    days = pd.bdate_range(a, b).date
    return O.TradingCalendar([d for d in days if d not in (date(2024, 7, 4), date(2022, 11, 24))])


def test_bs_put_call_parity_and_limits():
    S, K, T, s, r = 100.0, 103.0, 10 / 252, 0.25, 0.04
    c, p = O.bs_price(S, K, T, s, "call", r), O.bs_price(S, K, T, s, "put", r)
    assert c - p == pytest.approx(S - K * math.exp(-r * T), abs=1e-9)
    assert O.bs_price(S, 90, 1e-9, s, "call", r) == pytest.approx(10.0, abs=1e-6)


def test_bs_matches_repo_replay_if_importable():
    try:
        from alpaca_options_credit.replay.credit import bs_price as repo_bs
    except Exception:
        pytest.skip("repo src not on path")
    for right in ("call", "put"):
        assert O.bs_price(450, 445, 3 / 252, 0.18, right, 0.04) == pytest.approx(
            repo_bs(450, 445, 3 / 252, 0.18, right, rate=0.04, div=0.0), rel=1e-9)


@pytest.mark.parametrize("sym,d,exp", [
    ("SPY", date(2022, 11, 15), True),    # Tuesday, after 2022-11-14 start
    ("SPY", date(2022, 11, 8), False),    # Tuesday, before
    ("SPY", date(2022, 11, 9), True),     # Wednesday
    ("IWM", date(2024, 4, 23), True),     # Tuesday after 2024-04-16
    ("IWM", date(2024, 4, 9), False),
    ("ADBE", date(2024, 4, 26), True),    # Friday
    ("ADBE", date(2024, 4, 24), False),   # Wednesday, single name
])
def test_expiry_calendar(sym, d, exp):
    assert O.expiry_on(sym, d, cal()) is exp


def test_holiday_friday_expiry_shifts_to_thursday():
    c = O.TradingCalendar([d for d in pd.bdate_range("2026-03-30", "2026-04-10").date if d != date(2026, 4, 3)])
    assert O.expiry_on("ADBE", date(2026, 4, 2), c) is True


def test_package_pricing_debit_credit():
    legs = O.legs_for("debit_vertical", "SPY", 1, 450.0, 455.0)
    assert [l.qty for l in legs] == [1, -1] and legs[1].strike == 455
    ty = 3 / (252)
    debit = O.price_legs(legs, "SPY", 450.0, ty, 0.18, True, False)
    credit = O.price_legs(legs, "SPY", 450.0, ty, 0.18, False, False)
    assert debit > credit >= 0                      # round trip loses the spread
    assert debit < O.price_legs(legs[:1], "SPY", 450.0, ty, 0.18, True, False)


def test_t_years_floor_and_same_day():
    c = cal()
    now = datetime(2024, 4, 23, 15, 50, tzinfo=ET)
    assert O.t_years(now, date(2024, 4, 23), c) == pytest.approx(15 / (252 * 390))
    now = datetime(2024, 4, 23, 10, 0, tzinfo=ET)
    assert O.t_years(now, date(2024, 4, 23), c) == pytest.approx(360 / (252 * 390))


# ---------------------------------------------------------------- 0DTE path
def _day(sym, d, path_close, base=450.0):
    rows, ts = [], datetime.combine(d, time(9, 30), tzinfo=ET)
    px = base
    for i, c in enumerate(path_close):
        o = px
        rows.append(dict(ts=ts + timedelta(minutes=5 * i), open=o, high=max(o, c) + 0.05, low=min(o, c) - 0.05,
                         close=c, volume=1e5))
        px = c
    return pd.DataFrame(rows)


class _Bars:
    def __init__(self, frames):
        self.frames = frames

    def session_frame(self, sym, d):
        return self.frames[(sym, d)]


def _trade(sym, d, ts, price, side=1):
    f = SimpleNamespace(symbol=sym, ts=ts, price=price, side=side, reason="entry")
    return SimpleNamespace(entry=f, exit=f, signal=SimpleNamespace(stop=price - side, targets={}))


def _ctx(d):
    return O.IVContext(calendar=cal(), vix9d_prev={d: 15.0}, vix_prev={d: 16.0}, rv20={})


def test_zero_dte_time_exit_and_skips():
    d = date(2024, 4, 23)
    flat = [450.0] * 78
    bars = _Bars({("SPY", d): _day("SPY", d, flat)})
    t0 = datetime.combine(d, time(10, 0), tzinfo=ET)
    trs = [_trade("SPY", d, t0, 450.0), _trade("ADBE", date(2024, 4, 24), t0, 500.0)]
    out, sk = O.zero_dte_records(trs, bars, _ctx(d))
    assert sk["no_same_day_expiry"] == 1
    for cell, recs in out.items():
        assert len(recs) == 1
        r = recs[0]
        # flat underlying: theta decay alone; either stop (if decay > |stop|) or time exit at 15:45 open
        assert r["reason"] in ("time_exit", "stop")
        if r["reason"] == "time_exit":
            assert r["exit_ts"].time() == time(15, 45)
        assert r["credit_pc"] < r["debit_pc"]


def test_zero_dte_take_profit_and_stop_priority():
    d = date(2024, 4, 23)
    up = [450.0] * 6 + [453.0] * 72          # 30 min after open, +3 points
    bars = _Bars({("SPY", d): _day("SPY", d, up)})
    t0 = datetime.combine(d, time(9, 30), tzinfo=ET)
    out, _ = O.zero_dte_records([_trade("SPY", d, t0, 450.0)], bars, _ctx(d))
    assert all(recs[0]["reason"] == "take_profit" for recs in out.values())
    # a bar spanning both stop and TP -> stop
    whip = _day("SPY", d, [450.0] * 78)
    whip.loc[3, ["high", "low"]] = [456.0, 444.0]
    out, _ = O.zero_dte_records([_trade("SPY", d, t0, 450.0)], _Bars({("SPY", d): whip}), _ctx(d))
    assert all(recs[0]["reason"] == "stop" for recs in out.values())
    for (sl, tp), recs in out.items():
        r = recs[0]
        assert r["credit_pc"] == pytest.approx((r["debit_pc"] - 0.05) / 100 * (1 + sl) * 100 - 0.05)


def test_options_account_caps_and_daily_stop():
    d = date(2024, 4, 23)
    t = lambda h, m: datetime.combine(d, time(h, m), tzinfo=ET)
    recs = [dict(symbol=f"S{i}", session=d, entry_ts=t(10, i), exit_ts=t(15, 0), debit_pc=100.0, credit_pc=0.0)
            for i in range(6)]
    daily, taken, c, _ = O.options_account(recs, [d], 0.02, max_losses_day=None, max_losses_week=None)
    assert len(taken) == 4 and c["skip_concurrency"] == 2
    assert daily.iloc[0] == pytest.approx(-0.08)
    recs2 = [dict(symbol="A", session=d, entry_ts=t(10, 0), exit_ts=t(10, 30), debit_pc=100.0, credit_pc=0.0),
             dict(symbol="B", session=d, entry_ts=t(11, 0), exit_ts=t(12, 0), debit_pc=100.0, credit_pc=200.0)]
    daily, taken, c, s = O.options_account(recs2, [d], 0.02)
    assert len(taken) == 1 and c["skip_daily_stop"] == 1 and s.iloc[0].daily_stop_ts == t(10, 30)


def test_options_guardrail_counts_option_losses_own_portfolio():
    """G6: losses are the option trades' own R < 0 (after spread/fees), d2 per session, w5 per week."""
    t = lambda d, h, m: datetime.combine(d, time(h, m), tzinfo=ET)
    mon, tue, wed, thu = (date(2024, 4, 22 + i) for i in range(4))
    def rec(d, sym, h, win):
        return dict(symbol=sym, session=d, entry_ts=t(d, h, 0), exit_ts=t(d, h, 30), debit_pc=100.0,
                    credit_pc=180.0 if win else 50.0)
    recs = [rec(mon, "A", 10, False), rec(mon, "B", 11, True), rec(mon, "C", 12, False), rec(mon, "D", 13, True),
            rec(tue, "A", 10, False), rec(tue, "B", 11, False), rec(tue, "C", 12, True),
            rec(wed, "A", 10, False), rec(wed, "B", 11, True),
            rec(thu, "A", 10, True)]
    daily, taken, c, s = O.options_account(recs, [mon, tue, wed, thu], 0.002)
    got = [(r["session"], r["symbol"]) for _, r in taken.iterrows()]
    # Mon: A loss, B win (wins never reduce a count), C loss -> trip, D blocked. Tue: A, B losses -> 4, trip, C
    # blocked. Wed: A loss = 5th -> week trip; B blocked. Thu: blocked (same week).
    assert got == [(mon, "A"), (mon, "B"), (mon, "C"), (tue, "A"), (tue, "B"), (wed, "A")]
    assert c["signals_arrived_blocked"] == 4 and c["signals_cancelled_at_trip"] == 0 and c["days_halted_day_limit"] == 2 and c["weeks_halted_week_limit"] == 1
    assert list(taken["week_losses_before"]) == [0, 1, 1, 2, 3, 4]
