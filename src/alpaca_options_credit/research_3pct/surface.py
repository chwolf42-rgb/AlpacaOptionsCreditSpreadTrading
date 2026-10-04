"""Black-Scholes credit/width at a target delta, before any trade list exists.

This is the structural test of the 20% width gate. It does not use the
entry tape and it does not choose a strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from alpaca_options_credit.replay.credit import (
    bs_delta,
    bs_price,
    leg_bid_ask,
    year_fraction,
)
from alpaca_options_credit.rth import ET


@dataclass(frozen=True)
class VerticalQuote:
    spot: float
    iv: float
    dte: int
    width: float
    right: str
    target_delta: float
    short_strike: float
    short_delta: float
    mid: float
    natural: float

    @property
    def mid_frac(self) -> float:
        return self.mid / self.width if self.width else 0.0

    @property
    def natural_frac(self) -> float:
        return self.natural / self.width if self.width else 0.0

    @property
    def clears_20(self) -> bool:
        return self.natural_frac + 1e-12 >= 0.20


def _years(dte: int) -> float:
    """Calendar time from a Wednesday 16:00 to Friday expiration ``dte`` days out.

    The clock only sets the year fraction. The surface is not a backtest.
    """
    start = datetime(2026, 6, 17, 16, 0, tzinfo=ET)
    expiry = (start + timedelta(days=dte)).date()
    # Snap to the function the replay uses: time until that calendar date's close.
    return year_fraction(start, expiry) if dte > 0 else 0.0


def strike_for_abs_delta(
    spot: float,
    t_years: float,
    iv: float,
    right: str,
    target: float,
) -> float:
    """Strike whose absolute Black-Scholes delta is ``target``."""
    lo = spot * 0.2
    hi = spot * 2.5
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        delta = abs(bs_delta(spot, mid, t_years, iv, right))
        # Put delta rises with the strike. Call delta falls with the strike.
        too_high = delta > target if right == "put" else delta < target
        if too_high:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def quote_vertical(
    *,
    spot: float,
    iv: float,
    dte: int,
    width: float,
    target_delta: float,
    right: str = "put",
) -> VerticalQuote | None:
    """Natural credit of a vertical whose short is at ``target_delta``.

    Natural credit is short bid minus long ask, using the same half-spread
    the replay uses (6% of mid, clamped to $0.05–$0.25). A non-positive
    long strike is not a listed vertical.
    """
    if spot <= 0 or iv <= 0 or width <= 0 or dte <= 0 or target_delta <= 0:
        return None
    t_years = _years(dte)
    if t_years <= 0:
        return None
    short_k = strike_for_abs_delta(spot, t_years, iv, right, target_delta)
    long_k = short_k - width if right == "put" else short_k + width
    if long_k <= 0 or short_k <= 0:
        return None
    short_mid = bs_price(spot, short_k, t_years, iv, right)
    long_mid = bs_price(spot, long_k, t_years, iv, right)
    short_bid, _short_ask = leg_bid_ask(short_mid)
    _long_bid, long_ask = leg_bid_ask(long_mid)
    if short_bid <= 0 or long_ask <= 0:
        natural = 0.0
    else:
        natural = short_bid - long_ask
    mid = short_mid - long_mid
    delta = abs(bs_delta(spot, short_k, t_years, iv, right))
    return VerticalQuote(
        spot=spot,
        iv=iv,
        dte=dte,
        width=width,
        right=right,
        target_delta=target_delta,
        short_strike=short_k,
        short_delta=delta,
        mid=mid,
        natural=natural,
    )


def surface_grid() -> list[VerticalQuote]:
    """Precommitted grid. Spots cover a $30 name through an index near $800."""
    spots = (30.0, 50.0, 100.0, 200.0, 500.0, 770.0)
    ivs = (0.12, 0.18, 0.25, 0.40, 0.80)
    dtes = (7, 14, 21, 30, 45)
    deltas = (0.10, 0.16, 0.20, 0.25, 0.30, 0.40)
    widths = (2.5, 5.0, 10.0)
    out: list[VerticalQuote] = []
    for spot in spots:
        for iv in ivs:
            for dte in dtes:
                for delta in deltas:
                    for width in widths:
                        for right in ("put", "call"):
                            row = quote_vertical(
                                spot=spot,
                                iv=iv,
                                dte=dte,
                                width=width,
                                target_delta=delta,
                                right=right,
                            )
                            if row is not None:
                                out.append(row)
    return out


def summarize_surface(rows: list[VerticalQuote]) -> list[dict]:
    """Median natural/width and the share that clear 20%, by delta, DTE, width."""
    buckets: dict[tuple, list[VerticalQuote]] = {}
    for row in rows:
        if row.dte <= 14:
            dte_name = "7-14"
        elif row.dte <= 30:
            dte_name = "21-30"
        else:
            dte_name = "30-45"
        key = (row.target_delta, dte_name, row.width)
        buckets.setdefault(key, []).append(row)
    out = []
    for key in sorted(buckets):
        group = buckets[key]
        fracs = sorted(row.natural_frac for row in group)
        mid = fracs[len(fracs) // 2]
        clears = sum(1 for row in group if row.clears_20)
        out.append(
            {
                "delta": key[0],
                "dte": key[1],
                "width": key[2],
                "n": len(group),
                "median_natural_frac": mid,
                "pct_clear_20": clears / len(group),
            }
        )
    return out
