"""D1-1: fills, portfolio rules and costs on HAND-COMPUTED fixture trades (SPEC sections 4.1-4.2, 5, 7)."""

import math

import pytest

from research.intraday_sr.harness import costs as K
from research.intraday_sr.harness.config import CostCfg, RiskCfg
from research.intraday_sr.harness.fills import exit_on_bar, stop_entry_fill
from research.intraday_sr.harness.portfolio import FrameBarSource, simulate
from research.intraday_sr.tests._harness_stubs import flat_day, idx, sig

D = "2024-03-04"


def run(frames, sigs, risk=RiskCfg(), costs=CostCfg()):
    return simulate(sigs, FrameBarSource(frames), risk, costs, tier_fn=lambda s, d: "T2" if s not in ("SPY",) else "T0")


def test_long_target_hand_computed():
    ov = {idx("10:00"): (100.00, 100.05, 99.95, 100.00),
          idx("10:05"): (100.05, 100.30, 100.00, 100.20),   # trades through 100.10 -> fill max(100.10, 100.05)
          idx("10:10"): (100.20, 100.72, 100.15, 100.60)}   # 1R target 100.70 traded through by $0.01
    r = run({"AAA": flat_day("AAA", D, overrides=ov)}, [sig()])
    assert len(r.trades) == 1
    tr = r.trades[0]
    assert tr.entry.price == pytest.approx(100.10) and tr.entry.qty == 833       # floor(500 / 0.60)
    assert tr.entry.cost == pytest.approx(833 * 3.5e-4 * 100.10)                 # 29.18410
    assert tr.exit.price == pytest.approx(100.70) and tr.exit.reason == "target"
    assert tr.exit.cost == pytest.approx(0.3e-4 * 833 * 100.70)                  # reg fee only: 2.516493
    assert tr.pnl == pytest.approx(468.09940, abs=1e-4)                           # 499.8 - 29.1841 - 2.516493
    assert tr.r == pytest.approx(468.09940 / 499.8, abs=1e-6)


def test_target_needs_trade_through_by_one_cent():
    ov = {idx("10:05"): (100.05, 100.30, 100.00, 100.20), idx("10:10"): (100.20, 100.705, 100.15, 100.60)}
    r = run({"AAA": flat_day("AAA", D, overrides=ov)}, [sig()])
    assert r.trades[0].exit.reason == "forced_eod"           # 100.705 < 100.71: no fill; flat bars until 15:55


def test_gap_through_stop_fills_at_open_with_2x_cost():
    ov = {idx("10:05"): (100.05, 100.30, 100.00, 100.20), idx("10:10"): (99.30, 99.40, 99.20, 99.35)}
    r = run({"AAA": flat_day("AAA", D, overrides=ov)}, [sig()])
    tr = r.trades[0]
    assert tr.exit.reason == "stop" and tr.exit.price == pytest.approx(99.30)
    assert tr.exit.cost == pytest.approx(2 * 833 * 3.5e-4 * 99.30 + 0.3e-4 * 833 * 99.30)
    assert tr.pnl == pytest.approx(833 * (99.30 - 100.10) - tr.entry.cost - tr.exit.cost)


def test_same_bar_stop_and_target_assumes_stop():
    ov = {idx("10:05"): (100.05, 100.30, 100.00, 100.20), idx("10:10"): (100.20, 100.80, 99.40, 100.0)}
    r = run({"AAA": flat_day("AAA", D, overrides=ov)}, [sig()])
    assert r.trades[0].exit.reason == "stop" and r.trades[0].exit.price == pytest.approx(99.50)
    assert r.meta[0]["ambiguous_same_bar"] is True


def test_forced_exit_at_1555_open():
    ov = {idx("10:05"): (100.05, 100.30, 100.00, 100.20), idx("15:55"): (100.33, 100.40, 100.30, 100.35)}
    r = run({"AAA": flat_day("AAA", D, overrides=ov)}, [sig()])
    tr = r.trades[0]
    assert tr.exit.reason == "forced_eod" and tr.exit.price == pytest.approx(100.33)
    assert tr.exit.ts.strftime("%H:%M") == "15:55"
    assert tr.exit.cost == pytest.approx(2 * 833 * 3.5e-4 * 100.33 + 0.3e-4 * 833 * 100.33)


def test_short_mirror_and_reg_fee_on_entry():
    ov = {idx("10:05"): (99.95, 100.00, 99.70, 99.80), idx("10:10"): (99.80, 99.85, 99.28, 99.30)}
    s = sig(direction=-1, trigger=99.90, stop=100.50, zlo=100.1, zhi=100.4, zone_target=98.0)
    r = run({"AAA": flat_day("AAA", D, overrides=ov)}, [s])
    tr = r.trades[0]
    assert tr.entry.price == pytest.approx(99.90) and tr.entry.qty == 833        # min(99.90, open 99.95)
    assert tr.entry.cost == pytest.approx(833 * 3.5e-4 * 99.90 + 0.3e-4 * 833 * 99.90)   # sell: reg fee
    assert tr.exit.reason == "target" and tr.exit.price == pytest.approx(99.30)  # 1R = 99.30, low 99.28 <= 99.29
    assert tr.exit.cost == pytest.approx(0.0)


def test_entry_bar_checks_stop_only():
    ov = {idx("10:05"): (100.05, 100.90, 100.00, 100.80)}   # fill 100.10 and touch 1R 100.70 in the same bar
    r = run({"AAA": flat_day("AAA", D, overrides=ov)}, [sig()])
    assert r.trades[0].exit.reason == "forced_eod"


def test_expiry_and_zone_close_cancel():
    ov = {idx("10:30"): (100.05, 100.30, 100.00, 100.20)}   # trigger only after expires_at 10:30
    r = run({"AAA": flat_day("AAA", D, overrides=ov)}, [sig()])
    assert len(r.trades) == 0 and r.counters.get("expired", 0) == 1
    ov = {idx("10:00"): (99.70, 99.75, 99.40, 99.45), idx("10:05"): (100.05, 100.30, 100.00, 100.20)}
    r = run({"AAA": flat_day("AAA", D, overrides=ov)}, [sig()])
    assert len(r.trades) == 0 and r.counters.get("cancel_zone_close", 0) == 1


def test_no_entries_from_1500_and_min_stop_widening():
    ov = {idx("15:00"): (100.05, 100.30, 100.00, 100.20)}
    r = run({"AAA": flat_day("AAA", D, overrides=ov)}, [sig(at="14:55", expires="15:30")])
    assert len(r.trades) == 0
    ov = {idx("10:05"): (100.05, 100.30, 100.00, 100.20)}
    r = run({"AAA": flat_day("AAA", D, overrides=ov)}, [sig(stop=100.0, atr_d=2.0)])   # 0.10 stop < 0.2 floor
    tr = r.trades[0]
    # widened stop 99.90 -> 0.20 risk wants 2,500 sh ($250k) -> capped at 1x equity: floor(100000/100.10) = 999
    assert r.counters["stop_widened"] == 1 and tr.entry.qty == math.floor(100_000 / 100.10) == 999
    assert r.meta[0]["risk_usd"] == pytest.approx(999 * 0.20) and r.meta[0]["capped"]


def test_caps_concurrency_entries_and_notional():
    syms = [f"S{i}" for i in range(6)]
    frames, sigs = {}, []
    for k, s in enumerate(syms):
        frames[s] = flat_day(s, D, overrides={idx("10:05"): (100.05, 100.30, 100.00, 100.20)})
        sigs.append(sig(symbol=s, score=0.9 - 0.1 * k))
    r = run(frames, sigs)
    assert len(r.trades) == 4 and r.counters["skip_concurrency"] == 2          # v1.1: 4 concurrent
    assert sorted(t.entry.symbol for t in r.trades) == ["S0", "S1", "S2", "S3"]  # higher zone score first
    # notional cap: a 0.05 stop wants 10,000 shares ($1M) -> capped at 1x equity = 999 shares
    r = run({"AAA": flat_day("AAA", D, overrides={idx("10:05"): (100.05, 100.30, 100.00, 100.20)})},
            [sig(stop=100.05, atr_d=0.1)])
    assert r.trades[0].entry.qty == 999 and r.counters["cap_limited"] == 1


def test_daily_loss_stop_flattens_and_halts():
    frames, sigs = {}, []
    for k, s in enumerate(["A1", "A2", "A3"]):
        ov = {idx("10:05"): (100.05, 100.30, 100.00, 100.20), idx("10:10"): (99.60, 99.62, 99.55, 99.58)}
        frames[s] = flat_day(s, D, overrides=ov, px=100.0)
        for j in range(idx("10:15"), 78):
            frames[s].loc[j, ["open", "high", "low", "close"]] = [99.58, 99.60, 99.55, 99.58]
        sigs.append(sig(symbol=s, stop=99.0))   # 1.10 risk -> 454 sh; open loss ~-0.52*454*3 + costs > 1.5%? no
    sigs.append(sig(symbol="B1", at="11:00", expires="11:30"))
    frames["B1"] = flat_day("B1", D, overrides={idx("11:05"): (100.05, 100.30, 100.00, 100.20)})
    r = run(frames, sigs, risk=RiskCfg(daily_loss_stop=0.002))   # tight stop so the fixture trips it
    assert r.counters.get("halt_daily_loss_stop") == 1
    assert {t.exit.reason for t in r.trades} == {"daily_stop"}
    assert all(t.exit.ts.strftime("%H:%M") == "10:15" for t in r.trades)   # flattened at the next open
    assert "B1" not in {t.entry.symbol for t in r.trades}


def test_cost_tiers_and_min_per_share():
    assert K.tier("SPY", 0.0) == "T0" and K.tier("AAPL", 5e9) == "T1" and K.tier("XYZ", 1e9) == "T2"
    # $20 stock, T0 1bp -> 0.002/share < $0.01 min
    assert K.fill_cost(K.ENTRY, 1, 1000, 20.0, 1.0, "T0") == pytest.approx(10.0)
    # adjusted price 50, adj_factor 4 -> as-traded $200, 250 as-traded shares for 1000 adjusted shares
    assert K.fill_cost(K.ENTRY, 1, 1000, 50.0, 4.0, "T1") == pytest.approx(250 * 2e-4 * 200)
    assert K.fill_cost(K.STOP, -1, 100, 100.0, 1.0, "T2", CostCfg(mult=1.5)) == pytest.approx(
        1.5 * 2 * 100 * 0.035 + 0.3e-4 * 10_000)


def test_bar_rules_pure():
    assert stop_entry_fill(1, 10.0, 10.2, 10.3, 10.1) == 10.2
    assert stop_entry_fill(-1, 10.0, 9.8, 9.9, 9.7) == 9.8
    assert stop_entry_fill(1, 10.0, 9.8, 9.99, 9.7) is None
    h = exit_on_bar(1, 9.0, 11.0, 11.5, 11.6, 11.4, 0.01, False)
    assert h.kind == "target" and h.gap and h.price == 11.5


def _loser_day(day, syms_times):
    """Each (sym, hhmm) gets a long that fills on the next bar and stops out on the bar after."""
    frames, sigs = {}, []
    for sym, at in syms_times:
        i = idx(at)
        ov = {i: (100.05, 100.30, 100.00, 100.20), i + 1: (100.20, 100.25, 99.40, 99.45)}
        frames[sym] = flat_day(sym, day, overrides=ov)
        h, m = divmod(570 + 5 * i, 60)
        sigs.append(sig(sym, day, f"{h:02d}:{m:02d}", expires="15:00"))
    return frames, sigs


def test_guardrail_d2_blocks_entries_after_two_losers():
    from research.intraday_sr.harness.config import GUARDRAILS
    g = {x.name: x for x in GUARDRAILS}
    frames, sigs = _loser_day(D, [("AAA", "10:00"), ("BBB", "10:30"), ("CCC", "11:00")])
    src = FrameBarSource(frames)
    tf = lambda s, d: "T2"
    base = simulate(sigs, src, tier_fn=tf, guardrail=g["none"])
    d2 = simulate(sigs, src, tier_fn=tf, guardrail=g["d2"])
    assert len(base.trades) == 3 and all(t.pnl < 0 for t in base.trades)
    assert len(d2.trades) == 2 and d2.counters["skip_guardrail_day"] == 1
    assert d2.counters["guardrail_day_triggers"] == 1


def test_guardrail_w5_carries_across_days_and_resets_weekly():
    from research.intraday_sr.harness.config import GUARDRAILS
    g = {x.name: x for x in GUARDRAILS}
    frames_by_day, sigs = {}, []
    days = ["2024-03-04", "2024-03-05", "2024-03-06", "2024-03-11"]     # Mon, Tue, Wed, next Mon
    allframes = {}
    for day in days:
        f, s = _loser_day(day, [("AAA", "10:00"), ("BBB", "10:30"), ("CCC", "11:00")] if day == "2024-03-06"
                          else [("AAA", "10:00"), ("BBB", "10:30")])
        for k, v in f.items():
            allframes.setdefault(k, []).append(v)
        sigs += s
    import pandas as pd
    src = FrameBarSource({k: pd.concat(v, ignore_index=True) for k, v in allframes.items()})
    tf = lambda s, d: "T2"
    w5 = simulate(sigs, src, tier_fn=tf, guardrail=g["w5"])
    base = simulate(sigs, src, tier_fn=tf, guardrail=g["none"])
    assert len(base.trades) == 9
    # Mon 2 + Tue 2 losers; Wed AAA is the 5th -> Wed BBB and CCC blocked; next Monday resets (2 trades)
    assert len(w5.trades) == 7
    assert w5.counters["skip_guardrail_week"] == 2 and w5.counters["guardrail_week_triggers"] == 1
    assert sum(t.entry.ts.date().isoformat() == "2024-03-11" for t in w5.trades) == 2
