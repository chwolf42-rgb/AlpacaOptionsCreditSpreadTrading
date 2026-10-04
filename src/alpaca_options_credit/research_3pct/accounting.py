"""Fees, the 5-spread book, monthly returns, and the train-only selector."""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from typing import Optional, Sequence

from alpaca_options_credit.replay.regime import max_drawdown
from alpaca_options_credit.replay.stats import ReplayTrade, select_risk_book
from alpaca_options_credit.research_3pct.protocol import (
    BOOT_SEED,
    EQUITY,
    MAX_CONCURRENT,
    MAX_OPEN_RISK,
    MIN_TRADES,
    N_BOOT,
    RISK_PCT,
    Interval,
    TrainRow,
    months_between,
    regulatory_fee,
    select_name,
)
from alpaca_options_credit.rth import as_et


def with_fees(trades: Sequence[ReplayTrade]) -> list[ReplayTrade]:
    out: list[ReplayTrade] = []
    for trade in trades:
        fee = regulatory_fee(trade.qty, trade.credit, trade.debit)
        out.append(replace(trade, pnl=trade.pnl - fee))
    return out


def build_book(
    trades: Sequence[ReplayTrade],
    *,
    max_concurrent: int = MAX_CONCURRENT,
    risk_pct: float = RISK_PCT,
    max_open_risk: float = MAX_OPEN_RISK,
    equity: float = EQUITY,
) -> list[ReplayTrade]:
    """Greedy book, then regulatory fees. Fees are not inside the sizer.

    A fee of about twenty cents does not change a 0.5% size on a $100k
    account. Charging it after the book keeps the locked sizer intact.
    """
    sized = select_risk_book(
        list(trades),
        max_concurrent=max_concurrent,
        equity=equity,
        risk_pct=risk_pct,
        max_portfolio_risk_pct=max_open_risk,
    )
    return with_fees(sized)


def in_window(trades: Sequence[ReplayTrade], start: date, end: date) -> list[ReplayTrade]:
    return [t for t in trades if start <= as_et(t.entry_time).date() <= end]


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


def month_pnl(trades: Sequence[ReplayTrade], start: date, end: date) -> list[float]:
    """Dollar P&L by calendar month of the exit, including months with no exit.

    A spread opened in the window and still marked at the tape's end
    contributes on its mark date. Months inside the window with no exit are
    zero, so a quiet book is not a 3% book.
    """
    buckets: dict[tuple[int, int], float] = defaultdict(float)
    for trade in trades:
        exited = as_et(trade.exit_time).date()
        if exited < start or exited > end:
            # Opened in-window but marked after the window. Keep it on the
            # last in-window month so the P&L is not dropped.
            if exited > end:
                buckets[(end.year, end.month)] += trade.pnl
            continue
        buckets[(exited.year, exited.month)] += trade.pnl
    return [buckets[key] for key in months_between(start, end)]


def monthly_returns(pnls: Sequence[float], equity: float = EQUITY) -> list[float]:
    """Each month's dollars divided by the fixed $100k sleeve."""
    if equity <= 0:
        return []
    return [pnl / equity for pnl in pnls]


@dataclass(frozen=True)
class BookReport:
    name: str
    n: int
    wins: int
    monthly: Optional[Interval]
    worst_month: Optional[float]
    pct_positive_months: Optional[float]
    trades_per_month: float
    months: int
    expectancy: Optional[Interval]
    expectancy_r: Optional[Interval]
    win_rate: Optional[Interval]
    max_dd_dollars: float
    max_dd_frac: float
    total_pnl: float
    exit_counts: dict[str, int]


def report_book(
    trades: Sequence[ReplayTrade],
    name: str,
    start: date,
    end: date,
) -> BookReport:
    pnls = month_pnl(trades, start, end)
    rets = monthly_returns(pnls)
    months = len(pnls)
    per = [t.pnl for t in trades]
    rs = [t.r_multiple for t in trades]
    flags = [1.0 if t.pnl > 0 else 0.0 for t in trades]
    counts: dict[str, int] = {}
    for trade in trades:
        counts[trade.exit_reason] = counts.get(trade.exit_reason, 0) + 1
    dd_dollars, dd_frac = max_drawdown(trades, EQUITY) if trades else (0.0, 0.0)
    positive = sum(1 for pnl in pnls if pnl > 0)
    # No trades is not a measured zero return. Quiet months inside a live
    # book stay in the series as zeros.
    measured = bool(trades)
    return BookReport(
        name=name,
        n=len(trades),
        wins=sum(1 for t in trades if t.pnl > 0),
        monthly=bootstrap_mean(rets) if measured else None,
        worst_month=(min(rets) if measured else None),
        pct_positive_months=(positive / months if measured else None),
        trades_per_month=(len(trades) / months if months else 0.0),
        months=months,
        expectancy=bootstrap_mean(per),
        expectancy_r=bootstrap_mean(rs),
        win_rate=bootstrap_mean(flags),
        max_dd_dollars=dd_dollars,
        max_dd_frac=dd_frac,
        total_pnl=sum(per),
        exit_counts=counts,
    )


def train_row(name: str, searchable: bool, book: BookReport) -> TrainRow:
    return TrainRow(
        name=name,
        searchable=searchable,
        n=book.n,
        monthly=book.monthly,
        expectancy_r=book.expectancy_r,
    )


def chosen_name(rows: Sequence[TrainRow]) -> Optional[str]:
    return select_name(rows, MIN_TRADES)

