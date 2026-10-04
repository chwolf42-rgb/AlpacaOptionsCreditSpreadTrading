"""Account-level metrics and the pre-declared block bootstrap."""

from __future__ import annotations

import math
import random
from datetime import date

from backtests.new_strategies.specs import (
    BOOTSTRAP_BLOCK,
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
)


def month_keys(start: date, end: date) -> list[str]:
    keys: list[str] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        keys.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return keys


def monthly_returns(trades: list[dict], start: date, end: date, account: float) -> list[dict]:
    """P&L of trades closed in each calendar month, divided by the fixed account.

    Months with no exit are zero. The denominator does not compound.
    """
    buckets = {key: 0.0 for key in month_keys(start, end)}
    counts = {key: 0 for key in buckets}
    for trade in trades:
        exit_day: date = trade["exit_date"]
        if exit_day < start or exit_day > end:
            continue
        key = f"{exit_day.year:04d}-{exit_day.month:02d}"
        if key not in buckets:
            continue
        buckets[key] += float(trade["pnl"])
        counts[key] += 1
    return [
        {"month": key, "return": buckets[key] / account, "trades": counts[key]}
        for key in buckets
    ]


def block_bootstrap_ci(
    returns: list[float],
    *,
    block: int = BOOTSTRAP_BLOCK,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict:
    """Circular block bootstrap of the mean. 90% interval, 5th and 95th.

    Index method: on the sorted sample of ``resamples`` means, lo is
    element floor(0.05 * resamples) and hi is element floor(0.95 * resamples).
    """
    n = len(returns)
    if n == 0:
        return {"mean": None, "lo": None, "hi": None, "n": 0}
    mean = sum(returns) / n
    if n == 1:
        return {"mean": mean, "lo": mean, "hi": mean, "n": 1, "resamples": resamples, "seed": seed, "block": block}
    rng = random.Random(seed)
    block = max(1, min(block, n))
    means: list[float] = []
    for _ in range(resamples):
        acc: list[float] = []
        while len(acc) < n:
            start = rng.randrange(n)
            for k in range(block):
                acc.append(returns[(start + k) % n])
                if len(acc) >= n:
                    break
        means.append(sum(acc) / n)
    means.sort()
    lo = means[int(math.floor(0.05 * resamples))]
    hi = means[min(resamples - 1, int(math.floor(0.95 * resamples)))]
    return {
        "mean": mean,
        "lo": lo,
        "hi": hi,
        "n": n,
        "resamples": resamples,
        "seed": seed,
        "block": block,
    }


def max_drawdown(equity: list[float]) -> dict:
    """Peak-to-trough on a mark-to-market equity curve. Returns dollars and fraction of peak."""
    if not equity:
        return {"dollars": 0.0, "pct": 0.0}
    peak = equity[0]
    worst = 0.0
    worst_pct = 0.0
    for value in equity:
        if value > peak:
            peak = value
        dd = peak - value
        if dd > worst:
            worst = dd
            worst_pct = dd / peak if peak else 0.0
    return {"dollars": worst, "pct": worst_pct}


def trade_stats(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "n": 0,
            "win_rate": None,
            "avg_r": None,
            "profit_factor": None,
            "pnl": 0.0,
        }
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    if gross_loss > 0:
        pf = gross_win / gross_loss
    elif gross_win > 0:
        pf = None  # infinite; reported as null and noted
    else:
        pf = None
    avg_r = sum(t["r"] for t in trades) / n
    return {
        "n": n,
        "win_rate": len(wins) / n,
        "avg_r": avg_r,
        "profit_factor": pf,
        "pnl": sum(t["pnl"] for t in trades),
        "n_window_mtm": sum(1 for t in trades if t["reason"] == "window_mtm"),
    }


def exposure_stats(rows: list[dict]) -> dict:
    if not rows:
        return {
            "avg_spreads": 0.0,
            "peak_spreads": 0,
            "avg_open_risk": 0.0,
            "peak_open_risk": 0.0,
            "avg_gross": 0.0,
            "peak_gross": 0.0,
            "days": 0,
        }
    return {
        "avg_spreads": sum(r["spreads"] for r in rows) / len(rows),
        "peak_spreads": max(r["spreads"] for r in rows),
        "avg_open_risk": sum(r["open_risk"] for r in rows) / len(rows),
        "peak_open_risk": max(r["open_risk"] for r in rows),
        "avg_gross": sum(r["gross"] for r in rows) / len(rows),
        "peak_gross": max(r["gross"] for r in rows),
        "days": len(rows),
    }


def summarize_window(
    trades: list[dict],
    equity: list[float],
    exposure: list[dict],
    start: date,
    end: date,
    account: float,
) -> dict:
    months = monthly_returns(trades, start, end, account)
    rets = [m["return"] for m in months]
    ci = block_bootstrap_ci(rets)
    stats = trade_stats(trades)
    n_months = len(months)
    worst = min(rets) if rets else None
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "n_months": n_months,
        "monthly": ci,
        "worst_month": worst,
        "max_drawdown": max_drawdown(equity),
        "trades_per_month": (stats["n"] / n_months) if n_months else 0.0,
        "trade_stats": stats,
        "exposure": exposure_stats(exposure),
        "months": months,
    }


def select_winner(scored: list[dict], min_trades: int) -> dict:
    """Highest mean monthly return. Ties within 0.01 bp go to lower drawdown, then lower grid index.

    If none has ``min_trades`` closed trades, the textbook default (index 0) is kept.
    """
    eligible = [row for row in scored if row["n_trades"] >= min_trades]
    pool = eligible if eligible else [row for row in scored if row["grid_index"] == 0]
    if not pool:
        pool = scored

    def key(row: dict) -> tuple:
        mean = row["mean_monthly"]
        if mean is None:
            mean = -1e9
        return (mean, -row["max_dd"], -row["grid_index"])

    return max(pool, key=key)
