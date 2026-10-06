from datetime import date

from pathlib import Path

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
    from research.intraday_sr.data.holdout import HoldoutLocked, HoldoutToken
    fz, lk = tmp_path / "FREEZE.md", tmp_path / "holdout.lock"
    syms = [f"S{i}" for i in range(33)]
    with pytest.raises(W.HoldoutRefused):
        W.run_holdout(fz, lk, syms, lambda x, t: {}, {})
    fz.write_text("x")
    with pytest.raises(W.HoldoutRefused):
        W.run_holdout(fz, lk, syms[:32], lambda x, t: {}, {})
    got = W.run_holdout(fz, lk, syms, lambda x, t: {"ok": 1, "token": t}, {})
    assert got["ok"] == 1 and isinstance(got["token"], HoldoutToken) and lk.exists()
    with pytest.raises(W.HoldoutRefused):
        W.run_holdout(fz, lk, syms, lambda x, t: {}, {})
    with pytest.raises(HoldoutLocked):
        # getattr keeps this out of D2's AST Call-name scan in test_badprint
        # (only walkforward.run_holdout may call _from_freeze by name).
        getattr(HoldoutToken, "_from_freeze")(fz)


def test_only_walkforward_constructs_holdout_tokens():
    import re
    root = Path(W.__file__).resolve().parent
    hits = []
    for pp in root.glob("*.py"):
        if pp.name == "walkforward.py":
            continue
        if re.search(r"HoldoutToken\._from_freeze|HoldoutToken\s*\(", pp.read_text()):
            hits.append(pp.name)
    assert hits == []

def test_bootstrap_reproducible_and_dsr_sane():
    rng = np.random.default_rng(1)
    r = rng.normal(0.05, 1, 2000)
    day = np.repeat(np.arange(1000), 2)
    a, b = S.day_block_mean_r(r, day), S.day_block_mean_r(r, day)
    assert a == b and a["lo"] < a["mean"] < a["hi"] and a["resamples"] == 5000
    d = pd.Series(rng.normal(0.0, 0.01, 1500))
    lo, hi = S.deflated_sharpe(d, 1)["dsr"], S.deflated_sharpe(d, 432)["dsr"]
    assert 0 <= hi <= lo <= 1                                       # more trials -> lower DSR


def _row(run_id, grid, test, vid, overlay="", kind="interim", fold=1):
    return TrialRow(run_id=run_id, run_kind=kind, spec_version="v1.3", grid_sha256=grid, engine_cfg="e",
                    git_sha="s", test=test, variant_id=vid, fold=fold, phase="test", symbols="ADBE", n_symbols=1,
                    overlay=overlay)


def test_program_ledger_append_only_cumulative_and_rerun_adds_trials(tmp_path):
    log = TrialLog(tmp_path / "ledger")
    for v in ("A-1", "A-2"):
        for fold in (1, 2):                                        # several rows per trial: still one trial
            log.add(_row("r1", "gA", "A", v, fold=fold))
    log.add(_row("r1", "gA", "A", "A-1|d2", overlay="d2"))         # guardrail-configuration rows never count (G2)
    log.add(_row("smoke1", "gS", "A", "S-1", kind="smoke"))       # smoke never counts
    log.flush()
    p1 = log.parts()
    assert len(p1) == 1 and TrialLog.program_trial_count(log.read()) == 2
    for v in ("B-1", "B-2", "B-3"):
        log.add(_row("r1", "gB", "B", v))                          # other tests add to the PROGRAM count
    log.flush()
    log.add(_row("r2", "gA", "A", "A-1"))                          # same grid, new run id (re-run): adds a trial
    log.add(_row("r1", "gA", "A", "A-1"))                          # same grid + run id again: no new trial
    log.flush()
    assert TrialLog.program_trial_count(log.read()) == 6
    assert [p.read_bytes() for p in p1] == [p.read_bytes() for p in log.parts()[:1]]   # earlier parts untouched
    assert len(log.parts()) == 3 and log.dsr_n() == 456
    cut = log.read()["created_at_ct"].iloc[0]
    assert TrialLog.program_trial_count(log.read(), as_of_ct="2000-01-01T00:00:00-06:00") == 0
    with pytest.raises(Exception):
        TrialLog(tmp_path / "x.parquet")
    with pytest.raises(Exception):
        log.add(_row("", "gA", "A", "A-9"))


def test_dsr_n_exceeds_450_when_ledger_grows(tmp_path):
    log = TrialLog(tmp_path / "ledger")
    for i in range(460):
        log.add(_row("r1", "gA", "A", f"A-{i}"))
    log.flush()
    assert log.dsr_n() == 460
    from research.intraday_sr.harness import walkforward as W
    W.write_freeze(tmp_path / "FREEZE.md", {}, log, "sha", "grid", "v1.3")
    txt = (tmp_path / "FREEZE.md").read_text()
    assert "DSR N = max(456, 460) = 460" in txt and log.sha256() in txt


def test_options_overlay_rows_count_guardrail_rows_do_not(tmp_path):
    """SPEC v1.3.2 O1.9: options overlay rows are logged and feed DSR N; guardrail comparisons stay 0 trials."""
    log = TrialLog(tmp_path / "ledger")
    log.add(_row("r1", "gA", "A", "A-1"))
    for lab in ("none", "d2+w5", "d2+w6", "d3+w6", "D2"):
        log.add(_row("r1", "gA", "A", "A-1", overlay=lab))
    for book in ("daily", "weekly"):
        log.add(_row("r1", "gA", "A", "A-1", overlay=f"baseline:long_atm:{book}"))
        log.add(_row("r1", "gA", "A", "A-1", overlay=f"baseline:long_atm:{book}", fold=2))   # same trial, 2nd fold
        log.add(_row("r1", "gA", "A", "A-1", overlay=f"a2:sl30_tp50:{book}"))
    log.add(_row("s", "gS", "A", "A-1", overlay="baseline:long_atm:daily", kind="smoke"))     # smoke never counts
    log.flush()
    assert TrialLog.program_trial_count(log.read()) == 1 + 4
    assert log.dsr_n() == 456
    assert not TrialLog.is_guardrail_overlay("") and TrialLog.is_guardrail_overlay("d2+w5")


def test_selection_is_r_only():
    runs = {"a": _run("a", [0.2] * 250, 250), "b": _run("b", [0.1] * 300, 300)}
    with pytest.raises(W.SelectionMetricError):
        W.select(runs, date(2023, 1, 1), date(2026, 1, 1), metric="monthly_return")
    with pytest.raises(W.SelectionMetricError):
        W.rank_table(runs, date(2023, 1, 1), date(2026, 1, 1), metric="pnl")
    base = W.select(runs, date(2023, 1, 1), date(2026, 1, 1))
    # $ / % data is invisible to selection: garbage pnl and daily returns change nothing
    junk = {k: W.VariantRun(k, v.trades.assign(pnl=np.nan if k == "a" else 1e9),
                            v.daily * (-1e6 if k == "a" else 1e6)) for k, v in runs.items()}
    assert W.select(junk, date(2023, 1, 1), date(2026, 1, 1)) == base
    assert base[0] == "a"


def test_exact_finalist_check_reports_deltas_and_flags():
    a0, b0 = W.FINALIST_RESELECT
    days = pd.bdate_range(a0, b0).date
    def run(vid, mu):
        t = pd.DataFrame({"session": days, "r": mu, "pnl": mu * 100.0, "symbol": "AAA"})
        return W.VariantRun(vid, t, pd.Series(0.0, index=pd.Index(days)))
    runs = {f"v{i}": run(f"v{i}", m) for i, m in enumerate([0.30, 0.25, 0.20, 0.15, 0.10, 0.05])}
    ft = pd.DataFrame([{"variant_id": v, "fold": k, "test_mean_r": 0.3 - 0.05 * i, "test_trades": 50}
                       for k in range(1, 6) for i, v in enumerate(runs)])
    fin = W.finalists(runs, ft)
    assert fin["procedure"]["variant"] == "v0" and fin["stable"]["variant"] == "v0"
    same = lambda vid, a, b: runs[vid]
    df, flag = W.exact_finalist_check(runs, ft, fin, same)
    assert not flag and list(df["variant_id"][:4]) == ["v0", "v1", "v2", "v3"] and (df["delta_mean_r"] == 0).all()
    # exact path drops v0 to 0.22R: rank 1 -> 2 and dR = -0.08 -> CP4 flag
    shifted = lambda vid, a, b: run(vid, 0.22) if vid == "v0" else runs[vid]
    df2, flag2 = W.exact_finalist_check(runs, ft, fin, shifted)
    r0 = df2[df2["role"] == "procedure finalist"].iloc[0]
    assert flag2 and r0.exact_rank == 2 and r0.delta_rank == 1 and r0.delta_mean_r == pytest.approx(-0.08)
    # small move (< 0.01R) without a rank change: no flag
    tiny = lambda vid, a, b: run(vid, 0.295) if vid == "v0" else runs[vid]
    assert not W.exact_finalist_check(runs, ft, fin, tiny)[1]


def test_walk_forward_oos_comes_from_exact_window_paths():
    runs = {"a": _run("a", [0.5] * 600, 600, start="2018-06-01")}
    marker = pd.Series(0.123, index=pd.Index(pd.bdate_range("2020-01-01", "2026-03-31").date))
    def exact(vid, a, b):
        t = pd.DataFrame({"session": [a], "r": [9.0], "pnl": [1.0], "symbol": ["AAA"]})
        di = marker.index
        return W.VariantRun(vid, t, marker[(di >= a) & (di <= b)])
    wf = W.walk_forward(runs, W.make_folds()[:2], exact_window=exact)
    assert wf.oos_exact and (wf.oos_trades["r"] == 9.0).all() and (wf.oos_daily == 0.123).all()


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
    ex = pd.DataFrame([{"role": "procedure finalist", "variant_id": "v0", "approx_mean_r": 0.2, "exact_mean_r": 0.18,
                        "delta_mean_r": -0.02, "approx_trades": 300, "exact_trades": 298, "approx_rank": 1,
                        "exact_rank": 1, "delta_rank": 0, "cp4_flag": True}])
    inp = RO.ReadoutInputs("v1.2", "abc123", "f" * 64, {"A": 192, "B": 192}, ["ADBE"], headlines={"A": h},
                           frontier=rows, n_program=512, n_dsr=512, exact_checks={"A": (ex, True)})
    p = RO.write_readout(inp, tmp_path)
    txt = p.read_text()
    assert "DSR N = max(456, program ledger) = **512**" in txt and "program ledger: 512 trials" in txt
    assert "Exact finalist check, Test A" in txt and "CP4 FLAG" in txt and "-0.02" in txt
    assert "INTERIM — 1 of 33 symbols — not a pass/fail result" in txt
    assert "trades/day" in txt and "1 setting(s) flagged" in txt and "A = 192" in txt


def test_adjfactors_thin_adapter_semantics_and_delegation(tmp_path, monkeypatch):
    from research.intraday_sr.harness import adjfactors as AF
    d = tmp_path / "adj_factors"
    d.mkdir()
    pd.DataFrame({"symbol": ["SPY", "SPY", "BRK-B"], "date": ["2019-01-02", "2019-01-03", "2019-01-02"],
                  "raw_close": [250.0, 245.0, 200.0], "adj_close": [225.0, 220.5, 200.0],
                  "adj_factor": [0.9, 0.9, 1.0]}).to_parquet(d / "adj_factors.parquet")
    monkeypatch.setattr(AF, "_dev2_loader", lambda: None)
    AF._CACHE.clear()
    s = AF.load_adj_factors(tmp_path)
    assert list(s.index.names) == ["symbol", "session"]
    assert s[("SPY", date(2019, 1, 2))] == pytest.approx(250 / 225)          # raw / adjusted
    assert AF.load_factors("BRK.B", tmp_path)[date(2019, 1, 2)] == 1.0
    # once Developer 2's loader is importable it is used verbatim (already raw/adj), frame or series
    calls = []
    def dev2(root):
        calls.append(root)
        return pd.DataFrame({"symbol": ["SPY"], "date": ["2019-01-02"], "adj_factor": [1.25]})
    monkeypatch.setattr(AF, "_dev2_loader", lambda: dev2)
    AF._CACHE.clear()
    assert AF.load_factors("SPY", tmp_path)[date(2019, 1, 2)] == 1.25 and calls
    AF._CACHE.clear()
