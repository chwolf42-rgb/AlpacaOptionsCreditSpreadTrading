"""D1-1 / G8: hand-computed guardrail fixture for SPEC v1.3 G3 (primary d2+w5).

Every fixture trade is a long: signal trigger 100.10, stop 99.50, ATR_d 2.0, tier T2 (3.5 bp/side).
A "loser" fills at 100.10 on bar i (bar 100.05/100.30/100.00/100.20) and stops at 99.50 on bar i+1
(bar 100.20/100.25/99.40/99.45). Risk 0.2% of $100k -> qty floor(200/0.60) = 333 shares, so
    entry cost  = 333 * 3.5e-4 * 100.10                      = 11.666655
    stop cost   = 2 * 333 * 3.5e-4 * 99.50 + 0.3e-4*333*99.50 = 24.187455
    pnl         = 333 * (99.50 - 100.10) - 11.666655 - 24.187455 = -235.654110
    R           = -235.654110 / 199.8                        = -1.179450   (a loss: R < 0)
Each loss is -0.236% of equity, so the -1.5% daily stop never interferes except in its own test.
"""
from datetime import date

import pandas as pd
import pytest

from research.intraday_sr.harness.config import COMPARISON, PRIMARY, CostCfg, RiskCfg
from research.intraday_sr.harness.portfolio import FrameBarSource, simulate
from research.intraday_sr.tests._harness_stubs import flat_day, idx, sig, t

RISK = RiskCfg(risk_pct=0.002)                 # defaults: max_losses_day=2, max_losses_week=5 (primary)
FILL = (100.05, 100.30, 100.00, 100.20)        # trades through 100.10 -> fill at 100.10
STOPBAR = (100.20, 100.25, 99.40, 99.45)       # through 99.50 -> stop at 99.50
FILL_AND_STOP = (100.05, 100.30, 99.40, 99.45) # fill bar that also reaches the stop
TF = lambda s, d: "T2"
LOSS_R = -235.654110 / 199.8


def hhmm(i):
    h, m = divmod(570 + 5 * i, 60)
    return f"{h:02d}:{m:02d}"


class Book:
    """Collects per-(symbol, day) bar overrides and signals; builds one FrameBarSource."""

    def __init__(self):
        self.ov, self.sigs = {}, []

    def bar(self, sym, day, at, row):
        self.ov.setdefault((sym, day), {})[idx(at)] = row

    def signal(self, sym, day, at, score=0.8, expires="15:00"):
        self.sigs.append(sig(sym, day, at, score=score, expires=expires))

    def loser(self, sym, day, at, score=0.8):
        """Armed at `at`, fills on the `at` bar, stops on the next bar."""
        self.signal(sym, day, at, score)
        self.bar(sym, day, at, FILL)
        self.bar(sym, day, hhmm(idx(at) + 1), STOPBAR)

    def src(self):
        frames = {}
        for (sym, day), ov in self.ov.items():
            frames.setdefault(sym, []).append(flat_day(sym, day, overrides=ov))
        for s in self.sigs:                                   # symbols with signals but no overrides
            if (s.symbol, s.available_at.date().isoformat()) not in self.ov:
                frames.setdefault(s.symbol, []).append(flat_day(s.symbol, s.available_at.date().isoformat()))
        return FrameBarSource({k: pd.concat(v, ignore_index=True).drop_duplicates("ts") for k, v in frames.items()})

    def run(self, risk=RISK, costs=CostCfg(), sessions=None, **kw):
        return simulate(self.sigs, self.src(), risk, costs, tier_fn=TF, sessions=sessions, **kw)


def syms(res):
    return [tr.entry.symbol for tr in res.trades]


def test_default_riskcfg_is_primary_d2_w5():
    assert (RiskCfg().max_losses_day, RiskCfg().max_losses_week) == (2, 5)
    assert PRIMARY.name == "d2+w5" and [g.name for g in COMPARISON] == ["none", "d2+w6"]


def test_loss_r_hand_computed_and_two_losses_block_rest_of_day_portfolio_wide():
    D = "2024-03-04"
    b = Book()
    b.loser("AAA", D, "10:00")                 # loss 1, exit 10:05
    b.loser("BBB", D, "10:30")                 # loss 2, exit 10:35 -> day limit trips (different symbol)
    b.signal("CCC", D, "10:20")                # armed before the trip, would fill at 11:00 -> cancelled at 10:35
    b.bar("CCC", D, "11:00", FILL)
    b.signal("DDD", D, "11:30")                # arrives after the trip -> never armed (blocked)
    b.bar("DDD", D, "11:30", FILL)
    r = b.run()
    assert syms(r) == ["AAA", "BBB"]
    assert r.trades[0].r == pytest.approx(LOSS_R, abs=1e-6) and r.trades[0].pnl == pytest.approx(-235.654110, abs=1e-6)
    s = r.sessions.iloc[0]
    assert s.day_limit_trip_ts == t(D, "10:35") and s.day_losses == 2
    assert s.signals_cancelled_at_trip == 1 and s.signals_arrived_blocked == 1          # CCC armed, DDD arriving
    assert [m["tripped"] for m in r.meta] == [None, "day"]
    assert [m["day_losses_before"] for m in r.meta] == [0, 1]
    # comparison 'none': all four trade (CCC, DDD are flat after fill -> forced exit)
    n = b.run(risk=COMPARISON[0].apply(RISK))
    assert syms(n) == ["AAA", "BBB", "CCC", "DDD"]


def test_open_trade_runs_to_normal_exit_and_post_trip_loss_counts_toward_week():
    D = "2024-03-04"
    b = Book()
    b.signal("EEE", D, "10:10")               # fills 10:10, stays open through the trip, stops at 12:00
    b.bar("EEE", D, "10:10", FILL)
    b.bar("EEE", D, "12:00", STOPBAR)
    b.loser("AAA", D, "10:20")
    b.loser("BBB", D, "10:30")                # trip at 10:35 while EEE is open
    r = b.run()
    assert syms(r) == ["AAA", "BBB", "EEE"]          # trade log is in exit order
    by = {tr.entry.symbol: (tr, m) for tr, m in zip(r.trades, r.meta)}
    tr, m = by["EEE"]
    assert tr.exit.reason == "stop" and tr.exit.ts == t(D, "12:00")   # not flattened by the guardrail
    assert m["is_loss"]
    s = r.sessions.iloc[0]
    assert s.day_losses == 3 and s.week_losses_end == 3                # the post-trip loss counts


def _week(b, days_losses):
    for day, n in days_losses:
        for k in range(n):
            b.loser(["AAA", "BBB", "CCC"][k], day, ["10:00", "10:30", "11:00"][k])


def test_fifth_weekly_loss_blocks_through_friday_and_resets_after_monday_holiday():
    # week of Mon 2024-05-20; next Monday 2024-05-27 is Memorial Day (no session) -> reset on Tue 05-28
    b = Book()
    _week(b, [("2024-05-20", 2), ("2024-05-21", 2), ("2024-05-22", 1)])   # 5th loss Wed 10:05
    b.signal("DDD", "2024-05-22", "11:00"); b.bar("DDD", "2024-05-22", "11:00", FILL)
    for day in ("2024-05-23", "2024-05-24"):                                  # Thu, Fri: blocked all day
        b.signal("DDD", day, "10:00"); b.bar("DDD", day, "10:00", FILL)
    b.signal("DDD", "2024-05-28", "10:00"); b.bar("DDD", "2024-05-28", "10:00", FILL)
    sessions = [date(2024, 5, d) for d in (20, 21, 22, 23, 24, 28)]
    r = b.run(sessions=sessions)
    days = [tr.entry.ts.date() for tr in r.trades]
    assert days.count(date(2024, 5, 22)) == 1 and date(2024, 5, 23) not in days and date(2024, 5, 24) not in days
    assert days[-1] == date(2024, 5, 28) and r.trades[-1].entry.symbol == "DDD"
    ss = r.sessions.set_index("session")
    assert ss.loc[date(2024, 5, 22), "week_limit_trip_ts"] == t("2024-05-22", "10:05")
    assert bool(ss.loc[date(2024, 5, 23), "week_blocked_at_open"]) and ss.loc[date(2024, 5, 24), "signals_arrived_blocked"] == 1
    assert ss.loc[date(2024, 5, 22), "signals_cancelled_at_trip"] == 0 and ss.loc[date(2024, 5, 22), "signals_arrived_blocked"] == 1
    assert ss.loc[date(2024, 5, 28), "week_losses_start"] == 0
    # d2+w6 comparison: the Wednesday 11:00 trade is allowed (5 < 6); it ends flat at 100.00 at the forced
    # exit (R < 0), the 6th loss, so Thu/Fri are blocked under d2+w6 as well
    r6 = b.run(risk=COMPARISON[1].apply(RISK), sessions=sessions)
    d6 = [tr.entry.ts.date() for tr in r6.trades]
    assert d6.count(date(2024, 5, 22)) == 2 and date(2024, 5, 23) not in d6


def test_several_exits_in_one_bar_symbol_tiebreak_names_the_tripping_exit():
    D = "2024-03-04"
    b = Book()
    b.loser("ZZZ", D, "10:00")                 # loss 1
    for s_ in ("BBB", "AAA"):                  # both fill 11:00, both stop in the 11:05 bar
        b.signal(s_, D, "11:00")
        b.bar(s_, D, "11:00", FILL)
        b.bar(s_, D, "11:05", STOPBAR)
    r = b.run()
    exits = [(tr.exit.ts, tr.entry.symbol, m["tripped"]) for tr, m in zip(r.trades, r.meta)]
    assert exits[1:] == [(t(D, "11:05"), "AAA", "day"), (t(D, "11:05"), "BBB", None)]
    assert r.sessions.iloc[0].day_losses == 3


def test_exits_processed_before_entries_in_the_same_bar():
    D = "2024-03-04"
    b = Book()
    b.loser("AAA", D, "10:00")                 # loss 1
    b.loser("BBB", D, "11:00")                 # loss 2 at the 11:05 bar (step i)
    b.signal("CCC", D, "10:30")                # armed; first trades through its trigger in the 11:05 bar
    b.bar("CCC", D, "11:05", FILL)
    r = b.run()
    assert syms(r) == ["AAA", "BBB"]           # CCC would have filled in step (ii) of the same bar -> blocked
    assert r.sessions.iloc[0].signals_cancelled_at_trip == 1 and r.sessions.iloc[0].signals_arrived_blocked == 0


def test_entry_stopped_in_fill_bar_counts_before_next_entry():
    D = "2024-03-04"
    b = Book()
    b.loser("ZZZ", D, "10:00")                         # loss 1
    b.signal("AAA", D, "11:00", score=0.9); b.bar("AAA", D, "11:00", FILL_AND_STOP)   # higher priority
    b.signal("BBB", D, "11:00", score=0.8); b.bar("BBB", D, "11:00", FILL)
    r = b.run()
    assert syms(r) == ["ZZZ", "AAA"] and r.trades[1].exit.ts == t(D, "11:00")
    assert r.meta[1]["tripped"] == "day"
    n = b.run(risk=COMPARISON[0].apply(RISK))
    assert syms(n) == ["ZZZ", "AAA", "BBB"]


def test_zero_r_scratch_is_not_a_loss():
    """R = 0 needs zero costs: with real costs an exit at the entry price is R < 0 and IS a loss."""
    D = "2024-03-04"
    free = CostCfg(mult=0.0, reg_fee_bp_sell=0.0)
    b = Book()
    b.loser("AAA", D, "10:00")                         # loss 1
    b.signal("BBB", D, "10:30"); b.bar("BBB", D, "10:30", FILL)
    b.signal("CCC", D, "14:00"); b.bar("CCC", D, "14:00", FILL)   # ends at 100.00 (forced exit): a loss
    # BBB flat at 100.10 after its fill -> forced exit at the 15:55 open = entry price -> pnl 0, R 0
    for i in range(idx("10:35"), idx("15:55") + 1):
        b.bar("BBB", D, hhmm(i), (100.10, 100.12, 100.08, 100.10))
    r = b.run(costs=free)
    rs = {tr.entry.symbol: tr.r for tr in r.trades}
    assert rs["BBB"] == 0.0 and "CCC" in rs            # the scratch did not trip the day limit
    assert [m["symbol"] for m in r.meta if m["is_loss"]] == ["AAA", "CCC"]
    assert {m["symbol"]: m["day_losses_before"] for m in r.meta}["CCC"] == 1   # BBB's R = 0 not counted


def test_daily_stop_flatten_losses_count_toward_week():
    D = "2024-03-04"
    b = Book()
    # 4 longs, stop 98.00 (wide), fill at 10:00; all close at 98.50 on the 10:05 bar -> open loss > 1.5% -> flatten
    for s_ in ("AAA", "BBB", "CCC", "DDD"):
        b.sigs.append(sig(s_, D, "10:00", stop=98.0, expires="15:00"))
        b.bar(s_, D, "10:00", FILL)
        b.bar(s_, D, "10:05", (100.0, 100.0, 98.45, 98.50))
        b.bar(s_, D, "10:10", (98.50, 98.55, 98.45, 98.50))
    r = b.run(risk=RiskCfg())                           # 0.5% risk: qty floor(500/2.10) = 238 each
    assert [tr.exit.reason for tr in r.trades] == ["daily_stop"] * 4
    s = r.sessions.iloc[0]
    assert s.daily_stop_ts == t(D, "10:10") and s.day_losses == 4 and s.week_losses_end == 4
    assert s.day_limit_trip_ts == t(D, "10:10")


def test_week_counter_restarts_at_window_start_midweek():
    # 2025-10-01 (Wed) is a fold test start: Mon/Tue losses do not carry into the new window
    b = Book()
    _week(b, [("2025-09-29", 2), ("2025-09-30", 2), ("2025-10-01", 1)])
    b.signal("DDD", "2025-10-01", "11:00"); b.bar("DDD", "2025-10-01", "11:00", FILL)
    r = b.run(sessions=[date(2025, 9, 29), date(2025, 9, 30), date(2025, 10, 1)])
    assert "DDD" in syms(r)
    assert r.sessions.set_index("session").loc[date(2025, 10, 1), "week_losses_start"] == 0
