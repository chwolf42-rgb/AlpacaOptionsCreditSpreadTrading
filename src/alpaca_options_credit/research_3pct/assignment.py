"""Early-assignment check on a closed daily bar.

American short options can be assigned before expiration. The replay prices
European Black-Scholes, so this is a separate pass: if the short is in the
money and its European extrinsic value is under five cents, treat that
session's close as an assignment and settle the vertical at intrinsic.
Dividend-driven assignment of a call that still has extrinsic is not
detected; there is no dividend calendar in this study.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional, Sequence

from alpaca_options_credit.models import Bar
from alpaca_options_credit.replay.credit import bs_price, intrinsic_spread, year_fraction
from alpaca_options_credit.rth import ET, as_et

EXTRINSIC_FLOOR = 0.05


def _intrinsic_leg(spot: float, strike: float, right: str) -> float:
    if right == "call":
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


def assignment_debit(
    *,
    spot: float,
    short_k: float,
    long_k: float,
    t_years: float,
    iv: float,
    right: str,
) -> Optional[float]:
    """Intrinsic spread debit if the short would be assigned, else None."""
    if spot <= 0 or short_k <= 0 or t_years <= 0 or iv <= 0:
        return None
    intrinsic = _intrinsic_leg(spot, short_k, right)
    if intrinsic <= 0:
        return None
    euro = bs_price(spot, short_k, t_years, iv, right)
    extrinsic = euro - intrinsic
    if extrinsic >= EXTRINSIC_FLOOR:
        return None
    return intrinsic_spread(spot, short_k, long_k, right)


def first_assignment(
    *,
    entry: datetime,
    exit_at: datetime,
    expiration: date,
    short_k: float,
    long_k: float,
    iv: float,
    right: str,
    daily: Sequence[Bar],
) -> Optional[tuple[datetime, float]]:
    """First daily close, strictly after entry and before the modeled exit, that assigns."""
    entry_day = as_et(entry).date()
    exit_day = as_et(exit_at).date()
    for bar in daily:
        day = as_et(bar.ts).date()
        if day <= entry_day or day >= exit_day:
            continue
        when = datetime.combine(day, time(16, 0), tzinfo=ET)
        t_years = year_fraction(when, expiration)
        debit = assignment_debit(
            spot=bar.close,
            short_k=short_k,
            long_k=long_k,
            t_years=t_years,
            iv=iv,
            right=right,
        )
        if debit is not None:
            return when, debit
    return None
