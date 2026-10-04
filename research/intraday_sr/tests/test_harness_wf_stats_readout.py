from datetime import date

import numpy as np
import pandas as pd
import pytest

from research.intraday_sr.harness import readout as RO
from research.intraday_sr.harness import stats as S
from research.intraday_sr.harness import walkforward as W
from research.intraday_sr.harness.triallog import SCHEMA, TrialLog, TrialRow


def test_folds_match_spec():
    f = W.make_folds()
    assert len(f) == 25
    assert (f[0].train_start, f[0].train_end, f[0].test_start, f[0].test_end) == (
        date(2019, 1, 2), date(2019, 12, 31), date(2020, 1, 1), date(2020, 3, 31))
    assert (f[-1].train_start, f[-1].train_end, f[-1].test_start, f[-1].test_end) == (
        date(2025, 1, 1), date(2025, 12, 31), date(2026, 1, 1), date(2026, 3, 31))
    assert all(x.test_end < date(2026, 4, 1) for x in f)          # holdout never inside a fold


def _run(vid, r, n, start="2024-01-02"):
    days = pd.bdate_range(start, periods=n).date
    t = pd.DataFrame({"session": days, "r": r, "pnl": np.array(r) * 100.0, "symbol": "AAA"})
    return W.VariantRun(vid, t, pd.Series(np.array(r) * 0.001, index=pd.Index(days)))


def test_select_min_trades_and_ties():
    runs = {"a": _run("a", [0.5] * 199, 199), "b": _run("b", [0.1] * 300, 300), "c": _run("c", [0.1] * 250, 250)}
    vid, info = W.select(runs, date(2023, 1, 1), date(2026, 1, 1))
    assert vid == "b" and info["eligible"] == 2                    # a has < 200 trades; b beats c on trade count


def test_holdout_refusals(tmp_path):
    fz, lk = tmp_path / "FREEZE.md", tmp_path / "holdout.lock"
    syms = [f"S{i}" for i in range(33)]
    with pytest.raises(W.HoldoutRefused):
        W.run_holdout(fz, lk, syms, lambda x: {}, {})
    fz.write_text("x")
    with pytest.raises(W.HoldoutRefused):
        W.run_holdout(fz, lk, syms[:32], lambda x: {}, {})
    assert W.run_holdout(fz, lk, syms, lambda x: {"ok": 1}, {}) == {"ok": 1}
    with pytest.raises(W.HoldoutRefused):
        W.run_holdout(fz, lk, syms, lambda x: {}, {})


def test_bootstrap_reproducible_and_dsr_sane():
    rng = np.random.default_rng(1)
    r = rng.normal(0.05, 1, 2000)
    day = np.repeat(np.arange(1000), 2)
    a, b = S.day_block_mean_r(r, day), S.day_block_mean_r(r, day)
    assert a == b and a["lo"] < a["mean"] < a["hi"] and a["resamples"] == 5000
    d = pd.Series(rng.normal(0.0, 0.01, 1500))
    lo, hi = S.deflated_sharpe(d, 1)["dsr"], S.deflated_sharpe(d, 432)["dsr"]
    assert 0 <= hi <= lo <= 1                                       # more trials -> lower DSR


def test_triallog_overlays_excluded_from_n(tmp_path):
    log = TrialLog(tmp_path / "triallog.parquet")
    base = dict(run_id="r", run_kind="interim", spec_version="v1.2", grid_sha256="g", engine_cfg="e", git_sha="s",
                fold=1, phase="test", symbols="ADBE", n_symbols=1)
    for v in ("A-1", "A-2"):
        log.add(TrialRow(test="A", variant_id=v, **{k: v2 for k, v2 in base.items() if k in SCHEMA}))
    log.add(TrialRow(test="A", variant_id="A-1|d2", overlay="d2", **{k: v2 for k, v2 in base.items() if k in SCHEMA}))
    df = log.flush()
    assert TrialLog.trial_count(df, "A") == 2 and TrialLog.trial_count(df, "A", True) == 3


def test_readout_renders_interim_with_frontier_flag(tmp_path):
    rng = np.random.default_rng(3)
    days = pd.bdate_range("2020-01-02", "2026-03-31").date
    rows = []
    for k, (n_per_day, mu) in enumerate([(10, 0.2), (2, 0.0), (9, -0.1)]):
        sess = np.repeat(days, n_per_day)
        t = pd.DataFrame({"session": sess, "r": rng.normal(mu, 1, sess.size)})
        t["pnl"] = t["r"] * 500
        daily = t.groupby("session")["pnl"].sum() / 100_000
        rows.append(RO.frontier_row("A", f"v{k}", t, daily))
    assert rows[0]["flag_150_250_lbR_gt0"] and not rows[1]["flag_150_250_lbR_gt0"] and not rows[2]["flag_150_250_lbR_gt0"]
    t0 = pd.DataFrame({"session": np.repeat(days, 10), "r": rng.normal(0.2, 1, days.size * 10)})
    t0["pnl"] = t0["r"] * 500
    d0 = t0.groupby("session")["pnl"].sum() / 100_000
    h = RO.headline(t0, d0, 192)
    inp = RO.ReadoutInputs("v1.2", "abc123", "f" * 64, {"A": 192, "B": 192}, ["ADBE"], headlines={"A": h},
                           frontier=rows)
    p = RO.write_readout(inp, tmp_path)
    txt = p.read_text()
    assert "INTERIM — 1 of 33 symbols — not a pass/fail result" in txt
    assert "trades/day" in txt and "1 setting(s) flagged" in txt and "A = 192" in txt
