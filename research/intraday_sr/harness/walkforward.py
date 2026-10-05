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
    # exact fixed-variant OOS path: each fold test window simulated from its own start (counters 0, $100k)
    exact_oos_trades: Optional[pd.DataFrame] = None
    exact_oos_daily: Optional[pd.Series] = None


def _win(df: pd.DataFrame, a: date, b: date) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return df
    s = pd.to_datetime(df["session"]).dt.date
    return df[(s >= a) & (s <= b)]


def _swin(s: pd.Series, a: date, b: date) -> pd.Series:
    idx = pd.to_datetime(s.index).date
    return s[(idx >= a) & (idx <= b)]


R_METRICS = ("mean_r",)          # R1 ruling (a): selection reads R only (mean R, CI, trade counts)
DOLLAR_PCT_COLUMNS = ("pnl", "ret", "return", "monthly", "equity", "dollar", "pct")


class SelectionMetricError(ValueError):
    """Selection may only use R-based metrics; $ and % returns come from the exact OOS-fold / holdout paths."""


def _r_view(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """The only columns selection code ever sees: session and R."""
    if df is None or len(df) == 0:
        return pd.DataFrame({"session": pd.Series(dtype=object), "r": pd.Series(dtype=float)})
    return df[["session", "r"]]


def rank_table(runs: Mapping[str, VariantRun], a: date, b: date, metric: str = "mean_r") -> pd.DataFrame:
    """Selection ranking on [a, b]: eligible = >= 200 trades; ordered by mean R desc, then trades desc, then id."""
    if metric not in R_METRICS or any(k in metric.lower() for k in DOLLAR_PCT_COLUMNS):
        raise SelectionMetricError(f"selection metric {metric!r} is not R-based; allowed: {R_METRICS}")
    _assert_primary(runs)
    rows = []
    for vid, run in runs.items():
        if run.status != "ok":
            continue
        t = _win(_r_view(run.trades), a, b)
        n = 0 if t is None else len(t)
        rows.append({"variant_id": vid, "trades": n, "mean_r": round(float(t["r"].mean()), 10) if n else np.nan,
                     "eligible": n >= MIN_TRAIN_TRADES})     # rounding: float noise is not a tie-break
    df = pd.DataFrame(rows, columns=["variant_id", "trades", "mean_r", "eligible"])
    el = df[df["eligible"]].sort_values(["mean_r", "trades", "variant_id"], ascending=[False, False, True],
                                         kind="stable")
    df["rank"] = df["variant_id"].map({v: i + 1 for i, v in enumerate(el["variant_id"])})
    return df.sort_values(["rank", "variant_id"], na_position="last").reset_index(drop=True)


def select(runs: Mapping[str, VariantRun], a: date, b: date, metric: str = "mean_r") -> tuple[Optional[str], dict]:
    rt = rank_table(runs, a, b, metric)
    el = rt[rt["eligible"]]
    if not len(el):
        return None, {"eligible": 0}
    top = el.iloc[0]
    return top["variant_id"], {"eligible": int(len(el)), "train_mean_r": float(top["mean_r"]),
                               "train_trades": int(top["trades"])}


@dataclass
class WFResult:
    picks: list
    oos_trades: pd.DataFrame
    oos_daily: pd.Series
    fold_table: pd.DataFrame          # per (variant, fold): test mean R, trades (for finalist rule + trial log)
    finalists: dict = field(default_factory=dict)
    oos_exact: bool = False           # True when OOS fold paths were re-simulated exactly from each fold start


ExactWindow = Callable[[str, date, date], VariantRun]


def walk_forward(runs: Mapping[str, VariantRun], folds: Sequence[Fold] | None = None,
                 exact_window: Optional[ExactWindow] = None) -> WFResult:
    """Per-fold selection on train windows (continuous path; guardrail week counters reset at window starts,
    including internal quarter starts of a train window, accepted by the R1 ruling for R-based selection only).
    OOS: with `exact_window`, each fold's test path is re-simulated from the fold start (counters 0, equity
    $100k); $ and % results come only from those exact paths."""
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
        run = exact_window(vid, f.test_start, f.test_end) if exact_window is not None else runs[vid]
        t = _win(run.trades, f.test_start, f.test_end)
        if len(t):
            tparts.append(t.assign(fold=f.k, variant_id=vid))
        dparts.append(_swin(run.daily, f.test_start, f.test_end))
    oos_t = pd.concat(tparts, ignore_index=True) if tparts else pd.DataFrame()
    oos_d = pd.concat(dparts) if dparts else pd.Series(dtype=float)
    ft = pd.DataFrame(rows)
    return WFResult(picks, oos_t, oos_d, ft, finalists(runs, ft), oos_exact=exact_window is not None)


def finalists(runs: Mapping[str, VariantRun], fold_table: pd.DataFrame) -> dict:
    _assert_primary(runs)
    proc, info = select(runs, *FINALIST_RESELECT, metric="mean_r")
    out = {"procedure": {"variant": proc, "window": [d.isoformat() for d in FINALIST_RESELECT], **info}}
    if len(fold_table):
        best = stable_order(fold_table)
        share_pos = _share_pos(fold_table)
        if len(best):
            v = best.index[0]
            out["stable"] = {"variant": v, "median_fold_rank": float(best.iloc[0]),
                             "share_positive_folds": float(share_pos[v])}
        else:
            out["stable"] = {"variant": None, "reason": "no variant positive in >= 60% of folds"}
    return out


def _share_pos(fold_table: pd.DataFrame) -> pd.Series:
    return fold_table.groupby("variant_id")["test_mean_r"].apply(
        lambda s: float((s > 0).sum()) / max(s.notna().sum(), 1))


def stable_order(fold_table: pd.DataFrame) -> pd.Series:
    """Variants positive in >= 60% of folds, ordered by median fold rank of test mean R (R-based)."""
    if not len(fold_table):
        return pd.Series(dtype=float)
    ft = fold_table[["variant_id", "fold", "test_mean_r"]].copy()
    ft["rank"] = ft.groupby("fold")["test_mean_r"].rank(ascending=False, method="min")
    med = ft.groupby("variant_id")["rank"].median()
    sp = _share_pos(fold_table)
    ok = sp[sp >= 0.60].index
    return med.loc[ok].sort_values(kind="stable")


def exact_finalist_check(runs: Mapping[str, VariantRun], fold_table: pd.DataFrame, fin: dict,
                         exact_window: ExactWindow, k_next: int = 3, tol_r: float = 0.01) -> tuple[pd.DataFrame, bool]:
    """R1 ruling (c): each finalist plus the next `k_next` by rank, re-simulated exactly on the finalist train
    window (FINALIST_RESELECT; counters and equity start at the window start, no internal resets). Reports the
    change in the selection metric (train mean R) and in rank (the reselect ranking with exact values
    substituted for the re-simulated variants). CP4 flag: any finalist's rank changes or |delta mean R| > tol_r."""
    a, b = FINALIST_RESELECT
    approx = rank_table(runs, a, b)
    order = list(approx.loc[approx["eligible"], "variant_id"])
    cands: list[tuple[str, str]] = []
    proc = (fin.get("procedure") or {}).get("variant")
    if proc:
        i = order.index(proc)
        cands += [("procedure finalist", proc)] + [(f"procedure next {j}", v) for j, v in
                                                     enumerate(order[i + 1:i + 1 + k_next], 1)]
    stab = (fin.get("stable") or {}).get("variant")
    if stab:
        so = list(stable_order(fold_table).index)
        i = so.index(stab)
        cands += [("stable finalist", stab)] + [(f"stable next {j}", v) for j, v in enumerate(so[i + 1:i + 1 + k_next], 1)]
    exact = {}
    for _, v in cands:
        if v not in exact:
            r = _win(_r_view(exact_window(v, a, b).trades), a, b)
            exact[v] = (len(r), round(float(r["r"].mean()), 10) if len(r) else np.nan)
    sub = approx.copy()
    for v, (n, m) in exact.items():
        sub.loc[sub["variant_id"] == v, ["trades", "mean_r"]] = [n, m]
    sub["eligible"] = sub["trades"] >= MIN_TRAIN_TRADES
    el = sub[sub["eligible"]].sort_values(["mean_r", "trades", "variant_id"], ascending=[False, False, True],
                                          kind="stable")
    ex_rank = {v: i + 1 for i, v in enumerate(el["variant_id"])}
    ap = approx.set_index("variant_id")
    rows, flag = [], False
    for role, v in cands:
        ar, er = ap.loc[v, "rank"], ex_rank.get(v, np.nan)
        dm = exact[v][1] - ap.loc[v, "mean_r"]
        rank_changed = not (pd.notna(ar) and pd.notna(er) and int(ar) == int(er))
        f = "finalist" in role and (rank_changed or not (abs(dm) <= tol_r))
        flag |= bool(f)
        rows.append({"role": role, "variant_id": v, "approx_mean_r": ap.loc[v, "mean_r"], "exact_mean_r": exact[v][1],
                     "delta_mean_r": dm, "approx_trades": int(ap.loc[v, "trades"]), "exact_trades": exact[v][0],
                     "approx_rank": ar, "exact_rank": er,
                     "delta_rank": (er - ar) if pd.notna(ar) and pd.notna(er) else np.nan, "cp4_flag": bool(f)})
    return pd.DataFrame(rows), flag


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_freeze(path: Path, finalists_by_test: dict, ledger, git_sha: str, grid_sha: str,
                 spec_version: str, config: str = PRIMARY_LABEL) -> str:
    """ledger: the program TrialLog. N = max(450, cumulative program trials at this freeze)."""
    if config != PRIMARY_LABEL:
        raise ComparisonConfigInSelection(f"FREEZE must use the primary configuration {PRIMARY_LABEL}, got {config}")
    now = datetime.now(CT).isoformat(timespec="microseconds")
    n_prog = ledger.program_trial_count(ledger.read(), now)
    lines = ["# FREEZE: intraday S/R finalists (written before the holdout)", "",
             f"- Written: {now}",
             f"- Spec version: {spec_version}", f"- Configuration: {PRIMARY_LABEL} (primary loss guardrail, G1)", f"- Grid sha256: `{grid_sha}`", f"- Git sha: `{git_sha}`",
             f"- Program ledger: `{ledger.path}` ({len(ledger.parts())} parts) sha256 `{ledger.sha256()}`",
             f"- Program trials at freeze: {n_prog}; DSR N = max(450, {n_prog}) = {max(450, n_prog)}", "",
             "```json", json.dumps(finalists_by_test, indent=1, default=str), "```", ""]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines))
    return sha256_file(path)


def run_holdout(freeze_path: Path, lock_path: Path, symbols_complete: Sequence[str],
                scorer: Callable[[dict, object], dict], finalists_by_test: dict) -> dict:
    """Opens the holdout exactly once and is the ONLY place that constructs a HoldoutToken (SPEC v1.3.1 C2).

    Refuses without FREEZE.md, with < 33 complete symbols, or if holdout.lock exists (a second opening is a protocol
    breach). Then creates holdout.lock, builds the token via HoldoutToken._from_freeze (caller-name gated to this
    function) and calls scorer(finalists_by_test, token). Loaders default to end=2026-03-31 and require the token
    for any bar on or after 2026-04-01.
    """
    from research.intraday_sr.data.holdout import HoldoutLocked, HoldoutToken
    freeze_path, lock_path = Path(freeze_path), Path(lock_path)
    if not freeze_path.is_file():
        raise HoldoutRefused("FREEZE.md missing")
    if len(set(symbols_complete)) < N_SYMBOLS_REQUIRED:
        raise HoldoutRefused(f"only {len(set(symbols_complete))} of {N_SYMBOLS_REQUIRED} symbols complete")
    if lock_path.exists():
        raise HoldoutRefused(f"holdout.lock exists ({lock_path.read_text()[:200]}); a second opening is a protocol breach")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "x") as fh:            # atomic create; races also refuse
        fh.write(json.dumps({"opened_at_ct": datetime.now(CT).isoformat(timespec="seconds"),
                             "freeze_sha256": sha256_file(freeze_path)}))
    try:
        token = HoldoutToken._from_freeze(freeze_path)   # must be called from run_holdout (D2-1 gate)
    except HoldoutLocked as e:
        raise HoldoutRefused(str(e)) from e
    return scorer(finalists_by_test, token)


# G3.10: the week loss counter restarts at every simulated window start (fold train/test starts, trading start,
# holdout start). Days are not affected (the day counter resets every session anyway). Dates that are not
# sessions are matched against the first session on/after them by the simulator.
WINDOW_STARTS = _window_starts()
