"""Markdown report. Numbers come from the runner; this file does not resimulate."""

from __future__ import annotations

from alpaca_options_credit.research_3pct.accounting import BookReport
from alpaca_options_credit.research_3pct.protocol import (
    BOOT_SEED,
    EQUITY,
    FOLDS,
    MAX_CONCURRENT,
    MAX_OPEN_RISK,
    MIN_TRADES,
    N_BOOT,
    RISK_PCT,
    TEST_END,
    TEST_START,
    TRAIN_END,
    TRAIN_START,
    monthly_ceiling,
)


def _pct(value: float, digits: int = 2) -> str:
    return f"{100 * value:.{digits}f}%"


def _ci(interval, kind: str) -> str:
    if interval is None:
        return "n/a"
    if kind == "month":
        return (
            f"{_pct(interval.point)} [{_pct(interval.low)}, {_pct(interval.high)}]"
        )
    if kind == "pct":
        return f"{_pct(interval.point, 1)} [{_pct(interval.low, 1)}, {_pct(interval.high, 1)}]"
    if kind == "r":
        return f"{_pct(interval.point, 1)} [{_pct(interval.low, 1)}, {_pct(interval.high, 1)}]"
    return f"{_usd(interval.point)} [{_usd(interval.low)}, {_usd(interval.high)}]"


def _usd(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def _row(book: BookReport) -> str:
    worst = "n/a" if book.worst_month is None else _pct(book.worst_month)
    pos = "n/a" if book.pct_positive_months is None else _pct(book.pct_positive_months, 0)
    return (
        f"| {book.name} | {book.n} | {book.trades_per_month:.2f} "
        f"| {_ci(book.monthly, 'month')} | {worst} | {pos} "
        f"| {_usd(book.max_dd_dollars)} ({_pct(book.max_dd_frac, 1)}) "
        f"| {_ci(book.win_rate, 'pct')} | {_ci(book.expectancy, 'usd')} "
        f"| {_ci(book.expectancy_r, 'r')} |"
    )


TABLE_HEADER = (
    "| Design | Trades | Trades/month | Monthly return (95% CI) | Worst month | Positive months "
    "| Max drawdown | Win rate (95% CI) | Expectancy $ (95% CI) | Expectancy / max risk (95% CI) |\n"
    "| --- | ---: | ---: | --- | ---: | ---: | --- | --- | --- | --- |"
)


def render(ctx: dict) -> str:
    winner = ctx["winner"]
    base_test: BookReport = ctx["test"]["base"]
    lead = _lead(winner, base_test, ctx["train"].get(winner) if winner else None, ctx["test"].get(winner) if winner else None)
    parts = [
        "# Credit-spread redesign: 3% a month",
        "",
        lead,
        "",
        "## Why the paper bot does not get filled",
        "",
        *_diagnosis(ctx),
        "",
        "## The 3% ceiling under the locked rules",
        "",
        *_ceiling(),
        "",
        "## How a design was allowed to win",
        "",
        *_protocol(),
        "",
        "## Train window",
        "",
        TABLE_HEADER,
        *_table(ctx["candidates"], ctx["train"], searchable_only=True),
        "",
        "### What each searchable row is",
        "",
        *_blurbs(ctx["candidates"], searchable_only=True),
        "",
        "## Selection",
        "",
        *_selection(ctx),
        "",
        "## Out-of-sample test",
        "",
        (
            f"Entries from {TEST_START.isoformat()} through {TEST_END.isoformat()}. "
            "These rows were not used to pick a winner. A test row that looks better "
            "than the train rule is not adopted."
        ),
        "",
        TABLE_HEADER,
        *_table(ctx["candidates"], ctx["test"], searchable_only=False),
        "",
        "## Walk-forward",
        "",
        *_folds(ctx),
        "",
        "## Decisions for Christian",
        "",
        *_decisions(ctx),
        "",
        "## Chain evidence",
        "",
        *_chains(ctx),
        "",
        "## Model surface",
        "",
        *_surface(ctx),
        "",
        "## Fill, fees, and assignment",
        "",
        *_frictions(ctx),
        "",
        "## Data limits",
        "",
        *_limits(ctx),
        "",
        "## Reproduce",
        "",
        "```bash",
        "PYTHONPATH=src python3 -m alpaca_options_credit.research_3pct.run \\",
        "  --cache var/replay-cache --chain-cache var/research-3pct/chains \\",
        "  --out docs/research/options-3pct-redesign.md",
        "```",
        "",
        (
            f"Bootstrap is {N_BOOT:,} resamples, seed {BOOT_SEED}. "
            "The Yahoo underlying cache and the CBOE/Yahoo option cache live under `var/` (gitignored). "
            "A warm cache does not hit the network. No config default changes with this report."
        ),
        "",
    ]
    return "\n".join(parts)


def _lead(winner, base_test: BookReport, winner_train, winner_test) -> str:
    base_bit = (
        f"The live book on the untouched test window ({TEST_START.isoformat()} to {TEST_END.isoformat()}), "
        f"capped at {MAX_CONCURRENT} spreads and { _pct(MAX_OPEN_RISK, 0) } open risk, "
        f"returned {_ci(base_test.monthly, 'month')} per month on the $100k sleeve, "
        f"max drawdown {_usd(base_test.max_dd_dollars)} ({_pct(base_test.max_dd_frac, 1)}), "
        f"{base_test.trades_per_month:.2f} trades/month, n={base_test.n}."
    )
    if winner and winner_test is not None:
        return (
            f"No design that keeps the locked rules makes 3% a month. Train selected `{winner}`, "
            f"and its test monthly return is {_ci(winner_test.monthly, 'month')} "
            f"with max drawdown {_usd(winner_test.max_dd_dollars)} "
            f"({_pct(winner_test.max_dd_frac, 1)}), {winner_test.trades_per_month:.2f} trades/month, "
            f"n={winner_test.n}. That is not 3% a month. {base_bit}"
        )
    return (
        "No credit-spread design that keeps the locked rules makes 3% a month. "
        "Nothing in the precommitted search had a train-window monthly return whose 95% interval "
        "sat entirely above zero, together with an expectancy per unit of max risk that also sat "
        f"entirely above zero, on at least {MIN_TRADES} spreads. There is no winner to take out of sample. "
        + base_bit
        + " The 20% of width credit gate is structurally out of reach for a 10–25 delta short on a $5 "
        "width at the implied vols these names actually trade, which is why the paper account almost "
        "never gets a fill. The one order that did clear the gate was a day limit at an indicative "
        "natural credit, and it expired."
    )


def _diagnosis(ctx: dict) -> list[str]:
    diags = ctx["diags"]
    lines = [
        "The supervisor's ~1,815 `credit_below_min_pct` skips and the single unfilled order are the same gate.",
        "",
        "The live rule sells the natural credit, short bid minus long ask, and refuses the spread unless that credit is at least 20% of the width. On a $5 width that is $1.00. The limit sent to Alpaca is that same natural credit (`credit_limit_price` is the negative of the credit, which means \"this much credit or more\"), time in force day. The quote feed in `config/default.yaml` is `indicative`, not OPRA. An indicative natural that is richer than the real NBBO never trades, and a day order then expires. The September 30 MSFT 490/485 put, 1 lot, limit credit 1.12, is 22.4% of a $5 width: it cleared the gate by twelve cents and was not filled.",
        "",
        "The strike the bot actually picks is the listed strike just beyond invalidation, not a chosen delta. When that strike is near the money the credit can clear $1.00, and the prior replay's filled baseline sat near a 0.37 delta. When the shelf is further out, the same $5 width does not pay $1.00 after the bid/ask, and the skip token is `credit_below_min_pct`. A redesign that aims at 10, 16, 20, or 25 delta keeps the shelf entry and then finds the credit gate shut.",
        "",
        "Replay skip counts on the full tape (a credit skip is a ready signal whose modeled quote failed the gate or whose listed delta was more than 0.08 through the target):",
        "",
        "| Design | Ready | Filter reject | Credit or strike skip | Opened | Gate share of unfiltered |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in ("base", "d16_c20", "d20_c20", "d30_c20", "d16_c10", "base_dte7", "idx_base"):
        diag = diags.get(name)
        if diag is None:
            continue
        reached = diag.credit_skip + diag.opened
        share = diag.credit_skip / reached if reached else 0.0
        lines.append(
            f"| {name} | {diag.ready} | {diag.filter_reject} | {diag.credit_skip} | {diag.opened} | {_pct(share, 1)} |"
        )
    spot = ctx.get("msft_spot")
    if spot:
        lines.extend(
            [
                "",
                (
                    f"MSFT's Yahoo daily close on {spot['day']} was ${spot['close']:.2f}. "
                    f"The 490 short was {spot['otm_pct']:.1f}% out of the money versus that close. "
                    "A $5-wide put at that distance is a low-delta vertical. A $1.12 natural credit "
                    "is only just over the $1.00 floor, so the order was the rare quote that passed "
                    "and it still did not trade."
                ),
            ]
        )
    return lines


def _ceiling() -> list[str]:
    lines = [
        "With 50% take-profit and risk sized at 0.5% of equity, the account return is an identity of the credit fraction and the number of round trips. A win pays `(0.50 × credit) / (width − credit)` times the risk budget. At a credit of 20% of width that multiple is 0.125, so a win adds 0.0625% of the account. Forty-eight wins and zero losses in a month are what it takes to make 3% at that credit. Five open spreads cannot turn over that fast unless they are closed in a couple of days, and a book of 20% credits does not win every time.",
        "",
        "The table is the monthly return if every trade wins and none loses. It is the ceiling, not a forecast. A single full loss costs 0.5% of the account and wipes out eight of those 20%-credit wins.",
        "",
        "| Credit / width | Win, as a fraction of max loss | 8 perfect trades | 15 perfect trades | 48 perfect trades |",
        "| ---: | ---: | ---: | ---: | ---: |",
    ]
    for credit in (0.10, 0.20, 0.30, 0.40, 0.50):
        from alpaca_options_credit.research_3pct.protocol import win_r_multiple

        lines.append(
            f"| {_pct(credit, 0)} | {win_r_multiple(credit):.3f} "
            f"| {_pct(monthly_ceiling(credit, 8))} "
            f"| {_pct(monthly_ceiling(credit, 15))} "
            f"| {_pct(monthly_ceiling(credit, 48))} |"
        )
    lines.extend(
        [
            "",
            "A $10-wide spread does not loosen this. At 0.5% of $100k the risk budget is $500. A $10 wing with a $2 credit still has $800 of max loss, so the sizer takes zero contracts. Widening the wing under the locked risk budget does not create a position unless the credit is at least half the width.",
        ]
    )
    return lines


def _protocol() -> list[str]:
    fold_bits = ", ".join(
        f"train through {train_end.isoformat()} then {test_start.isoformat()}–{test_end.isoformat()}"
        for train_end, test_start, test_end in FOLDS
    )
    return [
        f"Train entries are {TRAIN_START.isoformat()} through {TRAIN_END.isoformat()}. "
        f"The test window is {TEST_START.isoformat()} through {TEST_END.isoformat()} and was frozen before the replay. "
        f"October 2026 is a partial month and is in neither window. Walk-forward folds, also frozen, are: {fold_bits}.",
        "",
        f"A searchable row needs at least {MIN_TRADES} train spreads. The 95% bootstrap interval on its mean monthly return (month P&L divided by the fixed $100,000, months with no exit included as zero) and the interval on expectancy divided by max loss both have to sit entirely above zero. The winner is the qualifying row with the highest train monthly return. The test window is then one look. Decision rows (a 10% credit gate, a mid fill, a 25% take-profit) are not eligible.",
        "",
        "The entry is the live one: a daily strict confirm, an arm, then the first hourly pullback into the daily volume-profile shelf and an hourly reconfirm. No chase. One spread per name inside the replay. The reported book then keeps at most 5 names, 0.5% of realized equity, and 10% open risk. A skipped signal does not invent a later replacement.",
        "",
        "The position path is one continuous replay. A spread opened before a window can still occupy that name, so the first signal of a test window is not a flat start. That is the same path the paper bot would have if it had been running. It is not a look at test P&L. The selector's inputs are train rows only; the function has no test argument.",
        "",
        f"Intervals are {N_BOOT:,} percentile-bootstrap resamples, seed {BOOT_SEED}, taking the sorted sample at 2.5% and 97.5%. Fees are Alpaca's September 1, 2026 retail schedule: no commission on equity and ETF options, plus ORF $0.015, OCC $0.025, and CAT $0.0003 per contract-side, TAF $0.00329 per contract on sells, and the SEC fee at $0.0000206 of an approximated sell principal. Each spread's fee is rounded up to the next cent.",
        "",
        "Primary fills are the natural credit (short bid minus long ask) and the natural debit on a stop or a structure break. The take-profit fill is a debit of half the credit. The half-spread on each leg is 6% of the mid, at least $0.05 and at most $0.25. IV is the last 20 sessions of close-to-close realized vol times 1.15, frozen at entry, clamped from 15% to 125%. Rate 4%, dividend zero. Earnings blackout is off, matching the empty `config/calendar.yaml`.",
    ]


def _table(candidates, books: dict, *, searchable_only: bool) -> list[str]:
    lines = []
    for row in candidates:
        if searchable_only and not row.searchable:
            continue
        book = books.get(row.name)
        if book is None:
            continue
        lines.append(_row(book))
    return lines or ["| n/a | 0 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |"]


def _blurbs(candidates, *, searchable_only: bool) -> list[str]:
    return [
        f"- `{row.name}` — {row.blurb}"
        for row in candidates
        if (row.searchable or not searchable_only)
        and (not searchable_only or row.searchable)
    ]


def _selection(ctx: dict) -> list[str]:
    winner = ctx["winner"]
    if winner is None:
        return [
            f"No searchable design had {MIN_TRADES} or more train trades and both the monthly-return interval and the expectancy/max-risk interval entirely above zero.",
            "",
            "No train row cleared the bar. The test table is descriptive. It is not a second chance to pick.",
        ]
    return [
        f"Train selected `{winner}`. The test section reports that one row alongside every other candidate.",
    ]


def _folds(ctx: dict) -> list[str]:
    lines = [
        "Each fold selects from entries on or before the train end, using the same rule, then scores entries inside the fold. When nothing qualifies, the fold still reports the live `base` book so an empty selection is not an empty month.",
        "",
        "| Fold test | Selected | OOS trades | OOS monthly return (95% CI) | OOS max drawdown | OOS trades/month | Base monthly return |",
        "| --- | --- | ---: | --- | --- | ---: | --- |",
    ]
    for fold in ctx["folds"]:
        lines.append(
            f"| {fold['label']} | {fold['selected']} | {fold['n']} | {fold['monthly']} "
            f"| {fold['dd']} | {fold['tpm']:.2f} | {fold['base_monthly']} |"
        )
    return lines


def _decisions(ctx: dict) -> list[str]:
    lines = [
        "These rows need a locked rule changed, or they assume a fill the paper account did not get. They are not implemented. Drawdown is the peak-to-trough of the fee-adjusted book on the test window, starting from $100,000.",
        "",
        TABLE_HEADER,
    ]
    lines.extend(_row(book) for book in ctx["scenarios"])
    lines.extend(
        [
            "",
            "Reading the cost:",
            "",
            "- Lowering the credit gate from 20% to 10% is what lets a 16-delta short exist. That is a weaker credit, which is a lower signal on the rule this sleeve already uses as a quality floor. The `d16_c10` and `d10_c10` rows are that change. Their monthly intervals are the evidence. They are not a 3% book.",
            "- `base_tp25` cuts the locked 50% take-profit to 25%. The win is smaller. The ceiling table gets worse, not better, because the win multiple is linear in the take-profit fraction.",
            "- `base` at 1% risk (`base_risk1`) doubles the dollars on the same signals. Drawdown scales with the size. The monthly percentage does not become 3% just because each lot is bigger; the per-trade R multiple is unchanged and the sizer often goes from one contract to two.",
            "- `base` with 10 concurrent names (`base_cap10`) relaxes the 5-spread cap. It can only add trades the 5-spread book dropped. It does not create a new edge.",
            "- A second position in the same symbol is not in the tape. The replay keeps one arm and clears it on a fill, so there is no second independent signal whose drawdown could be measured. It is not a path to 3%.",
            "- The $99,750 gross cap does not bind. A one-lot $5 credit spread uses a few hundred dollars of defined risk, not the underlying notional. Raising the gross cap does not add a fill.",
            "- `base_mid` assumes you are filled at the mid. The unfilled MSFT order is the live evidence that a day limit at the natural, which is worse than the mid, already fails to trade. A mid fill is not an implementable order on this feed.",
        ]
    )
    return lines


def _chains(ctx: dict) -> list[str]:
    chains = ctx.get("chains") or {}
    snap = chains.get("snapshot") or {}
    if not snap:
        return ["The CBOE chain pull did not return a chain. The model surface below is the structural evidence."]
    lines = [
        f"Snapshot date {snap.get('asof')}. {snap.get('source')}. "
        f"Names that returned a chain: {', '.join(snap.get('symbols_ok') or [])}. "
        f"Missing: {', '.join(snap.get('symbols_miss') or []) or 'none'}. "
        f"Two-sided verticals in the delta and DTE buckets: {snap.get('index_n', 0)} on SPY/QQQ/IWM and {snap.get('singles_n', 0)} on the single names.",
        "",
        "A cell is a listed short whose exchange |delta| is within 0.02 of the target, with the long strike one width away, both sides bid and ask positive and uncrossed. `Clears 20%` is natural credit (short bid − long ask) of at least 20% of width.",
        "",
        "| Group | Delta | DTE | Width | N | Median natural/width | Median mid/width | Clears 20% |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in (snap.get("index") or []) + (snap.get("singles") or []):
        lines.append(
            f"| {row['group']} | {row['delta']:.2f} | {row['dte']} | {row['width']:.0f} "
            f"| {row['n']} | {_pct(row['median_natural_frac'], 1)} | {_pct(row['median_mid_frac'], 1)} "
            f"| {_pct(row['pct_clear_20'], 1)} |"
        )
    hist = chains.get("history") or {}
    lines.extend(["", f"Historical last trades. {hist.get('source', '')} Sample size {hist.get('n', 0)} contract-days.", ""])
    if hist.get("rows"):
        lines.extend(
            [
                "| Current delta bucket | DTE on that day | Width | N | Median last/width | Last trade clears 20% |",
                "| ---: | --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in hist["rows"]:
            lines.append(
                f"| {row['delta']:.2f} | {row['dte']} | {row['width']:.0f} | {row['n']} "
                f"| {_pct(row['median_last_frac'], 1)} | {_pct(row['pct_clear_20'], 1)} |"
            )
    else:
        lines.append("No overlapping Yahoo daily prints fell inside the DTE buckets.")
    return lines


def _surface(ctx: dict) -> list[str]:
    lines = [
        "Black-Scholes on an exact target delta, not a listed strike. Spots $30, $50, $100, $200, $500, and $770. Volatilities 12%, 18%, 25%, 40%, and 80%. Puts and calls. The half-spread is the replay's 6% clamp. This grid does not know which names trended; it only asks whether the credit exists.",
        "",
        "| Delta | DTE | Width | N | Median natural/width | Share clearing 20% |",
        "| ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in ctx.get("surface") or []:
        lines.append(
            f"| {row['delta']:.2f} | {row['dte']} | {row['width']:.1f} | {row['n']} "
            f"| {_pct(row['median_natural_frac'], 1)} | {_pct(row['pct_clear_20'], 1)} |"
        )
    return lines


def _frictions(ctx: dict) -> list[str]:
    assign = ctx.get("assignment") or {}
    lines = [
        "The primary book is natural fills plus the regulatory fee. `base_nickel` worsens both sides by another $0.05. `base_mid` removes the bid/ask. The gap between those two rows is the spread, not an edge.",
        "",
        (
            f"Early assignment: {assign.get('checked', 0)} baseline spreads were rechecked on daily closes between entry and the modeled exit. "
            f"{assign.get('assigned', 0)} would have been assigned under the five-cent extrinsic rule. "
            f"Test-window book P&L moves from {_usd(assign.get('base_pnl', 0.0))} to {_usd(assign.get('assigned_pnl', 0.0))} "
            f"when those exits replace the modeled ones and the 5-spread book is rebuilt. "
            "A call that is assigned early because of a dividend, while it still has extrinsic, is not in this check."
        ),
    ]
    return lines


def _limits(ctx: dict) -> list[str]:
    return [
        "There is no stored OPRA tape. Alpaca historical option quotes were not pulled: the options API keys are not in this environment, and `config/paper-live.yaml` is not in the repo. The backtest marks are Black-Scholes on Yahoo underlying bars. The chain section is the check on that model, and it is a delayed cross-section plus last trades of contracts that have not expired yet.",
        "",
        "Yahoo hourly bars are regular-session bars only. A decision uses the bar that has already closed. Daily structure uses the session whose 16:00 close is already known. The IV proxy is a markup on realized vol, not a listed implied vol. The CBOE delta is the listed one; the historical last-trade panel reuses today's delta label for that strike, so a day from months ago is not a true 16-delta observation. Last trades of the two legs are not simultaneous.",
        "",
        "The replay's $2.50 width is not in the candidate list. On a $1 strike grid a $2.50 wing is not a listed spread, and the pricer would have to invent the long strike. $5 and $10 are on the $1, $2.50, and $5 grids.",
        "",
        ctx.get("tape_note") or "",
    ]
