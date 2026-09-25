"""Bootstrap summaries for the replay trade list.

Intervals are percentile bootstrap confidence intervals (5,000 resamples).
Expectancy in dollars is the mean sized P&L. Expectancy as a share of max
risk is the mean of P&L / that spread's max loss, so a 1-lot and a 2-lot
book can be compared. A win is P&L > 0. Open marks are included; they are
not dropped.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Optional, Sequence

from alpaca_options_credit.risk import size_contracts


N_BOOT = 5000
BOOT_SEED = 20260925


@dataclass(frozen=True)
class ReplayTrade:
    symbol: str
    side: str
    variant: str
    entry_time: datetime
    exit_time: datetime
    exit_reason: str
    credit: float
    debit: float
    width: float
    qty: int
    max_loss: float
    pnl: float
    short_strike: float
    iv: float

    @property
    def r_multiple(self) -> float:
        if self.max_loss <= 0:
            return 0.0
        return self.pnl / self.max_loss

    @property
    def win(self) -> bool:
        return self.pnl > 0


@dataclass(frozen=True)
class Interval:
    point: float
    low: float
    high: float


@dataclass(frozen=True)
class BookStats:
    label: str
    n: int
    wins: int
    losses: int
    open_mtm: int
    trades_per_week: float
    weeks: float
    win_rate: Optional[Interval]
    expectancy: Optional[Interval]
    expectancy_r: Optional[Interval]
    avg_win: Optional[float]
    avg_loss: Optional[float]
    exit_counts: dict[str, int]


def bootstrap_mean(
    values: Sequence[float],
    *,
    n_boot: int = N_BOOT,
    seed: int = BOOT_SEED,
) -> Optional[Interval]:
    if not values:
        return None
    point = sum(values) / len(values)
    if len(values) == 1:
        return Interval(point, point, point)
    rng = random.Random(seed)
    n = len(values)
    stats = []
    for _ in range(n_boot):
        total = 0.0
        for _i in range(n):
            total += values[rng.randrange(n)]
        stats.append(total / n)
    stats.sort()
    low = stats[int(0.025 * n_boot)]
    high = stats[min(n_boot - 1, int(0.975 * n_boot))]
    return Interval(point, low, high)


def bootstrap_diff(
    left: Sequence[float],
    right: Sequence[float],
    *,
    n_boot: int = N_BOOT,
    seed: int = BOOT_SEED,
) -> Optional[Interval]:
    """Percentile interval for mean(left) − mean(right)."""
    if not left or not right:
        return None
    point = (sum(left) / len(left)) - (sum(right) / len(right))
    rng = random.Random(seed)
    n_l, n_r = len(left), len(right)
    stats = []
    for _ in range(n_boot):
        mean_l = sum(left[rng.randrange(n_l)] for _i in range(n_l)) / n_l
        mean_r = sum(right[rng.randrange(n_r)] for _i in range(n_r)) / n_r
        stats.append(mean_l - mean_r)
    stats.sort()
    low = stats[int(0.025 * n_boot)]
    high = stats[min(n_boot - 1, int(0.975 * n_boot))]
    return Interval(point, low, high)


def summarize(
    trades: Sequence[ReplayTrade],
    *,
    label: str,
    weeks: float,
) -> BookStats:
    pnls = [t.pnl for t in trades]
    rs = [t.r_multiple for t in trades]
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    flags = [1.0 if t.win else 0.0 for t in trades]
    counts: dict[str, int] = {}
    for trade in trades:
        counts[trade.exit_reason] = counts.get(trade.exit_reason, 0) + 1
    week_span = weeks if weeks > 0 else 0.0
    return BookStats(
        label=label,
        n=len(trades),
        wins=len(wins),
        losses=len(losses),
        open_mtm=sum(1 for t in trades if t.exit_reason == "open_mtm"),
        trades_per_week=(len(trades) / week_span) if week_span else 0.0,
        weeks=week_span,
        win_rate=bootstrap_mean(flags),
        expectancy=bootstrap_mean(pnls),
        expectancy_r=bootstrap_mean(rs),
        avg_win=(sum(wins) / len(wins)) if wins else None,
        avg_loss=(sum(losses) / len(losses)) if losses else None,
        exit_counts=counts,
    )


def slice_window(
    trades: Sequence[ReplayTrade],
    start: datetime,
    end: datetime,
) -> list[ReplayTrade]:
    """Trades opened inside ``[start, end]`` (entry time, inclusive)."""
    return [t for t in trades if start <= t.entry_time <= end]


def select_risk_book(
    trades: Sequence[ReplayTrade],
    *,
    max_concurrent: int,
    equity: float = 100_000.0,
    risk_pct: float = 0.005,
    max_portfolio_risk_pct: float = 0.10,
    multiplier: int = 100,
) -> list[ReplayTrade]:
    """Greedy book. Exits at or before an entry free the slot and the cash first.

    Sizing uses realized equity (closed P&L only) and the same 0.5% / 10%
    rules as ``risk.decide``. One name at a time is already true of the
    per-symbol replay; this adds the book-level caps. A skipped signal does
    not invent a later replacement on that name.
    """
    ordered = sorted(trades, key=lambda t: (t.entry_time, t.symbol))
    open_book: list[ReplayTrade] = []
    accepted: list[ReplayTrade] = []
    equity_now = equity

    def _release(as_of: datetime) -> None:
        nonlocal equity_now, open_book
        still: list[ReplayTrade] = []
        for held in open_book:
            if held.exit_time <= as_of:
                equity_now += held.pnl
            else:
                still.append(held)
        open_book = still

    for trade in ordered:
        _release(trade.entry_time)
        if any(held.symbol == trade.symbol for held in open_book):
            continue
        if len(open_book) >= max_concurrent:
            continue
        per_contract = (trade.width - trade.credit) * multiplier
        if per_contract <= 0:
            continue
        qty = size_contracts(equity_now, risk_pct, trade.width, trade.credit, multiplier)
        if qty < 1:
            continue
        this_loss = per_contract * qty
        open_risk = sum(held.max_loss for held in open_book)
        if open_risk + this_loss > equity_now * max_portfolio_risk_pct + 1e-9:
            continue
        pnl = (trade.credit - trade.debit) * qty * multiplier
        sized = replace(trade, qty=qty, max_loss=this_loss, pnl=pnl)
        accepted.append(sized)
        open_book.append(sized)
    return accepted
