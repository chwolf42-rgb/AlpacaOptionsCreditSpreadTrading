"""Black-Scholes-Merton prices, variance-swap term structure, and fills.

Fills are worse than the mid by 25% of the full bid-ask width on every
leg. The width depends on the underlying, absolute delta, and calendar
DTE. That model is fixed in this file; it is not a fitted parameter.
"""

from __future__ import annotations

import math

INDEX_ETFS = frozenset({"SPY", "QQQ", "IWM"})


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(
    spot: float,
    strike: float,
    t_years: float,
    sigma: float,
    right: str,
    rate: float,
    div: float,
) -> float:
    """European call or put. Expired contracts settle at intrinsic."""
    if spot <= 0 or strike <= 0:
        return 0.0
    intrinsic = _intrinsic(spot, strike, right)
    if t_years <= 1e-8 or sigma <= 1e-8:
        return intrinsic
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate - div + 0.5 * sigma * sigma) * t_years) / (
        sigma * sqrt_t
    )
    d2 = d1 - sigma * sqrt_t
    forward = spot * math.exp(-div * t_years)
    discount_k = strike * math.exp(-rate * t_years)
    if right == "call":
        return forward * norm_cdf(d1) - discount_k * norm_cdf(d2)
    return discount_k * norm_cdf(-d2) - forward * norm_cdf(-d1)


def bs_delta(
    spot: float,
    strike: float,
    t_years: float,
    sigma: float,
    right: str,
    rate: float,
    div: float,
) -> float:
    if spot <= 0 or strike <= 0:
        return 0.0
    if t_years <= 1e-8 or sigma <= 1e-8:
        intrinsic = _intrinsic(spot, strike, right)
        if intrinsic <= 0:
            return 0.0
        return 1.0 if right == "call" else -1.0
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate - div + 0.5 * sigma * sigma) * t_years) / (
        sigma * sqrt_t
    )
    discount = math.exp(-div * t_years)
    if right == "call":
        return discount * norm_cdf(d1)
    return discount * (norm_cdf(d1) - 1.0)


def _intrinsic(spot: float, strike: float, right: str) -> float:
    if right == "call":
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


def interp_variance_vol(knots: list[tuple[float, float]], dte: float) -> float:
    """Annualized vol from total-variance interpolation.

    ``knots`` are (calendar DTE, annualized vol). Inside the knot span,
    sigma^2 * T is linear in T. Outside the span the nearest knot's vol
    is used (no invented steepening).
    """
    pts = [(float(d), float(iv)) for d, iv in knots if d > 0 and iv > 0]
    pts.sort()
    if not pts:
        return 0.0
    dte = max(1.0, float(dte))
    if dte <= pts[0][0]:
        return pts[0][1]
    if dte >= pts[-1][0]:
        return pts[-1][1]
    for (d1, iv1), (d2, iv2) in zip(pts, pts[1:]):
        if d1 <= dte <= d2:
            w1 = iv1 * iv1 * (d1 / 365.0)
            w2 = iv2 * iv2 * (d2 / 365.0)
            frac = (dte - d1) / (d2 - d1)
            total = w1 + frac * (w2 - w1)
            return math.sqrt(max(total, 1e-12) / (dte / 365.0))
    return pts[-1][1]


def skew_iv(atm: float, spot: float, strike: float, t_years: float) -> float:
    """Sticky-delta equity skew. Slope is fixed at -0.10.

    Standardized moneyness m = log(K/S) / (atm * sqrt(T)).
    iv = atm * (1 - 0.10 * m), clamped. OTM puts (m < 0) are richer
    than ATM; OTM calls are cheaper. The slope is not fitted.
    """
    if atm <= 0:
        return 0.15
    if t_years <= 1e-8 or spot <= 0 or strike <= 0:
        return min(1.80, max(0.08, atm))
    m = math.log(strike / spot) / (atm * math.sqrt(t_years))
    factor = 1.0 - 0.10 * m
    factor = min(1.55, max(0.75, factor))
    return min(1.80, max(0.08, atm * factor))


def full_width(symbol: str, mid: float, delta: float, dte: int) -> float:
    """Full bid-ask width in dollars, before the 25% adverse fill.

    Index ETFs are penny-pilot tight. Single names are 2.5 times that,
    capped at $1. A very cheap mid cannot be quoted wider than the mid
    itself, so the bid stays non-negative after a 25% adverse move.
    """
    ad = abs(delta)
    if dte <= 2:
        base = 0.20
    elif dte <= 7:
        base = 0.06 if ad >= 0.30 else 0.12
    elif ad >= 0.30 and dte > 21:
        base = 0.04
    elif ad >= 0.15:
        base = 0.08
    else:
        base = 0.12
    if symbol not in INDEX_ETFS:
        base = min(1.00, base * 2.5)
    if mid > 0:
        base = min(base, max(0.02, mid))
    return max(0.02, base)


def buy_fill(mid: float, width: float) -> float | None:
    """Pay mid + 25% of the full width. None if there is no offer."""
    if mid <= 0 or width < 0:
        return None
    return mid + 0.25 * width


def sell_fill(mid: float, width: float) -> float | None:
    """Receive mid - 25% of the full width. None if the bid is under a penny."""
    if mid <= 0 or width < 0:
        return None
    px = mid - 0.25 * width
    if px < 0.01:
        return None
    return px


def strike_step(symbol: str, spot: float) -> float:
    if symbol in INDEX_ETFS:
        return 1.0
    if spot >= 200:
        return 5.0
    if spot >= 50:
        return 2.5
    return 1.0


def snap_strike(price: float, step: float) -> float:
    if step <= 0:
        return price
    return round(price / step) * step


def realized_vol(closes: list[float], end: int, lookback: int) -> float | None:
    """Annualized close-to-close sample vol using closes[end-lookback, end].

    ``end`` is included. Returns None until the window is full. This does
    not read any close after ``end``.
    """
    if lookback < 2 or end < lookback or end >= len(closes):
        return None
    start = end - lookback
    rets: list[float] = []
    for i in range(start + 1, end + 1):
        prev = closes[i - 1]
        cur = closes[i]
        if prev <= 0 or cur <= 0:
            return None
        rets.append(math.log(cur / prev))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return 0.0
    return math.sqrt(var * 252.0)


def rolling_mean(values: list[float], end: int, lookback: int) -> float | None:
    if lookback < 1 or end < lookback - 1 or end >= len(values):
        return None
    window = values[end - lookback + 1 : end + 1]
    return sum(window) / lookback
