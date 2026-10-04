"""Raw/adjusted session factors for as-traded prices (costs, $0.01 tick/min-per-share, option strikes and premium).

THIN ADAPTER (R1 ruling 4). Agreed contract with Developer 2: `Bar.adj_factor` = raw / adjusted, i.e.
as_traded = adjusted * Bar.adj_factor. Their `research.intraday_sr.data.adjust.load_adj_factors()` will invert
Trading's file into that direction. `load_adj_factors()` below has the same semantics (Series indexed by
(symbol, session) of raw/adj) and delegates to Developer 2's function as soon as it is importable (#20 / the d2
branch); until then the local implementation is used. Everything else in the harness calls only this module.

Trading's files (when present):
    <root>/adj_factors/<SYMBOL>.parquet   (BRK.B stored as BRK-B)   columns: date (NY), raw_close, adj_close, adj_factor
    <root>/adj_factors/adj_factors.parquet or <root>/adj_factors.parquet   (combined; adds a symbol column)
Trading's adj_factor = adj_close / raw_close, i.e. as_traded = adjusted / adj_factor.

The HARNESS convention (same as S0 `Bar.adj_factor` and `data/adjust.py`) is the inverse: f = raw / adj, so
as_traded = adjusted * f. This module converts: f = raw_close / adj_close (or 1 / adj_factor if the closes are
absent). Joined on the session date only; a missing file or session falls back to f = 1.0 and is reported as
APPROXIMATE (never borrowed from a neighbouring session).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

DEFAULT_ROOT = Path("/workspace/research2/data/alpaca_intraday/m5rth_fixed33")


def _file_symbol(symbol: str) -> str:
    return symbol.replace(".", "-")


def _to_harness(df: pd.DataFrame) -> pd.Series:
    d = pd.to_datetime(df["date"]).dt.date
    if {"raw_close", "adj_close"} <= set(df.columns):
        f = df["raw_close"].astype(float) / df["adj_close"].astype(float)
    elif "adj_factor" in df.columns:
        f = 1.0 / df["adj_factor"].astype(float)          # Trading's adj/raw -> harness raw/adj
    else:
        raise ValueError("adj factor file needs raw_close+adj_close or adj_factor")
    s = pd.Series(f.to_numpy(float), index=pd.Index(d, name="session"), name="adj_factor")
    s = s[np.isfinite(s) & (s > 0)]
    return s[~s.index.duplicated(keep="last")]


def _dev2_loader():
    try:
        from research.intraday_sr.data.adjust import load_adj_factors as f   # Developer 2 (after #20 / d2 lands)
        return f
    except Exception:  # noqa: BLE001
        return None


def _local_load_all(root: Path) -> pd.Series:
    parts = []
    comb = [p for p in (root / "adj_factors" / "adj_factors.parquet", root / "adj_factors.parquet") if p.is_file()]
    if comb:
        df = pd.read_parquet(comb[0])
        for sym, g in df.groupby(df["symbol"].astype(str)):
            s = _to_harness(g)
            parts.append(pd.Series(s.to_numpy(), index=pd.MultiIndex.from_arrays(
                [[sym.replace("-", ".")] * len(s), list(s.index)], names=["symbol", "session"])))
    else:
        for p in sorted((root / "adj_factors").glob("*.parquet")):
            s = _to_harness(pd.read_parquet(p))
            sym = p.stem.replace("-", ".")
            parts.append(pd.Series(s.to_numpy(), index=pd.MultiIndex.from_arrays(
                [[sym] * len(s), list(s.index)], names=["symbol", "session"])))
    if not parts:
        return pd.Series(dtype=float, index=pd.MultiIndex.from_arrays([[], []], names=["symbol", "session"]),
                         name="adj_factor")
    out = pd.concat(parts)
    out.name = "adj_factor"
    return out


_CACHE: dict = {}
SOURCE: dict = {}          # root -> "dev2" | "local"


def _normalize(out) -> pd.Series:
    """Accept Developer 2's return shape: a (symbol, session) Series, or a frame with symbol, date/session and
    adj_factor columns. Values are taken AS raw/adj (agreed contract); no inversion here."""
    if isinstance(out, pd.DataFrame):
        dcol = "session" if "session" in out.columns else "date"
        out = pd.Series(out["adj_factor"].astype(float).to_numpy(), index=pd.MultiIndex.from_arrays(
            [out["symbol"].astype(str).str.replace("-", ".").to_numpy(),
             pd.to_datetime(out[dcol]).dt.date.to_numpy()], names=["symbol", "session"]))
    if not isinstance(out, pd.Series) or out.index.nlevels != 2:
        raise TypeError("load_adj_factors must give a (symbol, session) indexed series")
    out = out.astype(float)
    out.index = out.index.set_names(["symbol", "session"])
    out.name = "adj_factor"
    return out


def load_adj_factors(root: Path | str = DEFAULT_ROOT) -> pd.Series:
    """Series indexed by (symbol, session date) of raw/adj (= Bar.adj_factor). Developer 2's loader if present."""
    root = Path(root)
    key = str(root.resolve()) if root.exists() else str(root)
    if key in _CACHE:
        return _CACHE[key]
    dev2 = _dev2_loader()
    out = None
    if dev2 is not None:
        for call in (lambda: dev2(root), lambda: dev2(root / "adj_factors" / "adj_factors.parquet"), dev2):
            try:
                out = _normalize(call())
                break
            except TypeError:
                continue
    SOURCE[key] = "dev2" if out is not None else "local"
    if out is None:
        out = _local_load_all(root)
    _CACHE[key] = out
    return out


def load_factors(symbol: str, root: Path | str = DEFAULT_ROOT) -> Optional[pd.Series]:
    """Series session -> f (raw/adj) for one symbol, or None if no factor covers it."""
    allf = load_adj_factors(root)
    if len(allf):
        syms = set(allf.index.get_level_values(0))
        for cand in (symbol, symbol.replace(".", "-"), symbol.replace("-", ".")):
            if cand in syms:
                return allf.xs(cand, level=0)
        return None
    return _load_factors_files(symbol, root)


def _load_factors_files(symbol: str, root: Path | str = DEFAULT_ROOT) -> Optional[pd.Series]:
    root = Path(root)
    p = root / "adj_factors" / f"{_file_symbol(symbol)}.parquet"
    if p.is_file():
        return _to_harness(pd.read_parquet(p))
    for comb in (root / "adj_factors" / "adj_factors.parquet", root / "adj_factors.parquet"):
        if comb.is_file():
            df = pd.read_parquet(comb)
            if "symbol" in df.columns:
                sub = df[df["symbol"].astype(str).isin({symbol, _file_symbol(symbol)})]
                if len(sub):
                    return _to_harness(sub)
    return None


@dataclass
class AdjInfo:
    source: dict = field(default_factory=dict)            # symbol -> "file" | "approx_1.0"
    approx_sessions: dict = field(default_factory=dict)   # symbol -> sessions defaulted to 1.0

    @property
    def approximate(self) -> bool:
        return any(v for v in self.approx_sessions.values()) or \
            any(v not in ("file", "frame") for v in self.source.values())

    def note(self) -> str:
        if not self.approximate:
            return "as-traded prices from Trading's raw/adjusted factors (all sessions covered)"
        miss = {s: n for s, n in self.approx_sessions.items() if n}
        return ("APPROXIMATE as-traded prices: raw/adj factor defaulted to 1.0 for "
                + ", ".join(f"{s} ({n} sessions)" for s, n in miss.items()) + " (costs, min $/share, option strikes)")


def attach(frame: pd.DataFrame, symbol: str, root: Path | str = DEFAULT_ROOT, info: Optional[AdjInfo] = None
           ) -> pd.DataFrame:
    """Return `frame` with an `adj_factor` column (raw/adj) joined on `session`; 1.0 where unknown."""
    info = info if info is not None else AdjInfo()
    f = load_factors(symbol, root)
    sess = pd.Series(frame["session"].to_numpy())
    if f is None:
        info.source[symbol] = "approx_1.0"
        info.approx_sessions[symbol] = int(sess.nunique())
        return frame.assign(adj_factor=1.0)
    vals = sess.map(f)
    info.source[symbol] = "file"
    info.approx_sessions[symbol] = int(sess[vals.isna()].nunique())
    return frame.assign(adj_factor=vals.fillna(1.0).to_numpy(float))


def option_factor_map(symbols: Iterable[str], sessions: Iterable[date], root: Path | str = DEFAULT_ROOT,
                      info: Optional[AdjInfo] = None) -> dict:
    """{(symbol, session): f} for IVContext.adj_factor (missing -> absent -> IVContext.f() returns 1.0)."""
    info = info if info is not None else AdjInfo()
    sessions = list(sessions)
    out = {}
    for sym in symbols:
        f = load_factors(sym, root)
        if f is None:
            info.source[sym] = "approx_1.0"
            info.approx_sessions[sym] = len(sessions)
            continue
        info.source[sym] = "file"
        miss = 0
        for d in sessions:
            v = f.get(d)
            if v is None or not np.isfinite(v):
                miss += 1
            else:
                out[(sym, d)] = float(v)
        info.approx_sessions[sym] = miss
    return out


class FactorCoverageError(RuntimeError):
    """Real (non-smoke) runs require Trading's factor for every (symbol, session) in the run."""


def coverage_gaps(symbol: str, sessions: Iterable[date], root: Path | str = DEFAULT_ROOT) -> list[date]:
    f = load_factors(symbol, root)
    sessions = sorted(set(sessions))
    if f is None:
        return sessions
    return [d for d in sessions if d not in f.index]


def require_coverage(sessions_by_symbol: dict, root: Path | str = DEFAULT_ROOT) -> None:
    bad = {}
    for sym, sess in sessions_by_symbol.items():
        g = coverage_gaps(sym, sess, root)
        if g:
            bad[sym] = (len(g), g[0], g[-1])
    if bad:
        raise FactorCoverageError("raw/adj factor missing (real runs refuse; 1.0 fallback is smoke-only): "
                                  + "; ".join(f"{s}: {n} sessions {a}..{b}" for s, (n, a, b) in bad.items()))
