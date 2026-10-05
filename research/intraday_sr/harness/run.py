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
from research.intraday_sr.harness import s0grids
from research.intraday_sr.harness.config import COMPARISON, PENDING_R5, PRIMARY, CostCfg, RiskCfg
from research.intraday_sr.harness.grid_check import check_grids_module, grid_hash
from research.intraday_sr.harness.portfolio import SignalContractError, simulate
from research.intraday_sr.harness.triallog import DEFAULT_LEDGER, TrialLog, TrialRow, git_sha
from research.intraday_sr.harness.walkforward import (DEV_END, VariantRun, exact_finalist_check, make_folds,
                                                      walk_forward)

SPEC_VERSION = "v1.3.1"
_G: dict = {}          # fork-shared state for workers


def engine_cfg_for(variant: dict):
    """grids row -> EngineCfg: K maps to k_zones; everything else is frozen in EngineCfg / grids.FIXED."""
    from research.intraday_sr.types import EngineCfg
    return EngineCfg(k_zones=int(variant["K"]))


def signal_cfg_for(variant: dict, test: str | None = None):
    """grids row -> SignalCfg (oscillator, rvol_min, entry_tf, target, k_confirm, variant_id, test)."""
    from research.intraday_sr.types import SignalCfg
    return SignalCfg(oscillator=variant["oscillator"], rvol_min=float(variant["rvol_min"]),
                     entry_tf=variant["entry_tf"], target=variant["target"], k_confirm=int(variant["k_confirm"]),
                     variant_id=variant["variant_id"], test=test or variant.get("test") or variant["variant_id"][0])


def _engine_cfg_hash(cfg) -> str:
    import dataclasses
    import hashlib
    return hashlib.sha256(json.dumps(dataclasses.asdict(cfg), sort_keys=True).encode()).hexdigest()[:16]


def resolve_symbol_parquet(cache_root: Path, symbol: str) -> Path:
    """Trading stores ``part_NNNN_<SYM>.parquet`` (BRK.B as BRK-B). D2-1 ``symbol_cache_path`` still returns
    ``<root>/<SYM>.parquet`` (CP0 B3); resolve by glob here so the adapter can load the real cache without
    patching Developer 2's data layer."""
    from research.intraday_sr.data.cache import cache_filename, symbol_cache_path
    direct = symbol_cache_path(cache_root, symbol)
    if direct.is_file():
        return direct
    stem = cache_filename(symbol)
    hits = sorted(cache_root.glob(f"part_*_{stem}.parquet"))
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise FileNotFoundError(f"no 5m cache file for {symbol} under {cache_root} "
                                f"(looked for {direct.name} and part_*_{stem}.parquet)")
    raise FileNotFoundError(f"ambiguous cache files for {symbol}: {hits}")


class S0Adapter:
    """Adapter over Developer 2's S0/#23 modules: grids, data.cache (+ part_* resolution), engine.signals."""
    smoke = False

    def __init__(self, cache_root: str | Path | None = None, adj_path: str | Path | None = None,
                 symbols: list[str] | None = None):
        self.grids = importlib.import_module("research.intraday_sr.grids")
        self.engine = importlib.import_module("research.intraday_sr.engine")
        self.types = importlib.import_module("research.intraday_sr.types")
        self.cache = importlib.import_module("research.intraday_sr.data.cache")
        self.adjust = importlib.import_module("research.intraday_sr.data.adjust")
        root_default = Path("/workspace/research2/data/alpaca_intraday/m5rth_fixed33")
        self.cache_root = Path(cache_root or root_default)
        self.adj_path = Path(adj_path or (self.cache_root / "adj_factors" / "adj_factors.parquet"))
        if not self.adj_path.is_file():
            # fall back to combined / directory handled by our thin adjfactors adapter
            self.adj_path = self.cache_root / "adj_factors"
        self._symbols = list(symbols) if symbols is not None else list(
            getattr(self.grids, "UNIVERSE", None) or self.grids.load_universe_symbols())
        self._factors = None
        self._frames: dict = {}
        self._engine_secs: dict = {}

    def symbols(self) -> list:
        return list(self._symbols)

    def variants(self, test: str) -> list:
        return [dict(v) for v in {"A": self.grids.TEST_A, "B": self.grids.TEST_B}[test]]

    def engine_cfg(self, v) -> str:
        return _engine_cfg_hash(engine_cfg_for(v))

    def factors(self):
        if self._factors is None:
            if self.adj_path.is_file():
                self._factors = self.adjust.load_adj_factors(self.adj_path)
            else:
                self._factors = AF.load_adj_factors(self.cache_root)
        return self._factors

    def frame(self, sym: str):
        """Load via #22 cache.load_symbol (default end 2026-03-31; no HoldoutToken)."""
        if sym not in self._frames:
            from datetime import date as _date
            fr, _report = self.cache.load_symbol(
                self.cache_root, sym, factors=self.factors(),
                start=_date(2019, 1, 2), end=_date(2026, 3, 31), token=None,
            )
            # harness expects session as datetime.date; D2-1 stores int YYYYMMDD
            if len(fr) and not hasattr(fr["session"].iloc[0], "year"):
                sser = fr["session"].astype(int)
                fr = fr.copy()
                fr["session"] = [_date(v // 10000, (v // 100) % 100, v % 100) for v in sser]
            if "symbol" in fr.columns:
                fr = fr.copy()
                fr["symbol"] = fr["symbol"].astype(str)
            self._frames[sym] = fr
        return self._frames[sym]

    def bar_source(self, symbols):
        from research.intraday_sr.harness.portfolio import FrameBarSource
        return FrameBarSource({s: self.frame(s) for s in symbols})

    def sessions(self) -> list:
        from datetime import date as _date
        def _as_date(x):
            if isinstance(x, _date):
                return x
            x = int(x)
            return _date(x // 10000, (x // 100) % 100, x % 100)
        days = set()
        for s in self._symbols:
            if s in self._frames:
                days.update(map(_as_date, self._frames[s]["session"].unique()))
        if not days:
            days.update(map(_as_date, self.frame(self._symbols[0])["session"].unique()))
        return sorted(days)

    def signals(self, symbol: str, v: dict) -> list:
        """Call engine.signals(...) as an iterator; SignalCfg.test is always A for Test A variants."""
        fr = self.frame(symbol)
        if fr.empty:
            return []
        bars = self.types.BarSet(fr)
        start = fr["available_at"].iloc[0].to_pydatetime()
        end = fr["available_at"].iloc[-1].to_pydatetime()
        cfg = engine_cfg_for(v)
        sig = signal_cfg_for(v, test="A" if (v.get("test") or "A") == "A" else v.get("test"))
        t0 = time.perf_counter()
        out = list(self.engine.signals(bars, start, end, cfg, sig))  # iterator -> list
        self._engine_secs[(symbol, v["variant_id"])] = time.perf_counter() - t0
        return out


def make_smoke_adapter():
    """SPY+AAPL smoke: 5m/15m x k_confirm 0/3 (4 Test A variants). Labels run_kind=smoke.

    Bars load via #22 load_symbol through /workspace/research4/cache_links/ (SYM.parquet
    symlinks to part_NNNN — stands in for F1 until Developer 2 patches symbol_cache_path).
    """
    ids = [
        "A-K3-rsi14_30_70-rvol1.5-5m-1R-k0",
        "A-K3-rsi14_30_70-rvol1.5-5m-1R-k3",
        "A-K3-rsi14_30_70-rvol1.5-15m-1R-k0",
        "A-K3-rsi14_30_70-rvol1.5-15m-1R-k3",
    ]
    ad = S0Adapter(cache_root="/workspace/research4/cache_links",
                   adj_path="/workspace/research4/cache_links/adj_factors/adj_factors.parquet",
                   symbols=["SPY", "AAPL"])
    ad.smoke = True
    ad._variant_ids = set(ids)
    _orig = ad.variants
    ad.variants = lambda test, _o=_orig, _ids=set(ids): [v for v in _o(test) if v["variant_id"] in _ids]
    ad._engine_secs = {}
    return ad


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


def _sig_day(s) -> date:
    return s.available_at.date()


def sim_window(variant: dict, a: date, b: date, guardrail=PRIMARY, cost_mult: float = 1.0, sigs=None):
    """EXACT window path: sessions in [a, b] only, guardrail counters and equity ($100k) start at `a`, no
    internal resets. Used for OOS folds, the holdout and the R1 exact finalist check."""
    ad, symbols, src, sessions = _G["adapter"], _G["symbols"], _G["src"], _G["all_sessions"]
    sigs = sigs if sigs is not None else [s for sym in symbols for s in ad.signals(sym, variant)]
    sess = [d for d in sessions if a <= d <= b]
    risk = guardrail.apply(RiskCfg(target=variant["target"]))
    res = simulate([s for s in sigs if a <= _sig_day(s) <= b], src, risk, CostCfg(mult=cost_mult), sessions=sess,
                   window_starts={sess[0]} if sess else set())
    return VariantRun(variant["variant_id"], _trades_df(res), res.daily, config=guardrail.name), res.counters


def run_variant(variant: dict, guardrail=PRIMARY, cost_mult: float = 1.0, keep_raw: bool = False):
    """Continuous development path (selection; R only) + the exact fixed-variant OOS path (25 fold windows)."""
    ad, symbols, src, sessions = _G["adapter"], _G["symbols"], _G["src"], _G["sessions"]
    try:
        sigs = [s for sym in symbols for s in ad.signals(sym, variant)]
        risk = guardrail.apply(RiskCfg(target=variant["target"]))
        res = simulate(sigs, src, risk, CostCfg(mult=cost_mult), sessions=sessions)
        vr = VariantRun(variant["variant_id"], _trades_df(res), res.daily, config=guardrail.name)
        tparts, dparts, cnt = [], [], {}
        for f in make_folds():
            w, wc = sim_window(variant, f.test_start, f.test_end, guardrail, cost_mult, sigs)
            tparts.append(w.trades.assign(fold=f.k))
            dparts.append(w.daily)
            for k, v in wc.items():
                cnt[k] = cnt.get(k, 0) + v
        vr.exact_oos_trades = pd.concat(tparts, ignore_index=True) if tparts else pd.DataFrame()
        vr.exact_oos_daily = pd.concat(dparts) if dparts else pd.Series(dtype=float)
        return (vr, cnt, res if keep_raw else None)       # counters: from the exact OOS windows
    except SignalContractError:                              # input contract violations stop the run, loudly
        raise
    except Exception as e:                                   # other failures are logged trials too
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


_SIGCACHE: dict = {}


def _signals(variant: dict) -> list:
    vid = variant["variant_id"]
    if vid not in _SIGCACHE:
        _SIGCACHE[vid] = [s for sym in _G["symbols"] for s in _G["adapter"].signals(sym, variant)]
    return _SIGCACHE[vid]


def selected_path(picks, vmap, guardrail=PRIMARY, cost_mult: float = 1.0):
    """The walk-forward selected path re-simulated EXACTLY per fold window (counters/equity from each fold start)."""
    tp, dp, cnt = [], [], {}
    for p in picks:
        if not p["variant"]:
            continue
        a_, b_ = (date.fromisoformat(x) for x in p["test"])
        w, wc = sim_window(vmap[p["variant"]], a_, b_, guardrail, cost_mult, _signals(vmap[p["variant"]]))
        tp.append(w.trades)
        dp.append(w.daily)
        for k, v in wc.items():
            cnt[k] = cnt.get(k, 0) + v
    return (pd.concat(tp, ignore_index=True) if tp else pd.DataFrame(columns=["session", "r", "pnl", "symbol"]),
            pd.concat(dp) if dp else pd.Series(dtype=float), cnt)


def oos_from_picks(picks, runs) -> tuple[pd.DataFrame, pd.Series]:
    """Selected-path OOS from the EXACT per-fold window paths (never the continuous selection path)."""
    tp, dp = [], []
    for p in picks:
        a, b = (date.fromisoformat(x) for x in p["test"])
        if p["variant"] is None or p["variant"] not in runs:
            continue
        r = runs[p["variant"]]
        if r.exact_oos_trades is None:
            raise RuntimeError("exact OOS path missing: $/% results come only from exact fold paths")
        tr, dl = r.exact_oos_trades, r.exact_oos_daily
        s = pd.to_datetime(tr["session"]).dt.date if len(tr) else None
        if s is not None:
            tp.append(tr[(s >= a) & (s <= b)])
        di = pd.to_datetime(dl.index).date
        dp.append(dl[(di >= a) & (di <= b)])
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
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="program trial ledger directory (append-only)")
    a = ap.parse_args(argv)
    t0 = time.time()
    ad = load_adapter(a.adapter)
    syms = ad.symbols() if a.symbols == "available" else a.symbols.split(",")
    out = Path(a.out) / a.tag
    out.mkdir(parents=True, exist_ok=True)
    _G.update(adapter=ad, symbols=syms, src=ad.bar_source(syms),
              sessions=[d for d in ad.sessions() if d <= DEV_END], all_sessions=list(ad.sessions()))
    src = _G["src"]
    if not ad.smoke and _G["all_sessions"] and max(_G["all_sessions"]) > DEV_END:
        raise SystemExit("bar source reaches into the holdout (> 2026-03-31); dev runs load through DEV_END only "
                         "(SPEC v1.3.1 C2); the holdout is opened only by walkforward.run_holdout()")
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
    # R1: one append-only PROGRAM ledger for all real runs (smoke runs get their own, never counted)
    log = TrialLog(out / "ledger_smoke") if ad.smoke else TrialLog(Path(a.ledger))
    inp = RO.ReadoutInputs(SPEC_VERSION, sha, "", {}, syms, smoke=ad.smoke)
    manifest = {"run_id": a.tag, "run_kind": kind, "spec_version": SPEC_VERSION, "git_sha": sha,
                "guardrail": PRIMARY.name, "symbols": syms, "ledger": str(log.path), "grids": {},
                "grid_sha256": s0grids.grid_sha256(), "grids_source": s0grids.source(),
                "guardrails": {"primary": PRIMARY.name, "comparison": [g.name for g in COMPARISON]},
                "frozen_constants_pending_r5": list(PENDING_R5),
                "approximations": [
                    "guardrail counters reset at internal quarter starts in train windows (continuous selection "
                    "path; accepted by the R1 ruling for R-based selection only; finalists re-checked exactly)",
                    "OOS-fold and holdout paths are exact: counters and equity ($100k) start at each window start"],
                "adj_factors": adj_note, "started_at_ct": pd.Timestamp.now(tz="America/Chicago").isoformat()}
    frontier, notes = [], [f"adapter: {type(ad).__name__}; workers {a.workers}", adj_note]
    if not ad.smoke:
        # R5/R6: the whole grids module must pass the conformance check; the logged hash is grids.GRID_SHA256 (full).
        errs = check_grids_module(s0grids.grids())
        if errs:
            raise SystemExit("grids.py does not match SPEC v1.3.1: " + "; ".join(errs))
    for test in a.tests.split(","):
        variants = ad.variants(test)
        if a.max_variants:
            variants = variants[:a.max_variants]
        if ad.smoke:
            gsha = "smoke-variants:" + grid_hash(variants)       # fake smoke axes; never in the program ledger
        else:
            rows = {"A": s0grids.grids().TEST_A, "B": s0grids.grids().TEST_B}[test]
            strip = lambda v: {k: x for k, x in v.items() if k != "test"}
            if sorted(map(strip, variants), key=lambda v: v["variant_id"]) != \
                    sorted(map(strip, rows), key=lambda v: v["variant_id"]):
                raise SystemExit(f"Test {test} variants differ from grids.TEST_{test}; GRID_SHA256 would not cover them")
            gsha = s0grids.grid_sha256()
        inp.grid_sha = gsha
        results = _par(run_variant, variants, a.workers)
        runs = {vr.variant_id: vr for vr, _, _ in results}
        manifest["grids"][test] = gsha

        def exact_window(vid, a_, b_, _runs=runs, _vmap={v["variant_id"]: v for v in variants}):
            r = _runs[vid]
            fw = {(f.test_start, f.test_end) for f in make_folds()}
            if (a_, b_) in fw and r.exact_oos_trades is not None:          # precomputed exact fold window
                t_ = r.exact_oos_trades
                s_ = pd.to_datetime(t_["session"]).dt.date if len(t_) else pd.Series(dtype=object)
                di = pd.to_datetime(r.exact_oos_daily.index).date
                return VariantRun(vid, t_[(s_ >= a_) & (s_ <= b_)] if len(t_) else t_,
                                  r.exact_oos_daily[(di >= a_) & (di <= b_)], config=r.config)
            return sim_window(_vmap[vid], a_, b_, sigs=_signals(_vmap[vid]))[0]

        wf = walk_forward(runs, make_folds(), exact_window=exact_window)
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
        inp.trial_counts = {**inp.trial_counts, test: TrialLog.trial_count(df[df["run_id"] == a.tag], test)}
        inp.n_program = TrialLog.program_trial_count(df)
        inp.n_dsr = log.dsr_n()
        h = RO.headline(wf.oos_trades if len(wf.oos_trades) else pd.DataFrame(columns=["session", "r", "pnl"]),
                        wf.oos_daily, inp.n_dsr, var_sr=_trial_sr_var(runs))
        ec, eflag = exact_finalist_check(runs, wf.fold_table, wf.finalists, exact_window)
        inp.exact_checks = {**inp.exact_checks, test: (ec, eflag)}
        manifest.setdefault("exact_finalist_check", {})[test] = {"cp4_flag": bool(eflag), "rows": int(len(ec))}
        inp.headlines = {**inp.headlines, test: h}
        inp.picks = {**inp.picks, test: pd.DataFrame([{k: p.get(k) for k in ("fold", "test", "variant", "eligible",
                                                                                "train_mean_r", "train_trades")}
                                                       for p in wf.picks])}
        if len(wf.oos_trades):
            g = wf.oos_trades.groupby("symbol")
            inp.per_symbol = {**inp.per_symbol, test: pd.DataFrame({"trades": g.size(), "mean R": g["r"].mean().round(3),
                                                                    "win": (g["pnl"].apply(lambda s: (s > 0).mean())).round(3)}).reset_index()}
        frontier += [RO.frontier_row(test, vid, vr.exact_oos_trades, vr.exact_oos_daily)
                     for vid, vr in runs.items() if vr.status == "ok"]           # exact fold paths only
        picked = sorted({p["variant"] for p in wf.picks if p["variant"]})
        vmap = {v["variant_id"]: v for v in variants}
        # sensitivity (SPEC section 8): 1.5x costs on the selected path (same picks, primary config, exact windows)
        t2, d2, _ = selected_path(wf.picks, vmap, PRIMARY, 1.5)
        inp.sensitivities = {**inp.sensitivities, test: pd.DataFrame([
            {"case": "base costs", "mean R": RO._num(h["mean_r_ci"]["mean"]), "monthly": RO._pct(h["monthly_ci"]["mean"])},
            {"case": "1.5x costs", "mean R": RO._num(float(t2["r"].mean()) if len(t2) else None),
             "monthly": RO._pct(float(S.monthly_returns(d2).mean()) if len(d2) else None)}])}
        # G2: comparison configurations AFTER selection, same picks, only RiskCfg changed -> guardrail_compare.parquet
        tp_, dp_, prim_cnt = selected_path(wf.picks, vmap, PRIMARY)
        gstats = [RO.guardrail_stats(PRIMARY.name, tp_, dp_, prim_cnt)]
        crow = []
        for gcfg in COMPARISON:
            tg, dg, cnt = selected_path(wf.picks, vmap, gcfg)
            gs = RO.guardrail_stats(gcfg.name, tg, dg, cnt)
            crow.append({"run_id": a.tag, "test": test, "scope": "selected_path", "guardrail": gcfg.name,
                         "trades": gs["trades"], "trades_per_mo": gs["trades_per_mo"], "win_rate": gs["win_rate"],
                         "mean_r": gs["mean_r"].get("mean"), "monthly_mean": gs["monthly"].get("mean"),
                         "max_dd": gs["max_dd"], "worst_week": gs["worst_week"],
                         "days_halted_day_limit": gs["days_halted_day_limit"],
                         "weeks_halted_week_limit": gs["weeks_halted_week_limit"],
                         "daily_stop_days": gs["daily_stop_days"],
                         "signals_cancelled_at_trip": gs["signals_cancelled_at_trip"],
                         "signals_arrived_blocked": gs["signals_arrived_blocked"]})
            gstats.append(gs)
        write_compare(out, crow)
        inp.guardrails = {**inp.guardrails, f"Test {test} selected path":
                          (RO.guardrail_rows(gstats), RO.trades_per_month_words(gstats))}
        (out / f"wf_{test}.json").write_text(json.dumps({"picks": wf.picks, "finalists": wf.finalists}, default=str, indent=1))
    inp.frontier = frontier
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    child = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    notes.append(f"runtime {time.time() - t0:.0f}s; max RSS parent {rss:.0f} MB, largest worker {child:.0f} MB")
    inp.notes = notes + manifest["approximations"]
    manifest["finished_at_ct"] = pd.Timestamp.now(tz="America/Chicago").isoformat()
    manifest["program_trials"] = inp.n_program
    manifest["dsr_n"] = inp.n_dsr
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str))
    p = RO.write_readout(inp, out)
    print(p)
    print(notes[-1])


if __name__ == "__main__":
    main()
