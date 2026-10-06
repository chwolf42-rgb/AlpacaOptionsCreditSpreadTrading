"""A1b: legacy switches reproduce the pre-A1b rules exactly; half-day entry cutoff; INTERIM readout with errored
variants; per-trade file; legacy runs never touch the PROGRAM ledger and are marked NOT FOR CP4."""

import json
import random
from pathlib import Path

import pandas as pd
import pytest

from research.intraday_sr.harness import portfolio as P
from research.intraday_sr.harness import readout as RO
from research.intraday_sr.harness import run as R
from research.intraday_sr.harness.config import CostCfg, RiskCfg
from research.intraday_sr.harness.fills import STOP, TARGET, ExitHit, exit_on_bar
from research.intraday_sr.harness.portfolio import FrameBarSource, simulate
from research.intraday_sr.tests._harness_stubs import flat_day, idx, sig
from research.intraday_sr.tests.test_harness_fills_gap import _bar, _near


@pytest.fixture(autouse=True)
def _reset_legacy():
    P.set_legacy(False, False)
    yield
    P.set_legacy(False, False)


def _old_exit_on_bar(direction, stop, target, o, h, l, tick, entry_bar):
    """Verbatim pre-A1b fills.exit_on_bar (7d60771)."""
    d = direction
    if not entry_bar:
        if d * (o - stop) <= 0:
            return ExitHit(o, STOP, True, False)
        if d * (o - target) >= tick:
            return ExitHit(o, TARGET, True, False)
    if d > 0:
        hit_stop = l <= stop
        hit_tgt = h >= target + tick
    else:
        hit_stop = h >= stop
        hit_tgt = l <= target - tick
    if hit_stop:
        return ExitHit(stop, STOP, False, bool(hit_tgt))
    if hit_tgt and not entry_bar:
        return ExitHit(target, TARGET, False, False)
    return None


def test_legacy_target_gap_reproduces_old_exit_on_bar():
    rng = random.Random(42)
    differ = 0
    for _ in range(30_000):
        tick = 0.01 / rng.choice([1.0, rng.uniform(0.5, 1.5)])
        o, h, l = _bar(rng, rng.uniform(20.0, 500.0))
        d, eb = rng.choice([1, -1]), rng.random() < 0.5
        stop, target = _near(rng, o, h, l, tick), _near(rng, o, h, l, tick)
        old = _old_exit_on_bar(d, stop, target, o, h, l, tick, eb)
        assert exit_on_bar(d, stop, target, o, h, l, tick, eb, legacy_target_gap=True) == old
        differ += exit_on_bar(d, stop, target, o, h, l, tick, eb) != old
    assert differ > 0                                         # the default (fixed) rule is really different


D_HALF, D_FULL = "2024-11-29", "2024-03-04"                  # half day (13:00 close) / normal day


def _sim(frames, sigs):
    return simulate(sigs, FrameBarSource(frames), RiskCfg(), CostCfg(), tier_fn=lambda s, d: "T2")


def test_halfday_no_entry_at_or_after_forced_exit_bar():
    n_half = idx("12:55") + 1                                 # last bar 12:55 -> half-day forced exit 12:55
    ov = {idx("12:55"): (100.05, 100.30, 100.00, 100.20)}     # trades through the 100.10 trigger
    frames = {"AAA": flat_day("AAA", D_HALF, n=n_half, overrides=ov)}
    s = [sig(day=D_HALF, at="12:50", expires="13:00")]
    assert _sim(frames, s).trades == []                       # A1b: no entry fill on the forced-exit bar
    P.set_legacy(halfday=True)
    tr = _sim(frames, s).trades                               # legacy: the 12:55 entry happens
    assert len(tr) == 1 and tr[0].entry.ts.strftime("%H:%M") == "12:55"


def test_halfday_entry_before_forced_bar_and_normal_day_unchanged():
    n_half = idx("12:55") + 1
    ov = {idx("12:50"): (100.05, 100.30, 100.00, 100.20)}
    tr = _sim({"AAA": flat_day("AAA", D_HALF, n=n_half, overrides=ov)}, [sig(day=D_HALF, at="12:45", expires="13:00")]).trades
    assert len(tr) == 1 and tr[0].entry.ts.strftime("%H:%M") == "12:50" and tr[0].exit.reason == "forced_eod"
    ov = {idx("14:55"): (100.05, 100.30, 100.00, 100.20)}     # normal day: 14:55 < 15:00 last_entry still fills
    for legacy in (False, True):
        P.set_legacy(halfday=legacy)
        tr = _sim({"AAA": flat_day("AAA", D_FULL, overrides=ov)}, [sig(day=D_FULL, at="14:50", expires="15:30")]).trades
        assert len(tr) == 1 and tr[0].entry.ts.strftime("%H:%M") == "14:55"


def test_readout_interim_at_33_of_33_lists_errored_variants(tmp_path):
    syms = [f"S{i:02d}" for i in range(33)]
    er = pd.DataFrame([{"variant_id": "A-x1", "error": "FillOutsideBar('target fill 1 outside bar [2, 3]')"}])
    inp = RO.ReadoutInputs("v1.3.1", "abc", "f" * 64, {"A": 192}, syms, errored={"A": er})
    txt = RO.write_readout(inp, tmp_path).read_text()
    assert "INTERIM — 33 of 33 symbols — not a pass/fail result" in txt and "informational only" in txt
    assert "errored variants (1)" in txt and "A-x1" in txt and "FillOutsideBar" in txt
    assert "NOT FOR CP4" not in txt
    inp = RO.ReadoutInputs("v1.3.1", "abc", "f" * 64, {"A": 192}, syms, not_for_cp4=R.NOT_FOR_CP4)
    assert "NOT FOR CP4/SELECTION" in RO.write_readout(inp, tmp_path / "b").read_text()


def test_ledger_dir_attribution_never_program_ledger(tmp_path):
    prog = tmp_path / "PROGRAM_LEDGER"
    assert R.ledger_dir(False, False, tmp_path / "run", prog) == prog
    assert R.ledger_dir(False, True, tmp_path / "run", prog) == tmp_path / "run" / "ledger_attribution"
    assert R.ledger_dir(True, True, tmp_path / "run", prog) == tmp_path / "run" / "ledger_smoke"
    with pytest.raises(SystemExit):
        R.ledger_dir(False, True, prog.parent, prog.parent / "ledger_attribution")


@pytest.mark.parametrize("legacy", [[], ["--legacy-target-gap", "--legacy-halfday"]])
def test_run_writes_trades_file_and_records_legacy_flags(tmp_path, monkeypatch, legacy):
    from research.intraday_sr.tests.test_harness_run_chunked import FixtureAdapter
    ad = FixtureAdapter()
    monkeypatch.setattr(R, "load_adapter", lambda spec, _ad=ad: _ad)
    R.main(["--tests", "A", "--tag", "tf", "--out", str(tmp_path), "--workers", "1", *legacy])
    out = tmp_path / "tf"
    m = json.loads((out / "manifest.json").read_text())
    assert m["legacy_target_gap"] is bool(legacy) and m["legacy_halfday"] is bool(legacy)
    txt = (out / "READOUT.md").read_text()
    if legacy:
        assert m["run_kind"] == "attribution" and "NOT FOR CP4/SELECTION" in m["not_for_cp4"]
        assert "NOT FOR CP4/SELECTION" in txt
    else:
        assert "not_for_cp4" not in m and "NOT FOR CP4" not in txt
    assert P.LEGACY == {"target_gap": bool(legacy), "halfday": bool(legacy)}
    t = pd.read_parquet(out / "trades_A.parquet")
    assert m["trades_files"]["A"] == {"file": "trades_A.parquet", "rows": len(t)}
    picked = [p for p in json.loads((out / "wf_A.json").read_text())["picks"] if p["variant"]]
    assert {"dev", "oos_exact"} <= set(t["path"]) <= {"dev", "oos_exact", "selected"}
    assert ("selected" in set(t["path"])) == bool(picked) or not picked
    assert set(t.loc[t["path"] == "dev", "variant_id"]) == {v["variant_id"] for v in ad.variants("A")}
    assert (t.loc[t["path"] == "oos_exact", "fold"] >= 1).all() and t["exit_price"].notna().all()
    assert {"entry_price", "exit_price", "qty", "entry_cost", "exit_cost", "direction"} <= set(t.columns)


def test_trades_file_keeps_selected_path_fold_and_variant(tmp_path):
    ov = {idx("10:05"): (100.05, 100.30, 100.00, 100.20), idx("10:10"): (100.20, 100.72, 100.15, 100.60)}
    res = _sim({"AAA": flat_day("AAA", D_FULL, overrides=ov)}, [sig()])
    df = R._trades_df(res)
    assert len(df) == 1 and df["exit_price"].iloc[0] == pytest.approx(100.70)
    tf = R.TradesFile(tmp_path / "t.parquet")
    tf.add(df, path="dev", variant_id="A-1", guardrail="d2+w5")
    tf.add(df.assign(fold=7, variant_id="A-2"), path="selected", guardrail="none")
    tf.add(df.iloc[:0], path="oos_exact", variant_id="A-3", guardrail="d2+w5")
    tf.close()
    t = pd.read_parquet(tmp_path / "t.parquet")
    assert tf.rows == 2 and list(t["path"]) == ["dev", "selected"]
    assert list(t["variant_id"]) == ["A-1", "A-2"] and list(t["fold"]) == [-1, 7]
    assert list(t["guardrail"]) == ["d2+w5", "none"]
