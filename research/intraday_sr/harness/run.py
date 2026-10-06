"""Run driver: grid -> per-variant portfolio simulation -> walk-forward -> trial log -> readout.

    python -m research.intraday_sr.harness.run --tests A --symbols available --tag interim1 --workers 6

Per-symbol chunking (default): pass 1 hands each worker ONE symbol; it loads that symbol once, runs every requested
variant for it in `chunk_order` (variants sharing entry_tf, then K, back to back, so the engine's bounded zone/level
caches stay warm), then calls `engine.zone_cache.clear_zone_cache()` before the next symbol. Pass 2 simulates each
variant across ALL symbols (the d2+w5 guardrail is portfolio-wide, so simulation cannot be split by symbol) from the
signals pass 1 collected. Ledger rows are identical to the unchunked path (--no-chunk), which is kept for checks.

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
import gc
import importlib
import json
import multiprocessing as mp
import os
import resource
import shutil
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
from research.intraday_sr.harness import spill as SP
from research.intraday_sr.harness.config import COMPARISON, PENDING_R5, PRIMARY, CostCfg, RiskCfg
from research.intraday_sr.harness.grid_check import check_grids_module, grid_hash
from research.intraday_sr.harness.fb_signals import (EngineContractError, StubEngineError, engine_cfg_for_variant,
                                                     formation_signals_fn, signals_for_b, signals_for_f,
                                                     test_b_signals_fn)
from research.intraday_sr.harness.guard import LookaheadError
from research.intraday_sr.harness.portfolio import SignalContractError, set_legacy, simulate
from research.intraday_sr.harness.triallog import DEFAULT_LEDGER, TrialLog, TrialRow, git_sha
from research.intraday_sr.harness.walkforward import (DEV_END, VariantRun, exact_finalist_check, make_folds,
                                                      walk_forward)

SPEC_VERSION = "v1.3.1"     # grid spec stamp: grids.py / GRID_SHA256 are pinned to v1.3.1 (never the engine version)
# Engine spec when research/intraday_sr/engine/version.py (Developer 2's engine_stamp(), lands on #23 after 30c20eb)
# is absent, as on the A1b head (engine frozen at 30c20eb = SPEC v1.3.3 engine). Kept out of grid_document().
ENGINE_SPEC_FALLBACK = "v1.3.3"
# SPEC v1.3.5 (docs 8504fd7) stamps F and B manifests only. Test A keeps ENGINE_SPEC_FALLBACK.
FORMATION_TESTS = ("F_W", "F_IHS", "F_M", "F_HS")
FORMATION_DSR_N = 48          # S2: each formation kind's DSR uses N=48, not the program floor
SPEC_DOC_FB = "v1.3.5"
SPEC_DOC_COMMIT_FB = "8504fd7c19136141a32746234b63dd084106369d"
ENGINE_SPEC_FB = "v1.3.5"
_G: dict = {}          # fork-shared state for workers


def expand_test(token: str) -> list[str]:
    """--test F is four portfolios (S1). A, B, and a single kind stay one portfolio."""
    token = token.strip()
    if token == "F":
        return list(FORMATION_TESTS)
    if token in ("A", "B") or token in FORMATION_TESTS:
        return [token]
    raise SystemExit(f"unknown test {token!r}; expected A, B, F, or one of {', '.join(FORMATION_TESTS)}")


def parse_test_args(tests: str | None, test: str | None) -> list[str]:
    """Resolve --test and --tests. Both omitted means Test A. Both set must expand to the same list."""
    def expand(raw: str) -> list[str]:
        out: list[str] = []
        for tok in raw.split(","):
            if tok.strip():
                out.extend(expand_test(tok))
        if not out:
            raise SystemExit("empty --test/--tests")
        return out

    if test is None and tests is None:
        return ["A"]
    got_test = expand(test) if test is not None else None
    got_tests = expand(tests) if tests is not None else None
    if got_test is not None and got_tests is not None and got_test != got_tests:
        raise SystemExit(f"--test {test} and --tests {tests} disagree: {got_test} vs {got_tests}")
    return got_test if got_test is not None else got_tests  # type: ignore[return-value]


def grid_family_for(test: str) -> str:
    return "formations" if test in FORMATION_TESTS else ""


def _canonical_grid_rows(test: str) -> list:
    g = s0grids.grids()
    if test == "A":
        return list(g.TEST_A)
    if test == "B":
        return list(g.TEST_B)
    if test in FORMATION_TESTS:
        return [v for v in g.FORMATIONS if v["test"] == test]
    raise SystemExit(f"no canonical grid for test {test!r}")


def _plan_is_fb(plan) -> bool:
    return any(test == "B" or test in FORMATION_TESTS for test, _, _ in plan)


def _git(repo: Path, *args: str) -> str:
    import subprocess
    try:
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def engine_stamp_fields(repo: Path) -> dict:
    """Run-time provenance for the manifest, outside grid_document()/GRID_SHA256: harness_commit (git HEAD),
    engine_commit (last commit touching research/intraday_sr/engine), engine_tree (that directory's tree hash) and
    engine_spec (engine/version.py's engine_stamp() when present, else ENGINE_SPEC_FALLBACK)."""
    spec, src = ENGINE_SPEC_FALLBACK, "harness fallback (engine/version.py absent)"
    try:
        ver = importlib.import_module("research.intraday_sr.engine.version")
        st = ver.engine_stamp() if hasattr(ver, "engine_stamp") else {}
        got = st.get("engine_spec") if isinstance(st, dict) else getattr(ver, "ENGINE_SPEC", None)
        if got:
            spec, src = str(got), "engine/version.py"
    except ImportError:
        pass
    return {"harness_commit": _git(repo, "rev-parse", "HEAD"),
            "engine_commit": _git(repo, "log", "-1", "--format=%H", "--", "research/intraday_sr/engine"),
            "engine_tree": _git(repo, "rev-parse", "HEAD:research/intraday_sr/engine"),
            "engine_spec": spec, "engine_spec_source": src}


NOT_FOR_CP4 = ("NOT FOR CP4/SELECTION: attribution check with legacy (pre-A1b) fill rules on; results are not "
               "program trials and are written to a scratch ledger, never PROGRAM_LEDGER")


def ledger_dir(smoke: bool, attribution: bool, out: Path, program_ledger: Path) -> Path:
    """R1: one append-only PROGRAM ledger for real runs. Smoke runs and legacy-flag attribution checks get their own
    scratch ledger under the run dir and can never append to the PROGRAM ledger."""
    if smoke:
        return out / "ledger_smoke"
    if attribution:
        p = out / "ledger_attribution"
        if Path(p).resolve() == Path(program_ledger).resolve():
            raise SystemExit("attribution ledger must not be the PROGRAM ledger")
        return p
    return program_ledger


def engine_cfg_for(variant: dict):
    """grids row -> EngineCfg. K maps to k_zones. Formations have no K axis: EngineCfg's default (5)."""
    return engine_cfg_for_variant(variant)


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


# The only bar columns the simulator (portfolio._run_day, FrameBarSource.daily, the adj-factor coverage check) reads.
# The bar source keeps just these, which roughly halves its per-session frames (~10 vs ~20 MB per symbol), memory that
# the parent holds through pass 2 and every forked pass-2 worker maps.
SIM_BAR_COLUMNS = ("ts", "open", "high", "low", "close", "adj_factor", "volume", "session",
                   "high_unclamped", "high_raw", "low_unclamped", "low_raw", "available_at")


def sim_bar_frame(fr: pd.DataFrame) -> pd.DataFrame:
    """Column subset of a loaded frame for the bar source (all simulator inputs kept, in the frame's order)."""
    return fr[[c for c in fr.columns if c in SIM_BAR_COLUMNS]]


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
        self._frame_files: dict = {}        # pass-1 handoff: symbol -> pickled frame written by its worker
        self._sessions_by_sym: dict = {}
        self._engine_secs: dict = {}

    def symbols(self) -> list:
        return list(self._symbols)

    def variants(self, test: str) -> list:
        if test == "A":
            rows = self.grids.TEST_A
        elif test == "B":
            rows = self.grids.TEST_B
        elif test in FORMATION_TESTS:
            rows = [v for v in self.grids.FORMATIONS if v["test"] == test]
        else:
            raise KeyError(test)
        return [dict(v) for v in rows]

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
        if sym not in self._frames and sym in self._frame_files:
            self._frames[sym] = pd.read_pickle(self._frame_files[sym])
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
        """Frames handed off as files by pass-1 workers are read one at a time and NOT kept: the bar source holds its
        own per-session copies, so the parent never holds all 33 full frames (about 0.6 GB)."""
        from research.intraday_sr.harness.portfolio import FrameBarSource
        ad = self

        class _Lazy:
            def items(self_):
                for s_ in symbols:
                    if s_ not in ad._frames and s_ in ad._frame_files:
                        fr = pd.read_pickle(ad._frame_files[s_])
                        ad._sessions_by_sym[s_] = set(fr["session"].unique())
                        yield s_, sim_bar_frame(fr)
                        del fr
                    else:
                        yield s_, sim_bar_frame(ad.frame(s_))
        return FrameBarSource(_Lazy())

    # Per-symbol chunking: a pass-1 worker loads its symbol and hands the loaded frame to the parent. With a frame
    # directory set (harness.run sets <out>/_frames) the worker writes it there and returns only the path, so the
    # parent's memory does not grow by one frame per finished symbol while pass-1 workers are still running.
    def symbol_payload(self, sym: str):
        fr = self._frames.get(sym)
        fdir = _G.get("frame_dir")
        if fr is None or not fdir:
            return fr
        Path(fdir).mkdir(parents=True, exist_ok=True)
        p = Path(fdir) / f"{sym}.pkl"
        tmp = Path(fdir) / f".{sym}.{os.getpid()}.tmp"
        fr.to_pickle(tmp)
        os.replace(tmp, p)
        return str(p)

    def adopt_symbol_payload(self, sym: str, payload) -> None:
        if isinstance(payload, (str, Path)):
            self._frame_files[sym] = Path(payload)
        elif payload is not None:
            self._frames[sym] = payload

    def symbol_cost(self, sym: str) -> float:
        """Scheduling weight only (largest symbols start first): size of the symbol's 5m cache file."""
        try:
            return float(resolve_symbol_parquet(self.cache_root, sym).stat().st_size)
        except OSError:
            return 0.0

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
            elif s in self._sessions_by_sym:
                days.update(map(_as_date, self._sessions_by_sym[s]))
        if not days:
            days.update(map(_as_date, self.frame(self._symbols[0])["session"].unique()))
        return sorted(days)

    def signals(self, symbol: str, v: dict) -> list:
        """Test A calls engine.signals. F and B call the entry points in harness/fb_signals.py.

        A missing F/B entry point raises StubEngineError before any empty iterator can become zero trades.
        """
        test = str(v.get("test") or "A")
        fr = self.frame(symbol)
        if test in FORMATION_TESTS or test == "B":
            if fr.empty:
                (formation_signals_fn if test in FORMATION_TESTS else test_b_signals_fn)()
                return []
            bars = self.types.BarSet(fr)
            start = fr["available_at"].iloc[0].to_pydatetime()
            end = fr["available_at"].iloc[-1].to_pydatetime()
            t0 = time.perf_counter()
            out = signals_for_f(bars, start, end, v) if test in FORMATION_TESTS else signals_for_b(
                bars, start, end, v)
            self._engine_secs[(symbol, v["variant_id"])] = time.perf_counter() - t0
            return out
        if fr.empty:
            return []
        bars = self.types.BarSet(fr)
        start = fr["available_at"].iloc[0].to_pydatetime()
        end = fr["available_at"].iloc[-1].to_pydatetime()
        cfg = engine_cfg_for(v)
        sig = signal_cfg_for(v, test="A")
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


TRADE_COLUMNS = ["session", "symbol", "r", "pnl", "exit_kind", "capped", "entry_ts", "exit_ts", "day_losses_before",
                 "week_losses_before", "direction", "signal_available_at", "entry_price", "exit_price", "qty",
                 "entry_cost", "exit_cost", "gross_R", "cost_R", "net_R"]


def _trades_df(res) -> pd.DataFrame:
    if not res.trades:
        return pd.DataFrame(columns=TRADE_COLUMNS)
    m = pd.DataFrame(res.meta)
    T = res.trades
    net_R = np.array([float(t.r) for t in T], dtype=float)
    entry_cost = np.array([float(t.entry.cost) for t in T], dtype=float)
    exit_cost = np.array([float(t.exit.cost) for t in T], dtype=float)
    risk = pd.to_numeric(m["risk_usd"], errors="coerce").to_numpy(float)
    cost_R = np.divide(entry_cost + exit_cost, risk, out=np.full(net_R.shape, np.nan), where=risk > 0)
    # R7: gross = net + cost, with net_R the after-cost R the portfolio already stored on Trade.r.
    gross_R = net_R + cost_R
    return pd.DataFrame({"session": m["session"], "symbol": m["symbol"], "r": net_R,
                         "pnl": [t.pnl for t in T], "exit_kind": m["exit_kind"], "capped": m["capped"],
                         "entry_ts": m["entry_ts"], "exit_ts": m["exit_ts"],
                         "day_losses_before": m["day_losses_before"], "week_losses_before": m["week_losses_before"],
                         # per-trade fills (A1b trades file, CP4 trade-by-trade reconciliation)
                         "direction": [int(t.entry.side) for t in T],
                         "signal_available_at": [t.signal.available_at for t in T],
                         "entry_price": [float(t.entry.price) for t in T], "exit_price": [float(t.exit.price) for t in T],
                         "qty": [float(t.entry.qty) for t in T], "entry_cost": entry_cost, "exit_cost": exit_cost,
                         "gross_R": gross_R, "cost_R": cost_R, "net_R": net_R})


class TradesFile:
    """<out>/trades_<test>.parquet, one row group per (path, variant): every variant's continuous development path
    ('dev') and exact OOS fold paths ('oos_exact', with fold), plus the walk-forward selected path under each
    guardrail configuration ('selected'). Written incrementally so the parent never builds one big frame."""

    def __init__(self, path: Path):
        self.path, self._w, self.rows = Path(path), None, 0

    def add(self, df: pd.DataFrame, **const) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq
        cols = ["path", "variant_id", "guardrail", "fold"] + TRADE_COLUMNS
        d = pd.DataFrame(df if df is not None and len(df) else [], columns=[c for c in cols if c not in const])
        if "fold" not in d.columns or d["fold"].isna().all():
            d["fold"] = const.pop("fold", -1)
        for k, v in const.items():
            d[k] = v
        d = d.reindex(columns=cols)
        for c in ("session", "signal_available_at", "entry_ts", "exit_ts"):
            d[c] = d[c].astype(str)
        d["fold"] = pd.to_numeric(d["fold"], errors="coerce").fillna(-1).astype("int32")
        for c in ("r", "pnl", "entry_price", "exit_price", "qty", "entry_cost", "exit_cost",
                  "gross_R", "cost_R", "net_R"):
            d[c] = pd.to_numeric(d[c], errors="coerce").astype("float64")
        for c in ("direction", "day_losses_before", "week_losses_before"):
            d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0).astype("int32")
        d["capped"] = d["capped"].fillna(False).astype(bool)
        for c in ("path", "variant_id", "guardrail", "symbol", "exit_kind"):
            d[c] = d[c].astype(str)
        t = pa.Table.from_pandas(d, preserve_index=False)
        if self._w is None:
            self._w = pq.ParquetWriter(self.path, t.schema, compression="zstd")
        self._w.write_table(t.cast(self._w.schema))
        self.rows += len(d)

    def close(self) -> None:
        if self._w is not None:
            self._w.close()


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


_TF_RANK = {"5m": 0, "15m": 1}


def chunk_order(variants: list) -> list:
    """Deterministic per-symbol variant order. Variants that share entry_tf run consecutively (zones are keyed by
    entry_tf), then K (zones are keyed by k_zones), then oscillator (signal prep is keyed by it), so the engine's
    bounded caches are reused warm before they are evicted."""
    def key(v):
        tf = str(v.get("entry_tf", ""))
        return (_TF_RANK.get(tf, 9), tf, int(v.get("K", 0) or 0), str(v.get("oscillator", "")),
                float(v.get("rvol_min", 0) or 0), str(v.get("target", "")), int(v.get("k_confirm", 0) or 0),
                str(v["variant_id"]))
    return sorted(variants, key=key)


def clear_engine_caches() -> None:
    """Free every engine cache between symbols (Developer 2's public hook on #23 >= 3d782d2).

    Detach live Signal/Zone map blobs first so any leftover refs from the last variant
    keep readable floats after ``release_frozen_maps`` inside ``clear_zone_cache``.
    """
    try:
        from research.intraday_sr.engine.zone_cache import clear_zone_cache
        from research.intraday_sr.types import detach_live_maps
    except ImportError:          # stub adapters / engines without the cache module
        return
    detach_live_maps()
    clear_zone_cache()


def release_engine_signals() -> None:
    """After each (variant, symbol) is spilled: drop the engine's retained signal list once it is over budget and
    return the frozen-map bytes (Developer 2's hook; each signals() call otherwise grows the map buffer ~8 MB)."""
    try:
        from research.intraday_sr.engine.zone_cache import release_oversized_signals
    except ImportError:          # stub adapters / engines without the hook
        return
    release_oversized_signals()


def _rss_now_mb() -> float:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return float("nan")


def _maxrss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def symbol_pass(sym: str) -> dict:
    """Pass-1 task: load `sym` once, signals for every requested variant (chunk_order), then clear engine caches.
    Each (variant, symbol) signal list is spilled to <spill>/<variant_id>/<symbol>.npz (compact columns; .pkl with
    --spill-format pickle) as soon as it is built, so neither the worker nor the parent holds the grid's signals."""
    ad, variants, spill = _G["adapter"], _G["chunk_variants"], Path(_G["spill_dir"])
    t0 = time.perf_counter()
    if hasattr(ad, "frame"):
        ad.frame(sym)
    load_s = time.perf_counter() - t0
    sigs, errs, secs, spill_s, order, rss = {}, {}, {}, {}, [], {}
    try:
        for v in chunk_order(variants):
            vid = v["variant_id"]
            order.append(vid)
            t1 = time.perf_counter()
            try:
                got = list(ad.signals(sym, v))
            except (SignalContractError, StubEngineError, EngineContractError, LookaheadError):
                raise
            except Exception as e:                    # logged as an errored trial in pass 2 (same as unchunked)
                errs[vid] = repr(e)
                secs[vid] = time.perf_counter() - t1
                continue
            secs[vid] = time.perf_counter() - t1
            t2 = time.perf_counter()
            if _G.get("spill_format", "compact") == "pickle":
                SP.write(spill, vid, sym, got)    # legacy full objects (checks only)
            else:
                SP.write_compact(spill, vid, sym, got)   # a spill failure is a harness fault: it stops the run
            spill_s[vid] = time.perf_counter() - t2
            sigs[vid] = len(got)
            del got                               # serialized to disk above, so nothing depends on the freed maps
            release_engine_signals()
            rss[vid] = _rss_now_mb()
    finally:
        clear_engine_caches()
    payload = ad.symbol_payload(sym) if (_G.get("chunk_transfer") and hasattr(ad, "symbol_payload")) else None
    return {"symbol": sym, "signals": sigs, "errors": errs, "secs": secs, "spill_s": spill_s, "order": order,
            "load_s": load_s, "total_s": time.perf_counter() - t0, "maxrss_mb": _maxrss_mb(), "rss_after_mb": rss,
            "pid": os.getpid(), "payload": payload}


def run_symbol_passes(ad, symbols: list, variants: list, workers: int, spill_dir: Path) -> dict:
    """Pass 1 over symbols. Spills signals per (variant, symbol) under `spill_dir` (read back by `_signals` in
    `symbols` order) and fills _SIGERR (first error in symbol order). Returns per-symbol timing records."""
    _G["spill_dir"] = str(spill_dir)
    _G["chunk_variants"] = list(variants)
    _G["chunk_transfer"] = workers > 1
    cost = getattr(ad, "symbol_cost", None)
    sched = sorted(symbols, key=lambda s_: -cost(s_)) if cost else list(symbols)     # largest first (LPT)
    if workers <= 1:
        recs = [symbol_pass(s_) for s_ in sched]
    else:
        # fresh process per symbol (maxtasksperchild=1): nothing a symbol allocated outlives it
        with mp.get_context("fork").Pool(min(workers, len(sched)), maxtasksperchild=1) as pool:
            recs = pool.map(symbol_pass, sched, chunksize=1)
    by = {r["symbol"]: r for r in recs}
    for s_ in symbols:
        r = by[s_]
        if r["payload"] is not None:
            ad.adopt_symbol_payload(s_, r["payload"])
        r["payload"] = None
    for v in variants:
        vid = v["variant_id"]
        err = next((by[s_]["errors"][vid] for s_ in symbols if vid in by[s_]["errors"]), None)
        if err is not None:
            _SIGERR[vid] = err
    for r in recs:
        r["n_signals"] = r.pop("signals")
    return by


def run_variant(variant: dict, guardrail=PRIMARY, cost_mult: float = 1.0, keep_raw: bool = False):
    """Continuous development path (selection; R only) + the exact fixed-variant OOS path (25 fold windows)."""
    ad, symbols, src, sessions = _G["adapter"], _G["symbols"], _G["src"], _G["sessions"]
    if variant["variant_id"] in _SIGERR:                     # pass-1 signal failure: same errored trial as unchunked
        return (VariantRun(variant["variant_id"], pd.DataFrame(), pd.Series(dtype=float), "error",
                           _SIGERR[variant["variant_id"]], config=guardrail.name), {}, None)
    try:
        sigs = _signals(variant, keep=False)     # workers never accumulate other variants' signals
        risk = guardrail.apply(RiskCfg(target=variant["target"]))
        res = simulate(sigs, src, risk, CostCfg(mult=cost_mult), sessions=sessions)
        vr = VariantRun(variant["variant_id"], _trades_df(res), res.daily, config=guardrail.name)
        vr.n_signals = len(sigs)
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
    except (SignalContractError, StubEngineError, EngineContractError, LookaheadError):
        raise                                            # contract, stub, and lookahead stop the run, loudly
    except Exception as e:                                   # other failures are logged trials too
        return (VariantRun(variant["variant_id"], pd.DataFrame(), pd.Series(dtype=float), "error", repr(e),
                           config=guardrail.name), {}, None)


def _drop_spill(out: Path, keep: bool) -> None:
    _SIGCACHE.clear()
    if (out / "_frames").exists():          # pass-1 frame handoff files: never kept
        shutil.rmtree(out / "_frames")
    if not keep and (out / "_signals").exists():
        shutil.rmtree(out / "_signals")


def _timed_run_variant(variant: dict):
    t1 = time.perf_counter()
    r = run_variant(variant)
    return r, time.perf_counter() - t1, _maxrss_mb()


def _par(fn, items, workers):
    if workers <= 1:
        return [fn(x) for x in items]
    # gc.freeze(): the forked workers' garbage collector then never walks (and so never copy-on-write dirties) the
    # parent's objects, chiefly the bar source; results are unaffected.
    gc.collect()
    gc.freeze()
    try:
        with mp.get_context("fork").Pool(workers) as pool:
            return pool.map(fn, items, chunksize=1)
    finally:
        gc.unfreeze()


def _trial_sr_var(runs) -> float | None:
    """Variance across trials of the (daily) Sharpe of each variant's development path, for the DSR."""
    srs = [S.sharpe(vr.daily) for vr in runs.values() if vr.status == "ok" and len(vr.daily) > 20]
    srs = [x for x in srs if x is not None and np.isfinite(x)]
    return float(np.var(srs, ddof=1)) if len(srs) >= 2 else None


_SIGCACHE: dict = {}
_SIGERR: dict = {}          # variant_id -> repr of the first pass-1 signal error (symbol order)


def _signals(variant: dict, keep: bool = True) -> list:
    """One variant's signals across all run symbols. From the spill (chunked path) the default loader is compact:
    lightweight records in `spill.merge_streams` order (available_at, symbol, within-symbol order), which is the
    order `simulate` reaches from the legacy list, so results are identical; --pass2-loader objects reads the full
    pickled Signals (legacy, needs --spill-format pickle)."""
    vid = variant["variant_id"]
    if vid in _SIGCACHE:
        return _SIGCACHE[vid]
    if _G.get("spill_dir"):
        if _G.get("pass2_loader", "compact") == "objects":
            sigs = SP.read(Path(_G["spill_dir"]), vid, _G["symbols"])
        else:
            sigs = SP.read_compact(Path(_G["spill_dir"]), vid, _G["symbols"], rename=_G.get("spill_rename"))
    else:
        sigs = [s for sym in _G["symbols"] for s in _G["adapter"].signals(sym, variant)]
    if keep:
        _SIGCACHE[vid] = sigs
    return sigs


def selected_path(picks, vmap, guardrail=PRIMARY, cost_mult: float = 1.0):
    """The walk-forward selected path re-simulated EXACTLY per fold window (counters/equity from each fold start)."""
    tp, dp, cnt = [], [], {}
    for p in picks:
        if not p["variant"]:
            continue
        a_, b_ = (date.fromisoformat(x) for x in p["test"])
        w, wc = sim_window(vmap[p["variant"]], a_, b_, guardrail, cost_mult, _signals(vmap[p["variant"]]))
        tp.append(w.trades.assign(fold=p.get("fold", -1), variant_id=p["variant"]))
        dp.append(w.daily)
        for k, v in wc.items():
            cnt[k] = cnt.get(k, 0) + v
    return (pd.concat(tp, ignore_index=True) if tp else pd.DataFrame(columns=["session", "r", "pnl", "symbol"]),
            pd.concat(dp) if dp else pd.Series(dtype=float), cnt)


def _r7_means(df: pd.DataFrame | None) -> dict:
    """R7: gross, cost, and net mean R. Empty or column-less frames stay explicit Nones."""
    n = 0 if df is None else int(len(df))
    if df is None or n == 0 or any(c not in df.columns for c in ("gross_R", "cost_R", "net_R")):
        return {"trades": n, "gross_mean_r": None, "cost_mean_r": None, "net_mean_r": None}
    return {"trades": n, "gross_mean_r": float(df["gross_R"].mean()),
            "cost_mean_r": float(df["cost_R"].mean()), "net_mean_r": float(df["net_R"].mean())}


def _r7_block(runs: dict, selected: pd.DataFrame) -> dict:
    parts = [vr.exact_oos_trades for vr in runs.values()
             if vr.status == "ok" and vr.exact_oos_trades is not None and len(vr.exact_oos_trades)]
    pooled = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return {"selected": _r7_means(selected), "pooled": _r7_means(pooled)}


def _empty_portfolio(test: str, results) -> None:
    """A live F/B entry point that found nothing must not look like a finished zero-trade study."""
    runs = [vr for vr, _, _ in results]
    if not runs or any(vr.status != "ok" for vr in runs) or sum(vr.n_signals for vr in runs) != 0:
        return
    if test in FORMATION_TESTS:
        raise SystemExit(
            f"Test {test}: zero formations from formation_signals across every variant and symbol. "
            "Refusing a silent zero-trade run. Pass --allow-empty only for fixtures."
        )
    if test == "B":
        raise SystemExit(
            f"Test {test}: zero signals from test_b_signals across every variant and symbol. "
            "Refusing a silent zero-trade run. Pass --allow-empty only for fixtures."
        )


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
    ap.add_argument("--tests", default=None, help="comma-separated portfolios (A, B, F, or F_W/F_IHS/F_M/F_HS). "
                    "F expands to the four kinds. Default A when --test is also omitted.")
    ap.add_argument("--test", default=None, help="SPEC flag. Same tokens as --tests. --test F is four portfolios.")
    ap.add_argument("--allow-empty", action="store_true",
                    help="fixtures only: a real F/B entry point that returns no signals may finish. "
                         "A missing entry point still raises StubEngineError.")
    ap.add_argument("--symbols", default="available")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", default="/workspace/research4/runs")
    ap.add_argument("--workers", type=int, default=max(1, min(6, (os.cpu_count() or 2) - 2)))
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--max-variants", type=int, default=0, help="smoke only")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="program trial ledger directory (append-only)")
    ap.add_argument("--no-chunk", action="store_true",
                    help="legacy path (checks only): each worker takes one variant across all symbols")
    ap.add_argument("--keep-signals", action="store_true",
                    help="keep the pass-1 signal spill (<out>/<tag>/_signals) after the run")
    ap.add_argument("--pass2-workers", type=int, default=0,
                    help="pass-2 (simulation) pool size; default = --workers. Pass 2 workers are lighter than pass 1")
    ap.add_argument("--spill-format", choices=("compact", "pickle"), default="compact",
                    help="pass-1 spill: compact columns (.npz, default) or full pickled Signals (.pkl, checks only)")
    ap.add_argument("--pass2-loader", choices=("compact", "objects"), default="compact",
                    help="pass-2 signal loader: compact records (default) or full pickled Signals (legacy check)")
    ap.add_argument("--timing-only", action="store_true",
                    help="pass 1 + pass 2 only, then write timing.json; no walk-forward, ledger, manifest or readout")
    ap.add_argument("--legacy-target-gap", action="store_true",
                    help="ATTRIBUTION CHECK ONLY: pre-A1b target gap rule (open past target by >= 1 tick). Never "
                         "writes the PROGRAM ledger; manifest/readout marked NOT FOR CP4/SELECTION")
    ap.add_argument("--legacy-halfday", action="store_true",
                    help="ATTRIBUTION CHECK ONLY: pre-A1b entry cutoff (last_entry only, entries allowed at/after a "
                         "half day's forced-exit bar). Never writes the PROGRAM ledger; NOT FOR CP4/SELECTION")
    a = ap.parse_args(argv)
    t0 = time.time()
    started_ct = pd.Timestamp.now(tz="America/Chicago").isoformat()     # true launch (same instant as t0)
    legacy = set_legacy(a.legacy_target_gap, a.legacy_halfday)         # before any worker forks
    attribution = any(legacy.values())
    _SIGCACHE.clear()
    _SIGERR.clear()
    _G.clear()
    ad = load_adapter(a.adapter)
    avail = ad.symbols()
    syms = avail if a.symbols == "available" else [x.strip() for x in a.symbols.split(",") if x.strip()]
    unknown = [x for x in syms if x not in avail]
    if unknown or not syms:
        raise SystemExit(f"--symbols: unknown or empty {unknown or syms}; available: {' '.join(avail)}")
    out = Path(a.out) / a.tag
    out.mkdir(parents=True, exist_ok=True)
    if not ad.smoke:
        # R5/R6: the whole grids module must pass the conformance check; the logged hash is grids.GRID_SHA256 (full).
        errs = check_grids_module(s0grids.grids())
        if errs:
            raise SystemExit("grids.py does not match SPEC v1.3.1: " + "; ".join(errs))
    plan = []
    for test in parse_test_args(a.tests, a.test):
        variants = ad.variants(test)
        if a.max_variants:
            variants = variants[:a.max_variants]
        if ad.smoke:
            gsha = "smoke-variants:" + grid_hash(variants)       # fake smoke axes; never in the program ledger
        else:
            rows = _canonical_grid_rows(test)
            strip = lambda v: {k: x for k, x in v.items() if k != "test"}
            if sorted(map(strip, variants), key=lambda v: v["variant_id"]) != \
                    sorted(map(strip, rows), key=lambda v: v["variant_id"]):
                raise SystemExit(f"Test {test} variants differ from the grids.py rows for {test}; "
                                 "GRID_SHA256 would not cover them")
            gsha = s0grids.grid_sha256()
        plan.append((test, variants, gsha))
    fb_stamp = _plan_is_fb(plan)
    p2w = a.pass2_workers or a.workers
    timing = {"run_id": a.tag, "started_at_ct": started_ct, "legacy": legacy, "symbols": syms, "workers": a.workers, "pass2_workers": p2w,
              "spill_format": a.spill_format, "pass2_loader": a.pass2_loader, "chunked": not a.no_chunk,
              "grid_sha256": s0grids.grid_sha256(), "pass1": None, "pass2": {}}
    _G.update(adapter=ad, symbols=syms, spill_format=a.spill_format, pass2_loader=a.pass2_loader)
    if not a.no_chunk:
        tp = time.time()
        spill = out / "_signals"
        if spill.exists():
            shutil.rmtree(spill)
        if (out / "_frames").exists():
            shutil.rmtree(out / "_frames")
        _G["frame_dir"] = str(out / "_frames")
        recs = run_symbol_passes(ad, syms, [v for _, vs, _ in plan for v in vs], a.workers, spill)
        timing["pass1"] = {"wall_s": time.time() - tp, "per_symbol": recs,
                           "spill_bytes": sum(f.stat().st_size for f in spill.rglob("*")
                                              if f.is_file() and f.suffix in (".pkl", ".npz"))}
    tp = time.time()
    _G.update(src=ad.bar_source(syms),
              sessions=[d for d in ad.sessions() if d <= DEV_END], all_sessions=list(ad.sessions()))
    timing["bar_source_s"] = time.time() - tp
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
    if attribution:
        kind = "attribution"
    log = TrialLog(ledger_dir(ad.smoke, attribution, out, Path(a.ledger)))
    inp = RO.ReadoutInputs(SPEC_VERSION, sha, "", {}, syms, smoke=ad.smoke,
                           not_for_cp4=NOT_FOR_CP4 if attribution else "")
    manifest = {"run_id": a.tag, "run_kind": kind, "spec_version": SPEC_VERSION, "git_sha": sha,
                "guardrail": PRIMARY.name, "symbols": syms, "ledger": str(log.path), "grids": {},
                "grid_sha256": s0grids.grid_sha256(), "grids_source": s0grids.source(),
                "guardrails": {"primary": PRIMARY.name, "comparison": [g.name for g in COMPARISON]},
                "frozen_constants_pending_r5": list(PENDING_R5),
                "approximations": [
                    "guardrail counters reset at internal quarter starts in train windows (continuous selection "
                    "path; accepted by the R1 ruling for R-based selection only; finalists re-checked exactly)",
                    "OOS-fold and holdout paths are exact: counters and equity ($100k) start at each window start"],
                "adj_factors": adj_note, "started_at_ct": started_ct,
                "pass2_started_at_ct": pd.Timestamp.now(tz="America/Chicago").isoformat(),   # after pass 1 + bars
                **engine_stamp_fields(Path(__file__).resolve().parents[3]),
                "legacy_target_gap": legacy["target_gap"], "legacy_halfday": legacy["halfday"]}
    if fb_stamp:
        # F/B only. An A-only manifest keeps engine_spec from engine_stamp_fields (v1.3.3 fallback on this tree).
        manifest["spec_doc"] = SPEC_DOC_FB
        manifest["spec_doc_commit"] = SPEC_DOC_COMMIT_FB
        manifest["engine_spec"] = ENGINE_SPEC_FB
        manifest["engine_spec_source"] = (
            "F/B stamp spec_doc v1.3.5 (8504fd7c19136141a32746234b63dd084106369d); "
            "engine_spec v1.3.5. engine_commit stays the git tree of research/intraday_sr/engine."
        )
        manifest["dsr_by_test"] = {}
        inp.spec_doc = SPEC_DOC_FB
        inp.spec_doc_commit = SPEC_DOC_COMMIT_FB
        inp.engine_spec = ENGINE_SPEC_FB
    if attribution:
        manifest["not_for_cp4"] = NOT_FOR_CP4
    mode = "per-symbol chunks" if not a.no_chunk else "per-variant (no-chunk)"
    frontier, notes = [], [f"adapter: {type(ad).__name__}; workers {a.workers} (pass 2: {p2w}); {mode}", adj_note]
    for test, variants, gsha in plan:
        inp.grid_sha = gsha
        tp = time.time()
        timed = _par(_timed_run_variant, variants, p2w)
        results = [r for r, _, _ in timed]
        if not a.allow_empty:
            _empty_portfolio(test, results)
        timing["pass2"][test] = {"wall_s": time.time() - tp,
                                 "per_variant": {r[0].variant_id: {"secs": sec, "maxrss_mb": mx}
                                                 for r, sec, mx in timed}}
        if a.timing_only:
            continue
        runs = {vr.variant_id: vr for vr, _, _ in results}
        manifest["grids"][test] = gsha
        tf = TradesFile(out / f"trades_{test}.parquet")
        for vid, vr in runs.items():                      # every variant: dev path + exact OOS fold paths
            tf.add(vr.trades, path="dev", variant_id=vid, guardrail=vr.config)
            tf.add(vr.exact_oos_trades, path="oos_exact", variant_id=vid, guardrail=vr.config)
        inp.errored = {**inp.errored, test: pd.DataFrame([{"variant_id": vid, "error": vr.error}
                                                          for vid, vr in sorted(runs.items()) if vr.status != "ok"],
                                                         columns=["variant_id", "error"])}

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
                    symbols=" ".join(syms), n_symbols=len(syms), grid_family=grid_family_for(test))
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
        dsr_floor = FORMATION_DSR_N if test in FORMATION_TESTS else None
        h = RO.headline(wf.oos_trades if len(wf.oos_trades) else pd.DataFrame(columns=["session", "r", "pnl"]),
                        wf.oos_daily, inp.n_dsr, var_sr=_trial_sr_var(runs), dsr_floor=dsr_floor)
        if fb_stamp:
            manifest["dsr_by_test"][test] = FORMATION_DSR_N if test in FORMATION_TESTS else inp.n_dsr
        ec, eflag = exact_finalist_check(runs, wf.fold_table, wf.finalists, exact_window)
        inp.exact_checks = {**inp.exact_checks, test: (ec, eflag)}
        manifest.setdefault("exact_finalist_check", {})[test] = {"cp4_flag": bool(eflag), "rows": int(len(ec))}
        inp.headlines = {**inp.headlines, test: h}
        inp.r7 = {**inp.r7, test: _r7_block(runs, wf.oos_trades)}
        fc = wf.fold_criterion
        inp.fold_83 = {**inp.fold_83, test: {k: fc[k] for k in (
            "n_folds", "n_positive", "n_non_positive", "n_unselected", "share_positive", "passes")}}
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
        tf.add(tp_, path="selected", guardrail=PRIMARY.name)
        gstats = [RO.guardrail_stats(PRIMARY.name, tp_, dp_, prim_cnt)]
        crow = []
        for gcfg in COMPARISON:
            tg, dg, cnt = selected_path(wf.picks, vmap, gcfg)
            tf.add(tg, path="selected", guardrail=gcfg.name)
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
        tf.close()
        manifest.setdefault("trades_files", {})[test] = {"file": tf.path.name, "rows": int(tf.rows)}
        inp.guardrails = {**inp.guardrails, f"Test {test} selected path":
                          (RO.guardrail_rows(gstats), RO.trades_per_month_words(gstats))}
        (out / f"wf_{test}.json").write_text(json.dumps(
            {"picks": wf.picks, "finalists": wf.finalists, "fold_criterion": wf.fold_criterion},
            default=str, indent=1))
    timing["total_wall_s"] = time.time() - t0
    timing["parent_maxrss_mb"] = _maxrss_mb()
    timing["children_maxrss_mb"] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    (out / "timing.json").write_text(json.dumps(timing, indent=1, default=str))
    if a.timing_only:
        _drop_spill(out, a.keep_signals)
        print(out / "timing.json")
        return timing
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
    _drop_spill(out, a.keep_signals)
    print(p)
    print(notes[-1])


if __name__ == "__main__":
    main()
