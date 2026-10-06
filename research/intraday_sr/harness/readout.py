"""READOUT.md generator (SPEC section 9 + v1.1 A1-A3). Pure formatting over harness outputs; no selection here.

Sections: header (trial count, grid hash, INTERIM label), per-test headline (trades/day next to edge metrics),
pass-bar checks in words, per-year / per-symbol tables, trade-off frontier (+ PNG) with the 150-250 trades/mo flag,
options baseline table, 0DTE table (higher-risk, model-based, low-confidence caveat), guardrail overlay table.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from research.intraday_sr.harness import stats as S
from research.intraday_sr.harness.config import N_PROGRAM

OOS_START, OOS_END = date(2020, 1, 1), date(2026, 3, 31)
FLAG_LO, FLAG_HI = 150, 250
ZERO_DTE_CAVEAT = ("0DTE: higher-risk scenario, model-based (no OPRA data). VIX9D-based IV understates near-expiry "
                   "skew and gamma, so these numbers are low-confidence.")


def _pct(x, nd=2):
    return "n/a" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{100 * x:.{nd}f}%"


def _num(x, nd=3):
    return "n/a" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:.{nd}f}"


def _ci(d, f=_num):
    if not d or d.get("mean") is None:
        return "n/a"
    return f"{f(d['mean'])} [{f(d['lo'])}, {f(d['hi'])}]"


def _md(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "_(none)_\n"
    cols = list(df.columns)
    out = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(str(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(out) + "\n"


def _window(trades: pd.DataFrame, daily: pd.Series, a=OOS_START, b=OOS_END):
    t = trades
    if t is not None and len(t):
        s = pd.to_datetime(t["session"]).dt.date
        t = t[(s >= a) & (s <= b)]
    di = pd.to_datetime(daily.index).date
    return t, daily[(di >= a) & (di <= b)]


# ------------------------------------------------------------------ frontier (v1.1 A1)
def _boot_ratio(num: np.ndarray, den: np.ndarray, seed=S.SEED, n=S.RESAMPLES):
    nb = len(num)
    if nb == 0 or den.sum() == 0:
        return {"mean": None, "lo": None, "hi": None}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, nb, size=(n, nb))
    d = den[idx].sum(1)
    v = np.where(d > 0, num[idx].sum(1) / np.maximum(d, 1e-12), np.nan)
    lo, hi = np.nanquantile(v, [0.025, 0.975])
    return {"mean": float(num.sum() / den.sum()), "lo": float(lo), "hi": float(hi)}


def frontier_row(test: str, variant_id: str, trades: pd.DataFrame, daily: pd.Series) -> dict:
    t, dly = _window(trades, daily)
    m = S.monthly_returns(dly)
    months = pd.PeriodIndex(m.index) if len(m) else pd.PeriodIndex([], freq="M")
    if t is None or len(t) == 0:
        cnt = np.zeros(len(months))
        mr, wr = {"mean": None}, {"mean": None}
    else:
        per = pd.to_datetime(t["session"]).dt.to_period("M")
        cnt = per.value_counts().reindex(months, fill_value=0).to_numpy(float)
        day = pd.to_datetime(t["session"]).dt.date.to_numpy()
        mr = S.day_block_mean_r(t["r"].to_numpy(float), day)
        codes, uniq = pd.factorize(pd.Series(day))
        wins = np.bincount(codes, weights=(t["pnl"].to_numpy(float) > 0).astype(float), minlength=len(uniq))
        n = np.bincount(codes, minlength=len(uniq)).astype(float)
        wr = _boot_ratio(wins, n)
    tpm = S.block_mean(cnt) if len(cnt) else {"mean": None}
    net = S.block_mean(m.to_numpy()) if len(m) else {"mean": None}
    flag = (tpm.get("mean") is not None and FLAG_LO <= tpm["mean"] <= FLAG_HI
            and mr.get("lo") is not None and mr["lo"] > 0)
    return {"test": test, "variant_id": variant_id, "trades_per_mo": tpm, "win_rate": wr, "mean_r": mr,
            "net_monthly": net, "flag_150_250_lbR_gt0": bool(flag)}


def frontier_table(rows: Sequence[dict]) -> pd.DataFrame:
    return pd.DataFrame([{
        "test": r["test"], "variant": r["variant_id"],
        "trades/mo [95% CI]": _ci(r["trades_per_mo"], lambda x: f"{x:.0f}"),
        "win rate": _ci(r["win_rate"], _pct), "avg R": _ci(r["mean_r"]),
        "net monthly": _ci(r["net_monthly"], _pct), "flag": "FLAG" if r["flag_150_250_lbR_gt0"] else "",
    } for r in sorted(rows, key=lambda r: -(r["trades_per_mo"].get("mean") or 0))])


def frontier_png(rows: Sequence[dict], path: Path) -> Optional[Path]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.5))
    for test, mk in (("A", "o"), ("B", "s"), ("F", "^")):
        rr = [r for r in rows if r["test"] == test and r["trades_per_mo"].get("mean") is not None
              and r["mean_r"].get("mean") is not None]
        if not rr:
            continue
        x = np.array([r["trades_per_mo"]["mean"] for r in rr])
        for k, key, scale in ((0, "mean_r", 1), (1, "net_monthly", 100)):
            y = np.array([r[key]["mean"] for r in rr]) * scale
            lo = np.array([r[key]["lo"] for r in rr]) * scale
            hi = np.array([r[key]["hi"] for r in rr]) * scale
            ax[k].errorbar(x, y, yerr=[y - lo, hi - y], fmt=mk, ms=3, alpha=.6, elinewidth=.5, label=f"Test {test}")
    for k, lab in ((0, "OOS mean R / trade (95% CI)"), (1, "OOS net monthly % (95% CI)")):
        ax[k].axvspan(FLAG_LO, FLAG_HI, color="g", alpha=.08)
        ax[k].axhline(0, color="k", lw=.6)
        ax[k].set_xlabel("trades / month")
        ax[k].set_ylabel(lab)
        if ax[k].get_legend_handles_labels()[1]:
            ax[k].legend(fontsize=8)
    fig.suptitle("Trade-off frontier, all logged variants (shaded: 150-250 trades/mo)")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


# ------------------------------------------------------------------ headline / pass bar
def headline(trades: pd.DataFrame, daily: pd.Series, n_trials: Optional[int] = None,
             var_sr: Optional[float] = None, *, dsr_floor: Optional[int] = None) -> dict:
    """Primary-configuration headline.

    Program DSR N = max(declared program N 456, trials logged) (SPEC v1.3.2 O1.9).
    A formation kind passes dsr_floor=48 (SPEC v1.3.5 S2) and that N is used as-is.
    """
    if dsr_floor is None:
        n_trials = max(int(n_trials or 0), N_PROGRAM)
    else:
        n_trials = int(dsr_floor)
    n_sessions = len(daily)
    day = pd.to_datetime(trades["session"]).dt.date.to_numpy() if len(trades) else np.array([])
    s = S.trade_summary(trades["r"].to_numpy(float) if len(trades) else np.array([]),
                        trades["pnl"].to_numpy(float) if len(trades) else np.array([]), day, n_sessions, daily)
    s["dsr"] = S.deflated_sharpe(daily, n_trials, var_sr)
    s["dsr_scope_n"] = None if dsr_floor is None else int(dsr_floor)
    if len(trades):
        yrs = pd.to_datetime(trades["session"]).dt.year
        my = S.monthly_returns(daily)
        s["years_positive"] = {int(y): float(g["r"].mean()) for y, g in trades.groupby(yrs)}
        s["monthly_by_year"] = {int(y): float(v.mean()) for y, v in my.groupby(my.index.year)} if len(my) else {}
    return s


def pass_bar_words(h: dict, cost_x15_mean_r: Optional[float] = None) -> list[str]:
    """SPEC section 8 checks, stated in words. Returns one line per criterion (PASS/FAIL/UNKNOWN)."""
    out = []
    mr = h.get("mean_r_ci", {})
    out.append(f"{'PASS' if mr.get('lo') is not None and mr['lo'] > 0 else 'FAIL'}: OOS mean R 95% lower bound "
               f"{_num(mr.get('lo'))} must be > 0.")
    mc = h.get("monthly_ci", {})
    out.append(f"{'PASS' if mc.get('lo') is not None and mc['lo'] > 0 else 'FAIL'}: OOS monthly return 95% lower "
               f"bound {_pct(mc.get('lo'))} must be > 0.")
    d = h.get("dsr", {}).get("dsr")
    out.append(f"{'PASS' if d is not None and d >= 0.95 else 'FAIL'}: deflated Sharpe {_num(d)} "
               f"(N = {h.get('dsr', {}).get('n_trials')}) must be >= 0.95.")
    yp = h.get("years_positive", {})
    npos = sum(v > 0 for v in yp.values())
    out.append(f"INFO: mean R positive in {npos} of {len(yp)} calendar years.")
    if cost_x15_mean_r is not None:
        out.append(f"{'PASS' if cost_x15_mean_r > 0 else 'FAIL'}: mean R at 1.5x costs {_num(cost_x15_mean_r)} "
                   "must stay > 0.")
    return out


# ------------------------------------------------------------------ options tables
def options_variant_row(name: str, daily: pd.Series, taken: pd.DataFrame, counters: Mapping, skipped: Mapping) -> dict:
    m = S.monthly_returns(daily)
    pnl = taken["pnl"].to_numpy(float) if len(taken) else np.array([])
    w, l = pnl[pnl > 0], pnl[pnl <= 0]
    aw, al = (w.mean() if w.size else np.nan), (l.mean() if l.size else np.nan)
    be = abs(al) / (aw + abs(al)) if w.size and l.size else np.nan
    mc = S.block_mean(m.to_numpy()) if len(m) else {"mean": None}
    return {"variant": name, "trades": int(pnl.size), "trades/mo": _num(pnl.size / max(len(m), 1), 1),
            "win rate": _pct(float((pnl > 0).mean()) if pnl.size else None),
            "avg win $": _num(aw, 0), "avg loss $": _num(al, 0), "break-even win": _pct(be),
            "monthly mean [95% CI]": _ci(mc, _pct), "max DD": _pct(S.max_drawdown(daily) if len(daily) else None),
            "worst day": _pct(float(daily.min()) if len(daily) else None),
            "skipped (no expiry)": int(skipped.get("no_same_day_expiry", skipped.get("no_expiry_in_bucket", 0)))}


def _worst_week(d: pd.Series) -> float:
    if not len(d):
        return np.nan
    idx = pd.DatetimeIndex(pd.to_datetime(d.index))
    return float(((1 + pd.Series(d.to_numpy(), idx)).groupby(idx.to_period("W")).prod() - 1).min())


def guardrail_stats(name: str, trades: pd.DataFrame, daily: pd.Series, counters: Mapping) -> dict:
    """Numbers for one configuration row (SPEC v1.3 G4). trades: session, r, pnl; daily: daily returns."""
    m = S.monthly_returns(daily)
    n = 0 if trades is None else len(trades)
    weeks = len(pd.DatetimeIndex(pd.to_datetime(daily.index)).to_period("W").unique()) if len(daily) else 0
    day = pd.to_datetime(trades["session"]).dt.date.to_numpy() if n else np.array([])
    return {"guardrail": name, "trades": n, "trades_per_mo": n / max(len(m), 1),
            "win_rate": float((trades["pnl"] > 0).mean()) if n else None,
            "mean_r": S.day_block_mean_r(trades["r"].to_numpy(float), day) if n else {"mean": None},
            "monthly": S.block_mean(m.to_numpy()) if len(m) else {"mean": None},
            "max_dd": S.max_drawdown(daily) if len(daily) else None, "worst_week": _worst_week(daily),
            "days_halted_day_limit": int(counters.get("days_halted_day_limit", 0)),
            "weeks_halted_week_limit": int(counters.get("weeks_halted_week_limit", 0)),
            "daily_stop_days": int(counters.get("daily_stop_days", 0)),
            "signals_cancelled_at_trip": int(counters.get("signals_cancelled_at_trip", 0)),
            "signals_arrived_blocked": int(counters.get("signals_arrived_blocked", 0)),
            "sessions": int(len(daily)), "weeks": int(weeks)}


def guardrail_rows(stats: Sequence[dict]) -> pd.DataFrame:
    """3-row table, primary d2+w5 first (this is the result), then none and d2+w6 (comparison, report only)."""
    order = {"d2+w5": 0, "none": 1, "d2+w6": 2}
    rows = []
    for g in sorted(stats, key=lambda g: order.get(g["guardrail"], 9)):
        rows.append({
            "guardrail": g["guardrail"] + (" (PRIMARY)" if g["guardrail"] == "d2+w5" else " (comparison)"),
            "trades/mo": _num(g["trades_per_mo"], 1), "win rate": _pct(g["win_rate"], 1),
            "mean R [95% CI]": _ci(g["mean_r"]), "monthly @0.5% risk [95% CI]": _ci(g["monthly"], _pct),
            "max DD": _pct(g["max_dd"]), "worst week": _pct(g["worst_week"]),
            "day-limit trigger rate": _pct(g["days_halted_day_limit"] / max(g["sessions"], 1), 1),
            "week-limit trigger rate": _pct(g["weeks_halted_week_limit"] / max(g["weeks"], 1), 1),
            "days halted (day limit)": g["days_halted_day_limit"], "weeks halted (week limit)": g["weeks_halted_week_limit"],
            "daily-stop days": g["daily_stop_days"], "armed triggers cancelled at trip": g["signals_cancelled_at_trip"],
            "signals arriving while blocked": g["signals_arrived_blocked"]})
    return pd.DataFrame(rows)


def r7_table(block: Mapping) -> pd.DataFrame:
    """Selected path beside the pool of every variant's exact OOS trades (SPEC v1.3.5 R7)."""
    rows = []
    for scope, key in (("selected path", "selected"), ("pooled all variants", "pooled")):
        m = block.get(key) or {}
        rows.append({"scope": scope, "trades": int(m.get("trades") or 0),
                     "gross mean R": _num(m.get("gross_mean_r")),
                     "mean cost R": _num(m.get("cost_mean_r")),
                     "net mean R": _num(m.get("net_mean_r"))})
    return pd.DataFrame(rows)


def trades_per_month_words(stats: Sequence[dict]) -> str:
    parts = []
    for g in stats:
        t = g["trades_per_mo"]
        band = "inside" if FLAG_LO <= t <= FLAG_HI else ("below" if t < FLAG_LO else "above")
        parts.append(f"{g['guardrail']} achieved {t:.0f} trades/mo ({band} the 150-250 band; target ~200)")
    return "; ".join(parts) + "."


GUARDRAIL_NOTE = (
    "Expected trades until the k-th loss is k / (1 - WR). At a 60% win rate the 2/day limit alone allows about 5 trades "
    "per session (~105/mo); the 5/week limit allows about 12.5 per week (~54/mo) and binds first, so the primary "
    "configuration's expected ceiling is ~54/mo at 60% WR (~43/mo at 50%, ~36/mo at 40%) [P, ignores signal supply and "
    "the other caps]. Reaching 150/mo under d2+w5 needs a win rate of about 86%+, 200/mo about 89% [P]. The 150-250 "
    "trades/mo target is therefore likely unreachable under the primary configuration; nothing is loosened to chase it.")


# ------------------------------------------------------------------ document
@dataclass
class ReadoutInputs:
    spec_version: str
    git_sha: str
    grid_sha: str
    trial_counts: Mapping[str, int]
    symbols: Sequence[str]
    n_universe: int = 33
    headlines: Mapping[str, dict] = field(default_factory=dict)        # test -> headline()
    picks: Mapping[str, pd.DataFrame] = field(default_factory=dict)    # test -> fold picks table
    per_symbol: Mapping[str, pd.DataFrame] = field(default_factory=dict)
    sensitivities: Mapping[str, pd.DataFrame] = field(default_factory=dict)
    frontier: Sequence[dict] = ()
    options_baseline: Optional[pd.DataFrame] = None
    options_0dte: Optional[pd.DataFrame] = None
    guardrails: Mapping[str, tuple] = field(default_factory=dict)   # scope label -> (guardrail_rows(), words)
    notes: Sequence[str] = ()
    smoke: bool = False
    n_program: int = 0                                                 # cumulative program-ledger trials
    n_dsr: int = N_PROGRAM                                             # max(456, n_program)
    exact_checks: Mapping[str, tuple] = field(default_factory=dict)    # test -> (exact_finalist_check df, cp4 flag)
    interim: bool = True               # A1b ruling: dev-window runs stay INTERIM / informational even at 33/33 (to CP4)
    errored: Mapping[str, pd.DataFrame] = field(default_factory=dict)  # test -> errored variants (variant_id, error)
    not_for_cp4: str = ""              # attribution checks (legacy flags): banner, never a CP4/selection input
    r7: Mapping[str, Mapping] = field(default_factory=dict)            # test -> {selected, pooled} gross/cost/net
    fold_83: Mapping[str, Mapping] = field(default_factory=dict)        # test -> §8.3 summary (S3)
    spec_doc: str = ""                 # set on F/B runs: v1.3.5
    spec_doc_commit: str = ""
    engine_spec: str = ""              # F/B stamp; A-only readouts leave this empty


def write_readout(inp: ReadoutInputs, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    L = []
    interim = inp.interim or len(inp.symbols) < inp.n_universe
    title = "PIPELINE SMOKE TEST — NOT A STRATEGY RESULT" if inp.smoke else (
        f"INTERIM — {len(inp.symbols)} of {inp.n_universe} symbols — not a pass/fail result (development window, "
        "informational only until CP4)" if interim
        else "Full universe, development window (holdout untouched)")
    if inp.not_for_cp4:
        title = f"{inp.not_for_cp4.split(':')[0]} — {title}"
    L += [f"# Intraday S/R readout: {title}", ""]
    if inp.not_for_cp4:
        L += [f"> **{inp.not_for_cp4}**", ""]
    L += [
          f"- Spec {inp.spec_version}; git {inp.git_sha[:12]}; grid sha256 {inp.grid_sha[:16]}",
          f"- Trial count: DSR N = max({N_PROGRAM}, program ledger) = **{inp.n_dsr}** (program ledger: {inp.n_program} "
          f"trials cumulative across all tests and runs; {N_PROGRAM} declared = A 192 + B 192 + formations 48 + options "
          "overlay 24 (SPEC v1.3.2 O1.9); the hashed v1.3.1 grid's N_TRIALS stays 450). "
          f"Grid hash (grids.GRID_SHA256): `{inp.grid_sha}`. This run "
          "logged: " + (", ".join(f"{k} = {v}" for k, v in inp.trial_counts.items()) or "none")
          + ". Guardrail configurations add 0 trials.",
          "- Configuration: **primary loss guardrail d2+w5** (2 losses/session, 5 losses/Mon-Fri week; SPEC v1.3 G1) for "
          "every result, selection, pass-bar item and the DSR. 'none' and 'd2+w6' are comparison rows only.",
          f"- Symbols ({len(inp.symbols)}): {' '.join(inp.symbols)}",
          "- Model-based research only; holdout 2026-04-01..2026-09-30 not opened; CP4 review before Trading.", ""]
    if inp.spec_doc:
        line = f"- spec_doc {inp.spec_doc} ({inp.spec_doc_commit})"
        if inp.engine_spec:
            line += f"; engine_spec {inp.engine_spec}"
        L += [line, ""]
    for test, er in inp.errored.items():
        n_er = 0 if er is None else len(er)
        L += [f"## Test {test}: errored variants ({n_er})", "",
              "Errored variants are logged program trials that produced no result; they are never eligible for "
              "selection." if n_er else "None: every variant ran.", ""]
        if n_er:
            L += [_md(er.assign(error=er["error"].astype(str).str.slice(0, 300).str.replace("|", "\\|", regex=False))), ""]
    for test, h in inp.headlines.items():
        L += [f"## Test {test}: walk-forward OOS (2020Q1-2026Q1, selected variant per fold)", "",
              f"- Trades {h['trades']} | **trades/day {_num(h['trades_per_day'], 2)}** | trades/mo "
              f"{_num(h['trades_per_month'], 1)} | win {_pct(h['win_rate'], 1)} | mean R {_ci(h['mean_r_ci'])} | "
              f"PF {_num(h['profit_factor'], 2)}",
              f"- Monthly {_ci(h['monthly_ci'], _pct)} | max DD {_pct(h['max_drawdown'])} | worst day "
              f"{_pct(h['worst_day'])} | Sharpe(daily) {_num(h['sharpe_daily'])} | DSR {_num(h['dsr'].get('dsr'))} "
              f"(N {h['dsr'].get('n_trials')}, var source: {h['dsr'].get('var_sr_source', 'n/a')})", "",
              "Pass bar (SPEC section 8)" + (" — informational only in an interim run:" if interim else ":"), ""]
        L += [f"- {x}" for x in pass_bar_words(h)] + [""]
        if h.get("dsr_scope_n"):
            L += [f"- DSR N for this test is **{int(h['dsr_scope_n'])}** (formations grid, SPEC v1.3.5 S2).", ""]
        crit = inp.fold_83.get(test) if inp.fold_83 else None
        if crit:
            L += [f"- §8.3: {crit['n_non_positive']} of {crit['n_folds']} folds are non-positive "
                  f"({crit['n_unselected']} with no variant at >= 200 train trades).", ""]
        if int(h.get("trades") or 0) < 500:
            L += [f"- FINDING (G5/S4): selected-path OOS has {int(h.get('trades') or 0)} trades (under 500). "
                  "Underpowered. This result cannot pass.", ""]
        block = inp.r7.get(test) if inp.r7 else None
        if block:
            L += ["Gross, cost, and net mean R (SPEC v1.3.5 R7):", "", _md(r7_table(block))]
        if h.get("years_positive"):
            L += ["Per year:", "", _md(pd.DataFrame([{"year": y, "mean R": _num(v),
                                                      "monthly mean": _pct(h["monthly_by_year"].get(y))}
                                                     for y, v in h["years_positive"].items()]))]
        if test in inp.picks:
            L += ["Fold picks:", "", _md(inp.picks[test])]
        if test in inp.per_symbol:
            L += ["Per symbol:", "", _md(inp.per_symbol[test])]
        if test in inp.sensitivities:
            L += ["Sensitivities:", "", _md(inp.sensitivities[test])]
    if inp.frontier:
        png = frontier_png(inp.frontier, out_dir / "frontier.png")
        nflag = sum(r["flag_150_250_lbR_gt0"] for r in inp.frontier)
        L += ["## Trade-off frontier (primary d2+w5; all logged variants, fixed-variant OOS 2020Q1-2026Q1)", "",
              f"{nflag} setting(s) flagged at 150-250 trades/mo with OOS mean-R 95% lower bound > 0. A flag is "
              "informational only: choosing from this curve is itself selection, so flagged settings must still "
              "clear the full section 8 bar.", ""]
        if png:
            L += ["![frontier](frontier.png)", ""]
        L += [_md(frontier_table(inp.frontier))]
    for test, (df, flag) in inp.exact_checks.items():
        L += [f"## Exact finalist check, Test {test} (R1 ruling 2c)", "",
              "Finalists plus the next 3 by rank, re-simulated on the finalist train window (2025-04-01..2026-03-31) "
              "with guardrail counters and equity starting at the window start and no internal resets. Selection "
              "itself used the continuous path (counters also reset at internal quarter starts).", "",
              ("**No finalist this run (none eligible): nothing to check.**" if not len(df) else
               f"**{'CP4 FLAG: a finalist changed rank or moved > 0.01R' if flag else 'No CP4 flag (finalist ranks unchanged, |dR| <= 0.01)'}**"),
              "", _md(df.round(4) if len(df) else df)]
    if inp.options_baseline is not None:
        L += ["## Options overlay, baseline (0.5% premium, frozen finalists only)", "", _md(inp.options_baseline)]
    if inp.options_0dte is not None:
        L += ["## 0DTE scenario ($2,000 premium on $100k; HIGHER-RISK, MODEL-BASED)", "", f"> {ZERO_DTE_CAVEAT}", "",
              _md(inp.options_0dte)]
    for scope, (df, words) in inp.guardrails.items():
        L += [f"## Guardrail configurations: {scope}", "",
              "Primary d2+w5 is the result; 'none' and 'd2+w6' ran after selection on the same picks with only RiskCfg "
              "changed (guardrail_compare.parquet; never read by selection, freeze or holdout code).", "",
              _md(df), f"Trades/mo achieved: {words}", ""]
    L += ["## Plain note on the guardrail and trade frequency (SPEC v1.3 G4)", "", GUARDRAIL_NOTE, ""]
    if inp.notes:
        L += ["## Notes", ""] + [f"- {n}" for n in inp.notes] + [""]
    p = out_dir / "READOUT.md"
    p.write_text("\n".join(L))
    return p
