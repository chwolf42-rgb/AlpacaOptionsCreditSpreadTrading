"""Run driver: grid -> per-variant portfolio simulation -> walk-forward -> trial log -> readout.

    python -m research.intraday_sr.harness.run --tests A --symbols available --tag interim1 --workers 6

The engine side is reached through an *adapter* (default: the S0 modules once they land). Any adapter must provide:
    symbols() -> list[str]                    complete symbols available locally
    bar_source(symbols) -> object with session_frame(sym, date) and daily(sym)   (5m execution bars)
    sessions() -> list[date]                  development-window sessions (<= 2026-03-31)
    variants(test) -> list[dict]              each with 'variant_id' and 'target' in {1R, 2R, zone}
    signals(symbol, variant) -> Iterable[Signal]
    engine_cfg(variant) -> str
    smoke: bool                               True for throwaway stand-ins (labels the readout, run_kind=smoke)
The holdout is never touched here (see walkforward.run_holdout).
"""
from __future__ import annotations

import argparse
import importlib
import json
import multiprocessing as mp
import os
import resource
import sys
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from research.intraday_sr.harness import adjfactors as AF
from research.intraday_sr.harness import readout as RO
from research.intraday_sr.harness import stats as S
from research.intraday_sr.harness.compare import write_compare
from research.intraday_sr.harness.config import COMPARISON, EXTRA_COMPARISON, PRIMARY, CostCfg, RiskCfg
from research.intraday_sr.harness.grid_check import check_test_grid, grid_hash
from research.intraday_sr.harness.portfolio import simulate
from research.intraday_sr.harness.triallog import TrialLog, TrialRow, git_sha
from research.intraday_sr.harness.walkforward import DEV_END, VariantRun, make_folds, walk_forward

SPEC_VERSION = "v1.3"
_G: dict = {}          # fork-shared state for workers


class S0Adapter:
    """Default adapter over Developer 2's S0 modules (types.py, grids.py, data/, engine/). Wired once S0 lands."""
    smoke = False

    def __init__(self):
        try:
            self.grids = importlib.import_module("research.intraday_sr.grids")
            self.engine = importlib.import_module("research.intraday_sr.engine.signals")
            self.cache = importlib.import_module("research.intraday_sr.data.cache")
        except ImportError as e:
            raise SystemExit(f"S0 engine/grids not available yet ({e}). Pass --adapter module:factory for a smoke run.")
        raise SystemExit("S0 modules found: wire S0Adapter to their final API (variants/signals/bar_source).")


def load_adapter(spec: str | None):
    if not spec:
        return S0Adapter()
    mod, _, attr = spec.partition(":")
    if mod.endswith(".py"):
        sys.path.insert(0, str(Path(mod).parent))
        mod = Path(mod).stem
    return getattr(importlib.import_module(mod), attr or "make_adapter")()


def _trades_df(res) -> pd.DataFrame:
    if not res.trades:
        return pd.DataFrame(columns=["session", "symbol", "r", "pnl", "exit_kind", "capped"])
    m = pd.DataFrame(res.meta)
    return pd.DataFrame({"session": m["session"], "symbol": m["symbol"], "r": [t.r for t in res.trades],
                         "pnl": [t.pnl for t in res.trades], "exit_kind": m["exit_kind"], "capped": m["capped"],
                         "entry_ts": m["entry_ts"], "exit_ts": m["exit_ts"],
                         "day_losses_before": m["day_losses_before"], "week_losses_before": m["week_losses_before"]})


def run_variant(variant: dict, guardrail=PRIMARY, cost_mult: float = 1.0, keep_raw: bool = False):
    ad, symbols, src, sessions = _G["adapter"], _G["symbols"], _G["src"], _G["sessions"]
    try:
        sigs = [s for sym in symbols for s in ad.signals(sym, variant)]
        risk = guardrail.apply(RiskCfg(target=variant["target"]))
        res = simulate(sigs, src, risk, CostCfg(mult=cost_mult), sessions=sessions)
        vr = VariantRun(variant["variant_id"], _trades_df(res), res.daily, config=guardrail.name)
        return (vr, res.counters, res if keep_raw else None)
    except Exception as e:                                   # failures are logged trials too
        return (VariantRun(variant["variant_id"], pd.DataFrame(), pd.Series(dtype=float), "error", repr(e),
                           config=guardrail.name), {}, None)


def _par(fn, items, workers):
    if workers <= 1:
        return [fn(x) for x in items]
    with mp.get_context("fork").Pool(workers) as pool:
        return pool.map(fn, items, chunksize=1)


def _trial_sr_var(runs) -> float | None:
    """Variance across trials of the (daily) Sharpe of each variant's development path, for the DSR."""
    srs = [S.sharpe(vr.daily) for vr in runs.values() if vr.status == "ok" and len(vr.daily) > 20]
    srs = [x for x in srs if x is not None and np.isfinite(x)]
    return float(np.var(srs, ddof=1)) if len(srs) >= 2 else None


def oos_from_picks(picks, runs) -> tuple[pd.DataFrame, pd.Series]:
    tp, dp = [], []
    for p in picks:
        a, b = (date.fromisoformat(x) for x in p["test"])
        if p["variant"] is None or p["variant"] not in runs:
            continue
        r = runs[p["variant"]]
        s = pd.to_datetime(r.trades["session"]).dt.date if len(r.trades) else None
        if s is not None:
            tp.append(r.trades[(s >= a) & (s <= b)])
        di = pd.to_datetime(r.daily.index).date
        dp.append(r.daily[(di >= a) & (di <= b)])
    return (pd.concat(tp, ignore_index=True) if tp else pd.DataFrame(columns=["session", "r", "pnl", "symbol"]),
            pd.concat(dp) if dp else pd.Series(dtype=float))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", default="A")
    ap.add_argument("--symbols", default="available")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", default="/workspace/research4/runs")
    ap.add_argument("--workers", type=int, default=max(1, min(6, (os.cpu_count() or 2) - 2)))
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--max-variants", type=int, default=0, help="smoke only")
    a = ap.parse_args(argv)
    t0 = time.time()
    ad = load_adapter(a.adapter)
    syms = ad.symbols() if a.symbols == "available" else a.symbols.split(",")
    out = Path(a.out) / a.tag
    out.mkdir(parents=True, exist_ok=True)
    _G.update(adapter=ad, symbols=syms, src=ad.bar_source(syms),
              sessions=[d for d in ad.sessions() if d <= DEV_END])
    src = _G["src"]
    adj_note = getattr(getattr(src, "adj_info", None), "note", lambda: "adj factor provenance unknown")()
    if not ad.smoke:
        # Real runs: Trading's raw/adj factor is REQUIRED for every run symbol and session; no 1.0 fallback.
        sess_by_sym = {s_: [d for d in _G["sessions"] if src.session_frame(s_, d) is not None] for s_ in syms}
        AF.require_coverage(sess_by_sym, getattr(ad, "adj_root", AF.DEFAULT_ROOT))
        info = getattr(src, "adj_info", None)
        if info is None or info.approximate:
            raise SystemExit("bar source carries approximate (1.0) adj factors; real runs need Trading's factors")
    sha = git_sha(Path(__file__).resolve().parents[3])
    kind = "smoke" if ad.smoke else ("interim" if len(syms) < 33 else "full")
    log = TrialLog(out / "triallog.parquet")
    inp = RO.ReadoutInputs(SPEC_VERSION, sha, "", {}, syms, smoke=ad.smoke)
    frontier, notes = [], [f"adapter: {type(ad).__name__}; workers {a.workers}", adj_note]
    for test in a.tests.split(","):
        variants = ad.variants(test)
        if a.max_variants:
            variants = variants[:a.max_variants]
        gsha = grid_hash(variants)
        inp.grid_sha = gsha
        errs = check_test_grid(f"Test {test}", variants) if test in ("A", "B") else []
        if errs and not ad.smoke:
            raise SystemExit("grid does not match SPEC v1.1: " + "; ".join(errs))
        notes += [f"Test {test} grid check: {e}" for e in errs]
        results = _par(run_variant, variants, a.workers)
        runs = {vr.variant_id: vr for vr, _, _ in results}
        wf = walk_forward(runs, make_folds())
        base = dict(run_id=a.tag, run_kind=kind, spec_version=SPEC_VERSION, grid_sha256=gsha, git_sha=sha, test=test,
                    symbols=" ".join(syms), n_symbols=len(syms))
        for v in variants:
            vr = runs[v["variant_id"]]
            if vr.status != "ok":
                log.add(TrialRow(engine_cfg=ad.engine_cfg(v), variant_id=vr.variant_id, fold=-1, phase="all",
                                 status=vr.status, error=vr.error, **base))
        for r in wf.fold_table.itertuples(index=False):
            for ph in ("train", "test"):
                log.add(TrialRow(engine_cfg="", variant_id=r.variant_id, fold=int(r.fold), phase=ph,
                                 trades=int(getattr(r, f"{ph}_trades")), mean_r=float(getattr(r, f"{ph}_mean_r")), **base))
        df = log.flush()
        n = TrialLog.trial_count(df, test)
        inp.trial_counts = {**inp.trial_counts, test: n}
        h = RO.headline(wf.oos_trades if len(wf.oos_trades) else pd.DataFrame(columns=["session", "r", "pnl"]),
                        wf.oos_daily, n, var_sr=_trial_sr_var(runs))
        inp.headlines = {**inp.headlines, test: h}
        inp.picks = {**inp.picks, test: pd.DataFrame([{k: p.get(k) for k in ("fold", "test", "variant", "eligible",
                                                                                "train_mean_r", "train_trades")}
                                                       for p in wf.picks])}
        if len(wf.oos_trades):
            g = wf.oos_trades.groupby("symbol")
            inp.per_symbol = {**inp.per_symbol, test: pd.DataFrame({"trades": g.size(), "mean R": g["r"].mean().round(3),
                                                                    "win": (g["pnl"].apply(lambda s: (s > 0).mean())).round(3)}).reset_index()}
        frontier += [RO.frontier_row(test, vid, vr.trades, vr.daily) for vid, vr in runs.items() if vr.status == "ok"]
        picked = sorted({p["variant"] for p in wf.picks if p["variant"]})
        vmap = {v["variant_id"]: v for v in variants}
        # sensitivity (SPEC section 8): 1.5x costs on the selected path (same picks, primary config)
        r15 = dict(zip(picked, [x[0] for x in _par(lambda v: run_variant(vmap[v], cost_mult=1.5), picked, 1)]))
        t2, d2 = oos_from_picks(wf.picks, r15)
        inp.sensitivities = {**inp.sensitivities, test: pd.DataFrame([
            {"case": "base costs", "mean R": RO._num(h["mean_r_ci"]["mean"]), "monthly": RO._pct(h["monthly_ci"]["mean"])},
            {"case": "1.5x costs", "mean R": RO._num(float(t2["r"].mean()) if len(t2) else None),
             "monthly": RO._pct(float(S.monthly_returns(d2).mean()) if len(d2) else None)}])}
        # G2: comparison configurations AFTER selection, same picks, only RiskCfg changed -> guardrail_compare.parquet
        prim_cnt = {}
        for v in picked:
            for k, val in run_variant(vmap[v])[1].items():
                prim_cnt[k] = prim_cnt.get(k, 0) + val
        gstats = [RO.guardrail_stats(PRIMARY.name, wf.oos_trades if len(wf.oos_trades) else
                                     pd.DataFrame(columns=["session", "r", "pnl"]), wf.oos_daily, prim_cnt)]
        crow = []
        for gcfg in COMPARISON + EXTRA_COMPARISON:
            outs = {v: run_variant(vmap[v], guardrail=gcfg) for v in picked}
            tg, dg = oos_from_picks(wf.picks, {v: o[0] for v, o in outs.items()})
            cnt = {}
            for o in outs.values():
                for k, val in o[1].items():
                    cnt[k] = cnt.get(k, 0) + val
            gs = RO.guardrail_stats(gcfg.name, tg, dg, cnt)
            crow.append({"run_id": a.tag, "test": test, "scope": "selected_path", "config": gcfg.name,
                         "trades": gs["trades"], "trades_per_mo": gs["trades_per_mo"], "win_rate": gs["win_rate"],
                         "mean_r": gs["mean_r"].get("mean"), "monthly_mean": gs["monthly"].get("mean"),
                         "max_dd": gs["max_dd"], "worst_week": gs["worst_week"],
                         "days_halted_day_limit": gs["days_halted_day_limit"],
                         "weeks_halted_week_limit": gs["weeks_halted_week_limit"],
                         "daily_stop_days": gs["daily_stop_days"], "signals_blocked": gs["signals_blocked"]})
            if gcfg in COMPARISON:
                gstats.append(gs)
        write_compare(out, crow)
        inp.guardrails = {**inp.guardrails, f"Test {test} selected path":
                          (RO.guardrail_rows(gstats), RO.trades_per_month_words(gstats))}
        (out / f"wf_{test}.json").write_text(json.dumps({"picks": wf.picks, "finalists": wf.finalists}, default=str, indent=1))
    inp.frontier = frontier
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    child = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    notes.append(f"runtime {time.time() - t0:.0f}s; max RSS parent {rss:.0f} MB, largest worker {child:.0f} MB")
    inp.notes = notes
    p = RO.write_readout(inp, out)
    print(p)
    print(notes[-1])


if __name__ == "__main__":
    main()
