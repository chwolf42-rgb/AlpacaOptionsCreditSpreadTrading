"""Frozen study protocol. Written before any trade result is computed.

The split dates, the seed, the fee schedule, the selection rule, and the
arithmetic ceiling are constants. ``select_name`` sees train rows only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence

# Expanding history is available from the Yahoo hourly tape (about Nov 2023).
# Train is the first 18 months of 2024-2025. Test starts the next session and
# stops at the last completed month. October 2026 is a partial month and is
# not in either window. These bounds do not move after a result is seen.
TRAIN_START = date(2024, 1, 2)
TRAIN_END = date(2025, 6, 30)
TEST_START = date(2025, 7, 1)
TEST_END = date(2026, 9, 30)

# Walk-forward: expanding train that ends the session before the fold, then
# that fold only. Selection inside a fold cannot see the fold.
FOLDS: tuple[tuple[date, date, date], ...] = (
    (date(2024, 6, 30), date(2024, 7, 1), date(2024, 12, 31)),
    (date(2024, 12, 31), date(2025, 1, 2), date(2025, 6, 30)),
    (date(2025, 6, 30), date(2025, 7, 1), date(2025, 12, 31)),
    (date(2025, 12, 31), date(2026, 1, 2), date(2026, 9, 30)),
)

EQUITY = 100_000.0
RISK_PCT = 0.005
MAX_CONCURRENT = 5
MAX_OPEN_RISK = 0.10
TP_FRAC = 0.50
MIN_CREDIT = 0.20

# A train row under this many spreads cannot clear the bar. The same count
# is required on the test window before a monthly-return interval is a claim.
MIN_TRADES = 30

N_BOOT = 5000
BOOT_SEED = 20261002

# Alpaca Securities brokerage fee schedule, updated September 1, 2026.
# Equity and ETF options are commission-free for retail. These are the
# pass-through regulatory fees. Index options ($0.50/contract) are not in
# this universe. Each spread leg is one contract; open and close are both
# charged. ORF and OCC and CAT apply to buys and sells. TAF and the SEC
# fee apply to sells only.
FEE_ORF = 0.015
FEE_OCC = 0.025
FEE_CAT_PER_SHARE = 0.000003
FEE_TAF_SELL = 0.00329
FEE_SEC_RATE = 0.0000206  # of sell principal, from April 4, 2026
CONTRACT_SHARES = 100


@dataclass(frozen=True)
class Interval:
    point: float
    low: float
    high: float


def months_between(start: date, end: date) -> list[tuple[int, int]]:
    """Inclusive calendar months from ``start`` through ``end``."""
    out: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        m += 1
        if m == 13:
            m = 1
            y += 1
    return out


def win_r_multiple(credit_frac: float, tp_frac: float = TP_FRAC) -> float:
    """Locked take-profit, as a fraction of max loss, if the spread is a winner.

    Credit is ``credit_frac`` of width. Max loss per share is width minus
    credit. Capturing ``tp_frac`` of the credit leaves a profit of
    ``tp_frac * credit``. The ratio does not depend on width or on the
    account, only on how rich the credit is.
    """
    if credit_frac <= 0 or credit_frac >= 1:
        return 0.0
    return (tp_frac * credit_frac) / (1.0 - credit_frac)


def monthly_ceiling(
    credit_frac: float,
    trades_per_month: float,
    *,
    tp_frac: float = TP_FRAC,
    risk_pct: float = RISK_PCT,
    win_rate: float = 1.0,
    loss_r: float = 1.0,
) -> float:
    """Account return per month if every trade is this credit fraction.

    A win pays ``win_r_multiple`` times the risk budget. A loss costs
    ``loss_r`` times the risk budget (1.0 is a full max-loss). With the
    locked 0.5% risk budget this is an identity, not a backtest.
    """
    r_win = win_r_multiple(credit_frac, tp_frac)
    expectancy_r = win_rate * r_win - (1.0 - win_rate) * loss_r
    return trades_per_month * expectancy_r * risk_pct


def regulatory_fee(qty: int, credit: float, debit: float) -> float:
    """Dollars charged to open and close ``qty`` spread units.

    Four contract-sides per unit (short and long, open and close). Two of
    those sides are sells: sell-to-open the short, sell-to-close the long.
    Sell principal is approximated as the spread credit on the open and the
    spread debit on the close. That understates the short-leg principal and
    is labeled as such in the report. The per-spread total is rounded up to
    the next cent, which is harsher than Alpaca's end-of-day rounding.
    """
    if qty <= 0:
        return 0.0
    sides = 4 * qty
    per_contract = FEE_ORF + FEE_OCC + FEE_CAT_PER_SHARE * CONTRACT_SHARES
    sells = 2 * qty
    sec = FEE_SEC_RATE * CONTRACT_SHARES * qty * (max(credit, 0.0) + max(debit, 0.0))
    raw = sides * per_contract + sells * FEE_TAF_SELL + sec
    return math.ceil(raw * 100.0 - 1e-9) / 100.0


def ci_entirely_above_zero(interval: Optional[Interval]) -> bool:
    return interval is not None and interval.low > 0


@dataclass(frozen=True)
class TrainRow:
    """Train-window facts the selector is allowed to see. No test fields."""

    name: str
    searchable: bool
    n: int
    monthly: Optional[Interval]
    expectancy_r: Optional[Interval]


def select_name(rows: Sequence[TrainRow], min_trades: int = MIN_TRADES) -> Optional[str]:
    """Highest train monthly return among rows whose intervals clear zero.

    A row qualifies only when it is searchable, it has at least
    ``min_trades`` spreads, and both the monthly-return interval and the
    expectancy-per-max-risk interval sit entirely above zero. Ties break
    toward the higher expectancy per unit of risk, then the name.
    """
    eligible: list[TrainRow] = []
    for row in rows:
        if not row.searchable or row.n < min_trades:
            continue
        if not ci_entirely_above_zero(row.monthly):
            continue
        if not ci_entirely_above_zero(row.expectancy_r):
            continue
        eligible.append(row)
    if not eligible:
        return None
    eligible.sort(
        key=lambda row: (
            -(row.monthly.point if row.monthly else 0.0),
            -(row.expectancy_r.point if row.expectancy_r else 0.0),
            row.name,
        )
    )
    return eligible[0].name
