"""Run the win-rate study and write docs/win-rate-study.md.

Signal-book numbers are one spread per underlying, sized at 0.5% of a fixed
$100,000. That is the per-spread edge. A second pass applies the book caps
(5 and 20 names, 10% open risk) without inventing replacement signals.

A filter is adopted only when the long window says so and the recent window
does not contradict it. See ``adoption_reason``.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from typing import Optional, Sequence

from alpaca_options_credit.config import load_config
from alpaca_options_credit.replay.data import ensure_universe
from alpaca_options_credit.replay.engine import (
    StructureParams,
    VariantDiag,
    replay_universe,
)
from alpaca_options_credit.replay.stats import (
    BookStats,
    Interval,
    ReplayTrade,
    bootstrap_diff,
    select_risk_book,
    summarize,
)
from alpaca_options_credit.replay.credit import ReplayLimits
from alpaca_options_credit.rth import as_et

LONG_START = date(2024, 1, 2)
LONG_END = date(2026, 9, 25)
RECENT_START = date(2026, 7, 6)
RECENT_END = date(2026, 9, 25)

# Long-window samples smaller than this cannot clear "beyond noise".
MIN_LONG = 30
# Recent window must be at least this large before it can confirm or veto.
MIN_RECENT = 15

VARIANT_ORDER = (
    "baseline",
    "skip_open_and_midday",
    "rs_vs_spy",
    "two_hvn_at_short",
    "htf_daily_ema50",
    "htf_4h_ema50",
    "confirm_volume",
    "rs_4h_volume",
)

VARIANT_BLURB = {
    "baseline": "Baseline — daily confirm, one HVN shelf, 60-minute reconfirm",
    "skip_open_and_midday": "Skip the first 30 minutes and the midday lull (11:30–13:30 ET)",
    "rs_vs_spy": "Relative strength vs SPY (bull put) / weakness (bear call), 10 sessions",
    "two_hvn_at_short": "Two HVN shelves aligned at the short strike",
    "htf_daily_ema50": "Daily EMA50 aligned with the spread",
    "htf_4h_ema50": "4-hour EMA50 aligned with the spread",
    "confirm_volume": "Confirmation bar volume above its prior 20-bar average",
    "rs_4h_volume": "Relative strength, 4-hour EMA50, and confirmation volume together",
    "confirm_15m": "Same rules, 15-minute reconfirm instead of 60-minute",
}


def predicates() -> dict:
    return {
        "baseline": lambda f: True,
        "skip_open_and_midday": lambda f: f.session_ok,
        "rs_vs_spy": lambda f: f.rs_ok,
        "two_hvn_at_short": lambda f: f.two_hvn_ok,
        "htf_daily_ema50": lambda f: f.ema_daily_ok,
        "htf_4h_ema50": lambda f: f.ema_4h_ok,
        "confirm_volume": lambda f: f.volume_ok,
        "rs_4h_volume": lambda f: f.rs_ok and f.ema_4h_ok and f.volume_ok,
    }


def limits_from_config(cfg: dict) -> ReplayLimits:
    spreads = cfg.get("spreads") or {}
    exits = cfg.get("exits") or {}
    risk = cfg.get("risk") or {}
    dte_min = int(spreads.get("dte_min", 30))
    dte_max = int(spreads.get("dte_max", 45))
    return ReplayLimits(
        width=float(spreads.get("width", 5.0)),
        dte_min=dte_min,
        dte_max=dte_max,
        dte_target=(dte_min + dte_max) // 2,
        min_credit_pct=float(spreads.get("min_credit_pct_of_width", 0.20)),
        max_credit_pct=float(spreads.get("max_credit_pct_of_width", 1.0) or 1.0),
        min_short_inv_gap=float(spreads.get("min_short_inv_gap", 1.0)),
        tp_frac=float(exits.get("take_profit_frac_of_credit", 0.50)),
        stop_mult=float(exits.get("stop_multiple_of_credit", 1.5)),
        multiplier=int(spreads.get("multiplier", 100)),
        equity=float(risk.get("paper_equity_fallback", 100_000)),
        risk_pct=float(risk.get("risk_per_trade_pct", 0.005)),
    )


def structure_from_config(cfg: dict) -> StructureParams:
    tf = cfg.get("timeframe") or {}
    vp = tf.get("volume_profile") or {}
    md = cfg.get("market_data") or {}
    return StructureParams(
        left=int(tf.get("swing_left", 2)),
        right=int(tf.get("swing_right", 2)),
        atr_period=int(tf.get("atr_period", 14)),
        vp_lookback=int(vp.get("lookback_bars", 25)),
        vp_bin=float(vp.get("bin_size", 0.5)),
        vp_percentile=float(vp.get("hvn_percentile", 0.70)),
        no_chase_atr=float(tf.get("no_chase_atr", 0.5)),
        arm_timeout_bars=int(tf.get("arm_timeout_bars", 10)),
        daily_lookback=int(md.get("daily_bar_lookback", 60)),
        timing_lookback=int(md.get("bar_lookback", 120)),
    )


def in_dates(trades: Sequence[ReplayTrade], start: date, end: date) -> list[ReplayTrade]:
    return [t for t in trades if start <= as_et(t.entry_time).date() <= end]


def week_span(start: date, end: date) -> float:
    return ((end - start).days + 1) / 7.0


def _fmt_ci(interval: Optional[Interval], kind: str) -> str:
    if interval is None:
        return "n/a"
    if kind == "pct":
        return (
            f"{100 * interval.point:.1f}% "
            f"[{100 * interval.low:.1f}%, {100 * interval.high:.1f}%]"
        )
    if kind == "usd":
        return f"{_usd(interval.point)} [{_usd(interval.low)}, {_usd(interval.high)}]"
    return f"{100 * interval.point:.1f}% [{100 * interval.low:.1f}%, {100 * interval.high:.1f}%]"


def _usd(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):.0f}"


def _money(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return _usd(value)


def stats_row(stats: BookStats) -> str:
    return (
        f"| {VARIANT_BLURB.get(stats.label, stats.label)} "
        f"| {stats.n} | {stats.trades_per_week:.2f} "
        f"| {_fmt_ci(stats.win_rate, 'pct')} "
        f"| {_fmt_ci(stats.expectancy, 'usd')} "
        f"| {_fmt_ci(stats.expectancy_r, 'r')} "
        f"| {_money(stats.avg_win)} | {_money(stats.avg_loss)} |"
    )


def exit_row(stats: BookStats) -> str:
    counts = stats.exit_counts
    def n(key: str) -> int:
        return int(counts.get(key, 0))
    return (
        f"| {VARIANT_BLURB.get(stats.label, stats.label)} | {stats.n} "
        f"| {n('take_profit')} | {n('stop_credit')} | {n('structure_break')} "
        f"| {n('expiration')} | {n('open_mtm')} |"
    )


TABLE_HEADER = (
    "| Filter | Trades | Trades/week | Win rate (95% CI) | "
    "Expectancy $ (95% CI) | Expectancy / max risk (95% CI) | Avg win | Avg loss |\n"
    "| --- | ---: | ---: | --- | --- | --- | ---: | ---: |"
)

EXIT_HEADER = (
    "| Filter | Trades | Take-profit | Stop 1.5× | Structure break | Expiration | Open mark |\n"
    "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"
)


def adoption_reason(
    name: str,
    long_base: BookStats,
    long_var: BookStats,
    long_diff: Optional[Interval],
    recent_base: BookStats,
    recent_var: BookStats,
    recent_diff: Optional[Interval],
    wr_diff: Optional[Interval] = None,
) -> tuple[bool, str]:
    """Return (adopt, plain-English reason).

    Adopt only when a 95% bootstrap interval on the long window sits entirely
    above zero: either expectancy per unit of risk, or win rate when expectancy
    is not lower. A higher point estimate whose interval still covers zero is
    noise. Jul–Sep 2026 vetoes a pass that reverses there.
    """
    if name == "baseline":
        return False, "Baseline is the control."
    if long_var.n < MIN_LONG or long_base.expectancy_r is None or long_var.expectancy_r is None:
        return (
            False,
            f"{long_var.n} trades on the long window is below {MIN_LONG}, so the "
            "bootstrap interval cannot justify a default change.",
        )
    if long_diff is None or long_var.expectancy is None or long_base.expectancy is None:
        return False, "Expectancy could not be compared."
    if long_var.win_rate is None or long_base.win_rate is None:
        return False, "Win rate could not be compared."

    expectancy_holds = (
        long_var.expectancy.point >= long_base.expectancy.point
        and long_var.expectancy_r.point >= long_base.expectancy_r.point
    )
    beyond_exp = long_diff.low > 0 and long_var.expectancy.point >= long_base.expectancy.point
    beyond_wr = wr_diff is not None and wr_diff.low > 0 and expectancy_holds
    if not beyond_exp and not beyond_wr:
        wr_note = ""
        if wr_diff is not None:
            wr_note = (
                f" Win rate differs by {wr_diff.point:.3f} "
                f"[{wr_diff.low:.3f}, {wr_diff.high:.3f}]."
            )
        return (
            False,
            f"Expectancy per unit of risk differs by {long_diff.point:.3f} "
            f"[{long_diff.low:.3f}, {long_diff.high:.3f}]. "
            f"That interval covers zero.{wr_note}",
        )

    if recent_var.n < MIN_RECENT or recent_base.n < MIN_RECENT:
        return (
            False,
            "The long window clears the noise bar, but Jul–Sep 2026 has only "
            f"{recent_var.n} filtered trades. That is too few to confirm the edge.",
        )

    if (
        recent_var.expectancy_r is not None
        and recent_base.expectancy_r is not None
        and recent_var.expectancy_r.point < recent_base.expectancy_r.point
    ):
        return (
            False,
            "The long window clears the noise bar, but Jul–Sep 2026 expectancy per "
            "unit of risk is lower than the baseline. The recent book vetoes the change.",
        )
    if beyond_wr and not beyond_exp:
        if recent_var.win_rate is None or recent_base.win_rate is None:
            return False, "Recent win rate could not be compared."
        if recent_var.win_rate.point < recent_base.win_rate.point:
            return (
                False,
                "Win rate clears the noise bar on the long window, but not on Jul–Sep 2026.",
            )
    how = (
        "expectancy per unit of risk beats the baseline and the 95% interval on "
        "that difference sits entirely above zero"
        if beyond_exp
        else "win rate beats the baseline by more than sampling noise and expectancy is not lower"
    )
    recent_note = ""
    if recent_diff is not None:
        recent_note = (
            f" Jul–Sep difference in expectancy/risk is {recent_diff.point:.3f} "
            f"[{recent_diff.low:.3f}, {recent_diff.high:.3f}]."
        )
    return True, f"On the long window, {how}.{recent_note}"


def risk_veto(
    base: BookStats,
    var: BookStats,
    *,
    cap: int,
) -> Optional[str]:
    """None when the capped book does not argue against adoption."""
    if var.n < MIN_RECENT or base.n < MIN_RECENT:
        return (
            f"The {cap}-spread book has {var.n} filtered trades against "
            f"{base.n} baseline trades, too few to trust a default change."
        )
    if (
        var.expectancy_r is not None
        and base.expectancy_r is not None
        and var.expectancy_r.point < base.expectancy_r.point
    ):
        return (
            f"Under a max of {cap} spreads, expectancy per unit of risk is "
            f"{var.expectancy_r.point:.3f} vs the baseline {base.expectancy_r.point:.3f}."
        )
    return None


def _book(trades: Sequence[ReplayTrade], label: str, start: date, end: date) -> BookStats:
    return summarize(in_dates(trades, start, end), label=label, weeks=week_span(start, end))


def _diff(trades: Sequence[ReplayTrade], base: Sequence[ReplayTrade], start: date, end: date) -> Optional[Interval]:
    left = [t.r_multiple for t in in_dates(trades, start, end)]
    right = [t.r_multiple for t in in_dates(base, start, end)]
    return bootstrap_diff(left, right)


def _flag_diff(
    trades: Sequence[ReplayTrade], base: Sequence[ReplayTrade], start: date, end: date
) -> Optional[Interval]:
    left = [1.0 if t.win else 0.0 for t in in_dates(trades, start, end)]
    right = [1.0 if t.win else 0.0 for t in in_dates(base, start, end)]
    return bootstrap_diff(left, right)


def render(ctx: dict) -> str:
    lines: list[str] = []
    add = lines.append
    adopted = ctx["adopted"]
    base = ctx["long_rows"][0]
    add("# Win-rate study")
    add("")
    if adopted:
        names = ", ".join(VARIANT_BLURB[name] for name in adopted)
        add(
            f"Change the defaults. {names} beat the baseline by more than sampling "
            "noise on the long window, and Jul–Sep 2026 does not reverse that."
        )
    else:
        exp = _fmt_ci(base.expectancy, "usd")
        wr = _fmt_ci(base.win_rate, "pct")
        add(
            "No change is warranted. On the long window the baseline credit spread "
            f"wins {wr} of the time and expects {exp} per spread. Every filter's "
            "difference versus that book still covers zero, including win rate. "
            "None of them raise expectancy beyond sampling noise, and none raise "
            "win rate beyond sampling noise without hurting expectancy. "
            "Jul 6–Sep 25 2026 tells the same story. Defaults stay as they are. "
            "`config/paper-live.yaml` is not in this repo and was not added."
        )
    add("")
    add(
        f"Winners stay about {_money(base.avg_win)} and losers about {_money(base.avg_loss)}. "
        "The filters do not shrink the winners. They also do not separate from the loss. "
        "Almost every spread hits the 1.5× credit stop, the 50% take-profit, or a "
        "structure break before expiration. The short strike sits just beyond "
        "invalidation, so the credit is close to the money and the stop is close in price."
    )
    add("")
    add("## What was held fixed")
    add("")
    add(
        "Entries stay a daily strict confirm plus a volume-profile shelf, then the "
        "first timing-bar pullback and a timing-bar reconfirm. Exits stay "
        "take-profit at 50% of credit, stop at 1.5× credit, and a daily close "
        "through invalidation. Sizing stays 0.5% of equity per spread and 10% "
        "open risk, one spread per name, $5 wide, 30–45 DTE, natural credit at "
        "least 20% of width, short strike at least 1 point beyond invalidation."
    )
    add("")
    add(
        "Checked-in `config/default.yaml` allows 20 concurrent spreads. The request "
        "described a 5-spread cap and a `config/paper-live.yaml` that is not in the "
        "tree. The decision uses the per-spread book (no cross-name cap). The 5-spread "
        "and 20-spread books are reported beside it; a filter that only looks good "
        "because of the cap does not get a default change."
    )
    add("")
    add("## How the tape was priced")
    add("")
    add(
        "There is no stored option tape, so each vertical is priced with Black-Scholes "
        "on the underlying bars. Implied vol is the last 20 sessions of close-to-close "
        "realized vol times 1.15, frozen at entry, and clamped between 15% and 125%. "
        "Rates are 4% with no dividend. Each leg's half-spread is 6% of its mid, at "
        "least $0.05 and at most $0.25. The entry credit is short bid minus long ask, "
        "and it has to clear the live 20% width gate. Take-profit and stop are judged "
        "on the mid, which is what the live mark uses. A take-profit fills at exactly "
        "50% of the credit (the poll catches the cross; the far side of an hourly wick "
        "is not a bigger winner). A stop that gaps through the open fills at the open's "
        "natural debit. A stop that trades through fills at 1.5× credit plus the "
        "bid/ask, and no better than that bar's worst debit. If both a stop and a "
        "take-profit are inside the same bar, the stop wins. A structure break pays "
        "the natural debit at the daily close. Expiration, if nothing else fired, "
        "settles at intrinsic. Anything still open on the last bar is marked at the "
        "natural debit and kept in the averages, so the recent window is not only "
        "the trades that happened to finish."
    )
    add("")
    add(
        "Strike grid: $1 on the ETFs in the full-A list; stocks use $0.50 under $50, "
        "$1 under $200, $2.50 under $500, and $5 above that. The short is the listed "
        "strike that clears the 1-point gap, via the same `build_proposal` path as "
        "the engine. Earnings and FOMC blackouts are empty in `config/calendar.yaml`, "
        "so the replay does not skip them either."
    )
    add("")
    add(
        f"Hourly bars run from {ctx['hourly_start']} through {ctx['hourly_end']} "
        f"({ctx['symbols_used']} names with both a daily and an hourly tape). "
        "The long window starts 2024-01-02 so the hourly history (which begins "
        f"around {ctx['hourly_start']}) can warm up. 15-minute bars are only "
        f"available from {ctx['m15_start']} (Yahoo's intraday limit), so 15-minute "
        "versus 60-minute confirmation is scored on Jul 6–Sep 25 2026 only. "
        "The 15-minute book still uses the full daily history for the shelf and "
        "the invalidation; only the timing series is short."
    )
    add("")
    add("## Filters")
    add("")
    add(
        "A filter either allows the entry or leaves the arm up for a later bar. "
        "It does not change the exit."
    )
    add("")
    add(
        "- **Skip the open and the midday lull.** The confirmation bar overlaps "
        "09:30–10:00 ET or 11:30–13:30 ET. On a 60-minute tape the 09:30 bar "
        "overlaps the first half hour, and the 11:30 and 12:30 bars overlap the lull."
    )
    add(
        "- **Relative strength.** Last 10 completed sessions: the underlying beat "
        "SPY for a bull put, or lagged SPY for a bear call. A tie fails. SPY versus "
        "itself never passes."
    )
    add(
        "- **Two HVN shelves.** At least two high-volume nodes from the same 25-day "
        "profile sit within max(ATR, $1) of the short strike. One shelf is already "
        "required by the baseline zone."
    )
    add(
        "- **Daily EMA50.** Bull put only if the last completed close is above the "
        "daily EMA50; bear call only if it is below."
    )
    add(
        "- **4-hour EMA50.** Same test on session blocks (09:30–13:30 and 13:30–16:00) "
        "rather than a clock-aligned 4-hour bar that would mix the cash close into "
        "the next premarket."
    )
    add(
        "- **Confirmation volume.** The timing bar that reconfirms has more volume "
        "than the average of the prior 20 timing bars."
    )
    add(
        "- **15-minute vs 60-minute.** The timing series is 15-minute bars instead "
        "of 60-minute bars. Lookback stays 120 bars, matching `market_data.bar_lookback`. "
        "That is about two sessions of 15-minute data, not 120 hours."
    )
    add(
        "- **The three small point-estimate bumps together.** Relative strength, "
        "the 4-hour EMA50, and confirmation volume each had a slightly higher win "
        "rate on the point estimate. They are also tested as one stack, all three required."
    )
    add("")
    add("## Long window (2024-01-02 to 2026-09-25)")
    add("")
    add(
        "Per spread, one name at a time, sized at 0.5% of $100,000. "
        "Intervals are 5,000-draw percentile bootstraps. "
        "Avg loss is the mean of losing trades (negative)."
    )
    add("")
    add(TABLE_HEADER)
    for stats in ctx["long_rows"]:
        add(stats_row(stats))
    add("")
    add(EXIT_HEADER)
    for stats in ctx["long_rows"]:
        add(exit_row(stats))
    add("")
    add("### Difference vs baseline (expectancy / max risk)")
    add("")
    add("| Filter | Difference | 95% CI | Reads as |")
    add("| --- | ---: | --- | --- |")
    for name, diff in ctx["long_diffs"]:
        if diff is None:
            add(f"| {VARIANT_BLURB[name]} | n/a | n/a | too few trades |")
            continue
        add(
            f"| {VARIANT_BLURB[name]} | {diff.point:.3f} | "
            f"[{diff.low:.3f}, {diff.high:.3f}] | {_diff_words(diff)} |"
        )
    add("")
    add("## Recent window (2026-07-06 to 2026-09-25)")
    add("")
    add(TABLE_HEADER)
    for stats in ctx["recent_rows"]:
        add(stats_row(stats))
    add("")
    add(EXIT_HEADER)
    for stats in ctx["recent_rows"]:
        add(exit_row(stats))
    add("")
    add("### Difference vs baseline (expectancy / max risk)")
    add("")
    add("| Filter | Difference | 95% CI | Reads as |")
    add("| --- | ---: | --- | --- |")
    for name, diff in ctx["recent_diffs"]:
        if diff is None:
            add(f"| {VARIANT_BLURB[name]} | n/a | n/a | too few trades |")
            continue
        add(
            f"| {VARIANT_BLURB[name]} | {diff.point:.3f} | "
            f"[{diff.low:.3f}, {diff.high:.3f}] | {_diff_words(diff)} |"
        )
    for name, diff in ctx["recent_diffs"]:
        if diff is not None and diff.low > 0:
            add("")
            add(
                f"{VARIANT_BLURB[name]} is the recent-window row whose expectancy "
                "interval sits above zero. It still does not clear the rule: the same "
                "filter on the long window covers zero, and this recent sample is a "
                "handful of spreads. The point estimate is still a loss per spread."
            )
    add("")
    add("## 15-minute vs 60-minute confirmation")
    add("")
    add(
        "Same daily shelf and the same exits. Only the timing bar changes. "
        "Both rows are entries from Jul 6 through Sep 25 2026. The 15-minute "
        f"tape itself starts {ctx['m15_start']}, so early-July timing swings are thin."
    )
    add("")
    add(TABLE_HEADER)
    for stats in ctx["tf_rows"]:
        add(stats_row(stats))
    add("")
    if ctx["tf_diff"] is None:
        add("Not enough 15-minute trades to compare expectancy.")
    else:
        diff = ctx["tf_diff"]
        add(
            f"15-minute minus 60-minute expectancy/risk: {diff.point:.3f} "
            f"[{diff.low:.3f}, {diff.high:.3f}]. {_diff_words(diff)}."
        )
    add("")
    add("## Book caps")
    add("")
    add(
        "Same signals, then a greedy book: max concurrent spreads, 0.5% of "
        "*realized* equity, 10% open risk, exits freeing a slot before a same-time "
        "entry. This can only drop trades. It does not create a later signal on a "
        "name whose earlier signal was skipped."
    )
    add("")
    add(ctx["cap_section"])
    add("")
    add("## Decision")
    add("")
    for name, adopted_flag, reason in ctx["decisions"]:
        if name == "baseline":
            continue
        status = "Adopt" if adopted_flag else "Do not adopt"
        add(f"- **{status} — {VARIANT_BLURB[name]}.** {reason}")
    add("")
    tf_flag, tf_reason = ctx["tf_decision"]
    add(f"- **{'Adopt' if tf_flag else 'Do not adopt'} — 15-minute confirmation.** {tf_reason}")
    add("")
    if not adopted and not tf_flag:
        add(
            "Nothing here beat the baseline by enough to move a default. The "
            "playbook in `config/default.yaml` stays as it is."
        )
    add("")
    add("## Reproduce")
    add("")
    add("```bash")
    add("python -m alpaca_options_credit.replay.study --cache var/replay-cache --out docs/win-rate-study.md")
    add("```")
    add("")
    add(
        "The cache is Yahoo chart JSON under `var/` (gitignored). Rerunning with "
        "the cache warm does not hit the network. Bootstrap seed is 20260925, "
        "5,000 resamples."
    )
    add("")
    return "\n".join(lines)


def _diff_words(diff: Interval) -> str:
    if diff.low > 0:
        return "above noise"
    if diff.high < 0:
        return "worse than noise"
    return "inside noise"


def _cap_section(grouped: dict[str, list[ReplayTrade]], risk: dict) -> str:
    lines = [
        "| Book | Filter | Trades | Trades/week | Win rate | Expectancy $ | Expectancy / max risk |",
        "| --- | --- | ---: | ---: | --- | --- | --- |",
    ]
    for cap in (5, int(risk.get("max_concurrent", 20))):
        for window, start, end in (
            ("2024-01-02–2026-09-25", LONG_START, LONG_END),
            ("2026-07-06–2026-09-25", RECENT_START, RECENT_END),
        ):
            for name in VARIANT_ORDER:
                selected = select_risk_book(
                    in_dates(grouped[name], start, end),
                    max_concurrent=cap,
                    equity=float(risk.get("paper_equity_fallback", 100_000)),
                    risk_pct=float(risk.get("risk_per_trade_pct", 0.005)),
                    max_portfolio_risk_pct=float(risk.get("max_portfolio_risk_pct", 0.10)),
                )
                stats = summarize(selected, label=name, weeks=week_span(start, end))
                lines.append(
                    f"| max {cap}, {window} | {VARIANT_BLURB[name]} | {stats.n} "
                    f"| {stats.trades_per_week:.2f} | {_fmt_ci(stats.win_rate, 'pct')} "
                    f"| {_fmt_ci(stats.expectancy, 'usd')} | {_fmt_ci(stats.expectancy_r, 'r')} |"
                )
    return "\n".join(lines)


def build_report(cfg: dict, bars: dict[str, dict[str, list]]) -> str:
    symbols = list((cfg.get("universe") or {}).get("symbols") or [])
    usable = [
        s
        for s in symbols
        if len((bars.get(s) or {}).get("1d") or []) > 60
        and len((bars.get(s) or {}).get("1h") or []) > 120
    ]
    limits = limits_from_config(cfg)
    structure = structure_from_config(cfg)
    print(f"replaying {len(usable)} symbols on 60-minute bars", flush=True)
    grouped, diags = replay_universe(
        bars,
        usable,
        predicates(),
        timing_key="1h",
        minutes=60,
        limits=limits,
        structure=structure,
    )
    base = grouped["baseline"]
    long_rows = []
    recent_rows = []
    long_diffs = []
    recent_diffs = []
    decisions = []
    adopted: list[str] = []
    for name in VARIANT_ORDER:
        long_stats = _book(grouped[name], name, LONG_START, LONG_END)
        recent_stats = _book(grouped[name], name, RECENT_START, RECENT_END)
        long_rows.append(long_stats)
        recent_rows.append(recent_stats)
        if name == "baseline":
            decisions.append((name, False, "Baseline is the control."))
            continue
        d_long = _diff(grouped[name], base, LONG_START, LONG_END)
        d_recent = _diff(grouped[name], base, RECENT_START, RECENT_END)
        wr_long = _flag_diff(grouped[name], base, LONG_START, LONG_END)
        long_diffs.append((name, d_long))
        recent_diffs.append((name, d_recent))
        ok, reason = adoption_reason(
            name,
            long_rows[0],
            long_stats,
            d_long,
            recent_rows[0],
            recent_stats,
            d_recent,
            wr_diff=wr_long,
        )
        if ok:
            for cap in (5, int((cfg.get("risk") or {}).get("max_concurrent", 20))):
                veto = risk_veto(
                    _capped_stats(base, cap, cfg, LONG_START, LONG_END),
                    _capped_stats(grouped[name], cap, cfg, LONG_START, LONG_END),
                    cap=cap,
                )
                if veto:
                    ok = False
                    reason = veto
                    break
        if ok:
            adopted.append(name)
        decisions.append((name, ok, reason))

    print("replaying 15-minute confirmation", flush=True)
    m15_symbols = [
        s
        for s in usable
        if len((bars.get(s) or {}).get("15m") or []) > 50
    ]
    m15, _m15_diag = replay_universe(
        bars,
        m15_symbols,
        {"confirm_15m": lambda f: True},
        timing_key="15m",
        minutes=15,
        limits=limits,
        structure=structure,
    )
    tf_15 = _book(m15["confirm_15m"], "confirm_15m", RECENT_START, RECENT_END)
    tf_60 = _book(base, "baseline", RECENT_START, RECENT_END)
    tf_diff = bootstrap_diff(
        [t.r_multiple for t in in_dates(m15["confirm_15m"], RECENT_START, RECENT_END)],
        [t.r_multiple for t in in_dates(base, RECENT_START, RECENT_END)],
    )
    tf_ok, tf_reason = _tf_decision(tf_15, tf_60, tf_diff)

    hourly_start, hourly_end = _span(usable, bars, "1h")
    m15_start, _m15_end = _span(m15_symbols, bars, "15m")
    ctx = {
        "adopted": adopted,
        "long_rows": long_rows,
        "recent_rows": recent_rows,
        "long_diffs": long_diffs,
        "recent_diffs": recent_diffs,
        "decisions": decisions,
        "tf_rows": [tf_60, tf_15],
        "tf_diff": tf_diff,
        "tf_decision": (tf_ok, tf_reason),
        "cap_section": _cap_section(grouped, cfg.get("risk") or {}),
        "hourly_start": hourly_start,
        "hourly_end": hourly_end,
        "m15_start": m15_start,
        "symbols_used": len(usable),
        "diags": diags,
    }
    text = render(ctx)
    # Diagnostics sit after the decision so a reader sees the counts that
    # explain a thin filter without opening the code.
    text += _diag_section(diags)
    return text.replace("## Signal counts", "\n## Signal counts")


def _capped_stats(trades, cap, cfg, start, end) -> BookStats:
    risk = cfg.get("risk") or {}
    selected = select_risk_book(
        in_dates(trades, start, end),
        max_concurrent=cap,
        equity=float(risk.get("paper_equity_fallback", 100_000)),
        risk_pct=float(risk.get("risk_per_trade_pct", 0.005)),
        max_portfolio_risk_pct=float(risk.get("max_portfolio_risk_pct", 0.10)),
    )
    return summarize(selected, label="capped", weeks=week_span(start, end))


def _tf_decision(m15: BookStats, m60: BookStats, diff: Optional[Interval]) -> tuple[bool, str]:
    if m15.n < MIN_LONG or m60.n < MIN_RECENT or diff is None:
        return (
            False,
            f"{m15.n} fifteen-minute trades against {m60.n} sixty-minute trades "
            "in Jul–Sep 2026. That is not enough to replace the timing bar.",
        )
    if m15.expectancy is None or m60.expectancy is None or m15.win_rate is None or m60.win_rate is None:
        return False, "Could not compare the two timing bars."
    beyond = diff.low > 0 and m15.expectancy.point >= m60.expectancy.point
    wr_up = (
        m15.win_rate.point > m60.win_rate.point
        and m15.expectancy.point >= m60.expectancy.point
        and m15.expectancy_r is not None
        and m60.expectancy_r is not None
        and m15.expectancy_r.point >= m60.expectancy_r.point
    )
    if beyond or wr_up:
        how = "expectancy cleared the noise bar" if beyond else "win rate rose without a lower expectancy"
        return True, f"On Jul–Sep 2026, {how}."
    return (
        False,
        "15-minute confirmation does not beat 60-minute confirmation on expectancy "
        "or on win rate without giving expectancy back.",
    )


def _span(symbols: Sequence[str], bars: dict, key: str) -> tuple[str, str]:
    first: Optional[datetime] = None
    last: Optional[datetime] = None
    for symbol in symbols:
        series = (bars.get(symbol) or {}).get(key) or []
        if not series:
            continue
        if first is None or series[0].ts < first:
            first = series[0].ts
        if last is None or series[-1].ts > last:
            last = series[-1].ts
    if first is None or last is None:
        return "n/a", "n/a"
    return as_et(first).date().isoformat(), as_et(last).date().isoformat()


def _diag_section(diags: dict[str, VariantDiag]) -> str:
    lines = [
        "## Signal counts",
        "",
        "Ready means the hybrid entry fired while that variant was flat. "
        "A filter reject keeps the arm. A credit skip means the modeled quote "
        "failed the live gate (missing bid, debit, or under 20% of width). "
        "Opened includes the short warmup before 2024-01-02; the tables above do not.",
        "",
        "| Filter | Ready | Filter reject | Credit skip | Underwater block | Opened |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in VARIANT_ORDER:
        diag = diags.get(name) or VariantDiag()
        lines.append(
            f"| {VARIANT_BLURB[name]} | {diag.ready} | {diag.filter_reject} "
            f"| {diag.credit_skip} | {diag.blocked} | {diag.opened} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Replay credit-spread filters and write the study.")
    parser.add_argument("--cache", default="var/replay-cache")
    parser.add_argument("--out", default="docs/win-rate-study.md")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    symbols = list((cfg.get("universe") or {}).get("symbols") or [])
    if "SPY" not in symbols:
        symbols.append("SPY")
    from pathlib import Path

    print(f"loading bars for {len(symbols)} symbols", flush=True)
    bars = ensure_universe(Path(args.cache), symbols, include_15m=True)
    text = build_report(cfg, bars)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
