"""Walk-forward, selection, finalists, FREEZE and the one-time holdout (SPEC section 6, v1.1).

Each variant runs ONCE as a continuous account path over the development window (2019-01-02 -> 2026-03-31,
signals from 2019-02-01); folds slice its trades and daily returns. Selection per fold: highest train mean R
per trade (after costs) among variants with >= 200 train trades; ties -> more trades, then variant id. The
selected variant's test-quarter trades, concatenated, are the headline OOS result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from research.intraday_sr.harness.config import PRIMARY_LABEL, Fold

CT = ZoneInfo("America/Chicago")
DEV_START, DEV_END = date(2019, 1, 2), date(2026, 3, 31)
TRADE_FROM = date(2019, 2, 1)
HOLDOUT_START, HOLDOUT_END = date(2026, 4, 1), date(2026, 9, 30)
FINALIST_RESELECT = (date(2025, 4, 1), date(2026, 3, 31))
MIN_TRAIN_TRADES = 200
N_SYMBOLS_REQUIRED = 33


class HoldoutRefused(RuntimeError):
    pass


def _window_starts() -> frozenset:
    out = {TRADE_FROM, HOLDOUT_START}
    for k, q in enumerate(pd.period_range("2020Q1", "2026Q1", freq="Q"), start=1):
        out.add(q.start_time.date())
        out.add(max((q.start_time - pd.DateOffset(months=12)).date(), DEV_START))
    return frozenset(out)


def make_folds() -> list[Fold]:
    """25 folds: test quarters 2020Q1 .. 2026Q1, train = the 12 months before."""
    out = []
    for k, q in enumerate(pd.period_range("2020Q1", "2026Q1", freq="Q"), start=1):
        ts, te = q.start_time.date(), q.end_time.date()
        tr0 = (q.start_time - pd.DateOffset(months=12)).date()
        out.append(Fold(k, max(tr0, DEV_START), (q.start_time - pd.Timedelta(days=1)).date(), ts, te, TRADE_FROM))
    return out


class ComparisonConfigInSelection(RuntimeError):
    """SPEC v1.3 G2/G8: selection, finalists, freeze and the holdout read only the primary configuration."""


def _assert_primary(runs: Mapping[str, "VariantRun"]) -> None:
    bad = sorted({r.config for r in runs.values() if r.config != PRIMARY_LABEL})
    if bad:
        raise ComparisonConfigInSelection(f"selection received non-primary runs {bad}; only {PRIMARY_LABEL} allowed")


@dataclass
class VariantRun:
    """One variant's continuous development-window path. trades: DataFrame with at least
    session (date), r, pnl, symbol. daily: Series of daily returns indexed by session date."""
    variant_id: str
    trades: pd.DataFrame
    daily: pd.Series
    status: str = "ok"
    error: str = ""
    config: str = PRIMARY_LABEL          # guardrail label of the RiskCfg used; selection accepts only the primary


def _win(df: pd.DataFrame, a: date, b: date) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return df
    s = pd.to_datetime(df["session"]).dt.date
    return df[(s >= a) & (s <= b)]


def _swin(s: pd.Series, a: date, b: date) -> pd.Series:
    idx = pd.to_datetime(s.index).date
    return s[(idx >= a) & (idx <= b)]


def select(runs: Mapping[str, VariantRun], a: date, b: date) -> tuple[Optional[str], dict]:
    _assert_primary(runs)
    cands = []
    for vid, run in runs.items():
        if run.status != "ok":
            continue
        t = _win(run.trades, a, b)
        n = 0 if t is None else len(t)
        if n >= MIN_TRAIN_TRADES:
            cands.append((round(float(t["r"].mean()), 10), n, vid))   # rounding: float noise is not a tie-break
    if not cands:
        return None, {"eligible": 0}
    cands.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return cands[0][2], {"eligible": len(cands), "train_mean_r": cands[0][0], "train_trades": cands[0][1]}


@dataclass
class WFResult:
    picks: list
    oos_trades: pd.DataFrame
    oos_daily: pd.Series
    fold_table: pd.DataFrame          # per (variant, fold): test mean R, trades (for finalist rule + trial log)
    finalists: dict = field(default_factory=dict)


def walk_forward(runs: Mapping[str, VariantRun], folds: Sequence[Fold] | None = None) -> WFResult:
    _assert_primary(runs)
    folds = list(folds or make_folds())
    picks, tparts, dparts, rows = [], [], [], []
    for f in folds:
        vid, info = select(runs, max(f.train_start, f.trade_from or f.train_start), f.train_end)
        picks.append({"fold": f.k, "test": [f.test_start.isoformat(), f.test_end.isoformat()], "variant": vid, **info})
        for v, run in runs.items():
            if run.status != "ok":
                continue
            tt = _win(run.trades, f.test_start, f.test_end)
            tr = _win(run.trades, max(f.train_start, f.trade_from or f.train_start), f.train_end)
            rows.append({"variant_id": v, "fold": f.k, "test_trades": len(tt), "test_mean_r": float(tt["r"].mean()) if len(tt) else np.nan,
                         "train_trades": len(tr), "train_mean_r": float(tr["r"].mean()) if len(tr) else np.nan})
        if vid is None:
            # no eligible variant: the fold is flat (no trades), recorded as such
            sessions = sorted({d for run in runs.values() for d in pd.to_datetime(run.daily.index).date
                               if f.test_start <= d <= f.test_end})
            dparts.append(pd.Series(0.0, index=pd.Index(sessions)))
            continue
        run = runs[vid]
        t = _win(run.trades, f.test_start, f.test_end)
        if len(t):
            tparts.append(t.assign(fold=f.k, variant_id=vid))
        dparts.append(_swin(run.daily, f.test_start, f.test_end))
    oos_t = pd.concat(tparts, ignore_index=True) if tparts else pd.DataFrame()
    oos_d = pd.concat(dparts) if dparts else pd.Series(dtype=float)
    ft = pd.DataFrame(rows)
    return WFResult(picks, oos_t, oos_d, ft, finalists(runs, ft))


def finalists(runs: Mapping[str, VariantRun], fold_table: pd.DataFrame) -> dict:
    _assert_primary(runs)
    proc, info = select(runs, *FINALIST_RESELECT)
    out = {"procedure": {"variant": proc, "window": [d.isoformat() for d in FINALIST_RESELECT], **info}}
    if len(fold_table):
        ft = fold_table.copy()
        ft["rank"] = ft.groupby("fold")["test_mean_r"].rank(ascending=False, method="min")
        g = ft.groupby("variant_id")
        share_pos = g["test_mean_r"].apply(lambda s: float((s > 0).sum()) / max(s.notna().sum(), 1))
        med_rank = g["rank"].median()
        ok = share_pos[share_pos >= 0.60].index
        if len(ok):
            best = med_rank.loc[ok].sort_values(kind="stable")
            v = best.index[0]
            out["stable"] = {"variant": v, "median_fold_rank": float(best.iloc[0]),
                             "share_positive_folds": float(share_pos[v])}
        else:
            out["stable"] = {"variant": None, "reason": "no variant positive in >= 60% of folds"}
    return out


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_freeze(path: Path, finalists_by_test: dict, triallog_path: Path, git_sha: str, grid_sha: str,
                 spec_version: str, config: str = PRIMARY_LABEL) -> str:
    if config != PRIMARY_LABEL:
        raise ComparisonConfigInSelection(f"FREEZE must use the primary configuration {PRIMARY_LABEL}, got {config}")
    lines = ["# FREEZE: intraday S/R finalists (written before the holdout)", "",
             f"- Written: {datetime.now(CT).isoformat(timespec='seconds')}",
             f"- Spec version: {spec_version}", f"- Configuration: {PRIMARY_LABEL} (primary loss guardrail, G1)", f"- Grid sha256: `{grid_sha}`", f"- Git sha: `{git_sha}`",
             f"- Trial log: `{triallog_path}` sha256 `{sha256_file(triallog_path)}`", "",
             "```json", json.dumps(finalists_by_test, indent=1, default=str), "```", ""]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines))
    return sha256_file(path)


def run_holdout(freeze_path: Path, lock_path: Path, symbols_complete: Sequence[str],
                scorer: Callable[[dict], dict], finalists_by_test: dict) -> dict:
    """Opens the holdout exactly once. Refuses without FREEZE.md, with < 33 complete symbols, or if
    holdout.lock exists (a second opening is a protocol breach)."""
    freeze_path, lock_path = Path(freeze_path), Path(lock_path)
    if not freeze_path.exists():
        raise HoldoutRefused("FREEZE.md missing")
    if len(set(symbols_complete)) < N_SYMBOLS_REQUIRED:
        raise HoldoutRefused(f"only {len(set(symbols_complete))} of {N_SYMBOLS_REQUIRED} symbols complete")
    if lock_path.exists():
        raise HoldoutRefused(f"holdout.lock exists ({lock_path.read_text()[:200]}); a second opening is a protocol breach")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "x") as fh:            # atomic create; races also refuse
        fh.write(json.dumps({"opened_at_ct": datetime.now(CT).isoformat(timespec="seconds"),
                             "freeze_sha256": sha256_file(freeze_path)}))
    return scorer(finalists_by_test)


# G3.10: the week loss counter restarts at every simulated window start (fold train/test starts, trading start,
# holdout start). Days are not affected (the day counter resets every session anyway). Dates that are not
# sessions are matched against the first session on/after them by the simulator.
WINDOW_STARTS = _window_starts()
