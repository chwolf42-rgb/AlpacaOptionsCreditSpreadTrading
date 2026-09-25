"""Black-Scholes vertical credit, listed-strike grid, and exit fills.

The live sleeve sells a credit spread off the underlying's invalidation.
Historical option quotes are not in this repo, so the replay prices both
legs with Black-Scholes and then applies the same gates the engine uses:

- short strike clears ``min_short_inv_gap`` on the modeled strike grid
- natural credit is short bid − long ask (not the mid)
- credit must be at least ``min_credit_pct_of_width`` of the width
- take-profit and stop are judged on the spread mid, matching ``spread_mark``
- the take-profit fill is exactly half the credit (a poll that catches the
  cross, not the far side of an hourly wick)
- a stop that gaps through the open fills at the open's natural debit;
  a stop that trades through fills at 1.5× credit plus the bid/ask at that
  spot, and never better than the bar's worst natural debit
- structure-break and the final mark pay the natural debit (cross the spread)
- expiration settles at intrinsic

Implied vol is trailing close-to-close realized vol times a variance-risk
premium, frozen at entry so later marks do not peek at future volatility.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional, Sequence

from alpaca_options_credit.models import Bar, ContractQuote, Side, SpreadProposal
from alpaca_options_credit.rth import ET, as_et
from alpaca_options_credit.strategy.spreads import build_proposal
from alpaca_options_credit.strategy.spreads import stop_hit, take_profit_hit

# Equity discount. Dividend yield is left at zero: on a $5 vertical the
# dividend is second order next to the bid/ask and the 20% credit floor.
RATE = 0.04
DIVIDEND = 0.0

# 20-session close-to-close vol, marked up for the variance risk premium
# sellers of equity premium are paid, then clamped.
RV_LOOKBACK = 20
IV_PREMIUM = 1.15
IV_FLOOR = 0.15
IV_CAP = 1.25

# Each leg's half-spread. Liquid names in this universe are often a nickel
# wide; cheap wings are wider as a percent of mid. Capped so a model quote
# cannot invent a dollar-wide market on a $5 vertical.
HALF_SPREAD_PCT = 0.06
HALF_SPREAD_MIN = 0.05
HALF_SPREAD_MAX = 0.25

# ETFs in the full-A list that keep $1 strikes well above $200.
ETF_ONE_POINT = frozenset(
    {
        "DIA",
        "EEM",
        "GLD",
        "HYG",
        "IWM",
        "KRE",
        "QQQ",
        "SLV",
        "SMH",
        "SPY",
        "TLT",
        "XBI",
        "XLE",
        "XLF",
        "XLK",
        "XLU",
        "XOP",
    }
)

SESSION_CLOSE = time(16, 0)


@dataclass(frozen=True)
class ReplayLimits:
    width: float = 5.0
    dte_min: int = 30
    dte_max: int = 45
    dte_target: int = 37
    min_credit_pct: float = 0.20
    max_credit_pct: float = 1.0
    min_short_inv_gap: float = 1.0
    tp_frac: float = 0.50
    stop_mult: float = 1.5
    multiplier: int = 100
    equity: float = 100_000.0
    risk_pct: float = 0.005


@dataclass(frozen=True)
class PricedSpread:
    proposal: SpreadProposal
    iv: float
    expiration: date
    right: str
    spot: float


@dataclass(frozen=True)
class ExitFill:
    reason: str
    debit: float
    when: datetime


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(
    spot: float,
    strike: float,
    t_years: float,
    sigma: float,
    right: str,
    *,
    rate: float = RATE,
    div: float = DIVIDEND,
) -> float:
    """European call or put. Expired contracts settle at intrinsic."""
    if spot <= 0 or strike <= 0:
        return 0.0
    intrinsic = _intrinsic_leg(spot, strike, right)
    if t_years <= 1e-8 or sigma <= 1e-8:
        return intrinsic
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate - div + 0.5 * sigma * sigma) * t_years) / (
        sigma * sqrt_t
    )
    d2 = d1 - sigma * sqrt_t
    discount_k = strike * math.exp(-rate * t_years)
    forward_s = spot * math.exp(-div * t_years)
    if right == "call":
        return forward_s * norm_cdf(d1) - discount_k * norm_cdf(d2)
    return discount_k * norm_cdf(-d2) - forward_s * norm_cdf(-d1)


def _intrinsic_leg(spot: float, strike: float, right: str) -> float:
    if right == "call":
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


def realized_vol(closes: Sequence[float], lookback: int = RV_LOOKBACK) -> Optional[float]:
    """Annualized close-to-close sample vol. None until the window is full."""
    if lookback < 2 or len(closes) < lookback + 1:
        return None
    window = list(closes[-(lookback + 1) :])
    rets: list[float] = []
    for i in range(1, len(window)):
        prev, cur = window[i - 1], window[i]
        if prev <= 0 or cur <= 0:
            return None
        rets.append(math.log(cur / prev))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return 0.0
    return math.sqrt(var) * math.sqrt(252.0)


def implied_vol(rv: float) -> float:
    return min(IV_CAP, max(IV_FLOOR, rv * IV_PREMIUM))


def half_spread(mid: float) -> float:
    if mid <= 0:
        return HALF_SPREAD_MIN
    return min(HALF_SPREAD_MAX, max(HALF_SPREAD_MIN, HALF_SPREAD_PCT * mid))


def leg_bid_ask(mid: float) -> tuple[float, float]:
    """Two-sided quote. A mid under a penny has no bid (live ``quote_missing``)."""
    if mid <= 0:
        return 0.0, 0.0
    half = half_spread(mid)
    bid = mid - half
    ask = mid + half
    if bid < 0.01:
        bid = 0.0
    return bid, ask


def year_fraction(when: datetime, expiration: date) -> float:
    """Calendar time from ``when`` to the expiration-session close."""
    expiry = datetime.combine(expiration, SESSION_CLOSE, tzinfo=ET)
    secs = (expiry - as_et(when)).total_seconds()
    if secs <= 0:
        return 0.0
    return secs / (365.25 * 24.0 * 3600.0)


def strike_increment(symbol: str, spot: float) -> float:
    """Listed-strike step. ETFs stay on $1; stocks widen with price."""
    if symbol.upper() in ETF_ONE_POINT:
        return 1.0
    if spot < 50:
        return 0.5
    if spot < 200:
        return 1.0
    if spot < 500:
        return 2.5
    return 5.0


def friday_expiration(
    today: date,
    *,
    dte_min: int = 30,
    dte_max: int = 45,
    target: int = 37,
) -> Optional[date]:
    """Friday inside the DTE window closest to the target (live mid-window)."""
    best: Optional[date] = None
    best_key: Optional[tuple[int, int]] = None
    for dte in range(dte_min, dte_max + 1):
        expiry = today + timedelta(days=dte)
        if expiry.weekday() != 4:
            continue
        key = (abs(dte - target), dte)
        if best_key is None or key < best_key:
            best_key = key
            best = expiry
    return best


def strike_grid(lo: float, hi: float, step: float) -> list[float]:
    if step <= 0 or hi < lo:
        return []
    start = math.ceil((lo - 1e-9) / step) * step
    out: list[float] = []
    k = start
    while k <= hi + 1e-9:
        out.append(round(k, 2))
        k += step
    return out


def _leg_mids(
    spot: float,
    short_k: float,
    long_k: float,
    t_years: float,
    sigma: float,
    right: str,
) -> tuple[float, float]:
    return (
        bs_price(spot, short_k, t_years, sigma, right),
        bs_price(spot, long_k, t_years, sigma, right),
    )


def spread_mid(
    spot: float,
    short_k: float,
    long_k: float,
    t_years: float,
    sigma: float,
    right: str,
) -> float:
    short_mid, long_mid = _leg_mids(spot, short_k, long_k, t_years, sigma, right)
    return short_mid - long_mid


def natural_debit(
    spot: float,
    short_k: float,
    long_k: float,
    t_years: float,
    sigma: float,
    right: str,
) -> float:
    """Debit to close: buy the short at the ask, sell the long at the bid."""
    if t_years <= 1e-8:
        return intrinsic_spread(spot, short_k, long_k, right)
    short_mid, long_mid = _leg_mids(spot, short_k, long_k, t_years, sigma, right)
    _short_bid, short_ask = leg_bid_ask(short_mid)
    long_bid, _long_ask = leg_bid_ask(long_mid)
    if short_ask <= 0:
        short_ask = max(short_mid, 0.0)
    return max(0.0, short_ask - max(long_bid, 0.0))


def intrinsic_spread(spot: float, short_k: float, long_k: float, right: str) -> float:
    return _intrinsic_leg(spot, short_k, right) - _intrinsic_leg(spot, long_k, right)


def modeled_proposal(
    *,
    symbol: str,
    side: Side,
    invalidation: float,
    spot: float,
    when: datetime,
    iv: float,
    limits: ReplayLimits,
) -> Optional[PricedSpread]:
    """Chain of model quotes around invalidation, then the live credit gate."""
    if spot <= 0 or iv <= 0 or invalidation <= 0:
        return None
    today = as_et(when).date()
    expiration = friday_expiration(
        today,
        dte_min=limits.dte_min,
        dte_max=limits.dte_max,
        target=limits.dte_target,
    )
    if expiration is None:
        return None
    t_years = year_fraction(when, expiration)
    if t_years <= 0:
        return None
    step = strike_increment(symbol, spot)
    right = "put" if side is Side.BULLISH else "call"
    lo = invalidation - limits.width * 3
    hi = invalidation + limits.width * 3
    quotes: list[ContractQuote] = []
    for strike in strike_grid(max(step, lo), hi, step):
        mid = bs_price(spot, strike, t_years, iv, right)
        bid, ask = leg_bid_ask(mid)
        quotes.append(
            ContractQuote(
                occ=f"{symbol}{expiration.strftime('%y%m%d')}{right[0].upper()}{int(round(strike * 1000)):08d}",
                strike=strike,
                expiration=expiration,
                right=right,
                bid=bid,
                ask=ask,
            )
        )
    proposal = build_proposal(
        underlying=symbol,
        side=side,
        invalidation=invalidation,
        chain=quotes,
        width=limits.width,
        min_credit_pct=limits.min_credit_pct,
        today=today,
        dte_min=limits.dte_min,
        dte_max=limits.dte_max,
        max_credit_pct_of_width=limits.max_credit_pct,
        min_short_inv_gap=limits.min_short_inv_gap,
    )
    return PricedSpread(proposal=proposal, iv=iv, expiration=expiration, right=right, spot=spot)


def simulate_exit(
    *,
    side: Side,
    short_k: float,
    long_k: float,
    credit: float,
    iv: float,
    expiration: date,
    invalidation: float,
    bars: Sequence[Bar],
    start_index: int,
    bar_end_fn,
    daily_close_at,
    limits: ReplayLimits,
) -> ExitFill:
    """Walk bars after the fill. Stop is tested before take-profit on each bar.

    ``bars[start_index]`` is the signal bar (already closed). The position is
    marked from the next bar forward. If the tape ends first, the last close
    is marked to market at the natural debit (``open_mtm``).
    """
    right = "put" if side is Side.BULLISH else "call"
    last_when: Optional[datetime] = None
    last_spot: Optional[float] = None
    for i in range(start_index + 1, len(bars)):
        bar = bars[i]
        when = bar_end_fn(bar)
        last_when = when
        last_spot = bar.close
        t_years = year_fraction(when, expiration)
        fill = _exit_on_bar(
            bar=bar,
            when=when,
            side=side,
            short_k=short_k,
            long_k=long_k,
            credit=credit,
            iv=iv,
            t_years=t_years,
            right=right,
            invalidation=invalidation,
            daily_close=daily_close_at(when),
            limits=limits,
        )
        if fill is not None:
            return fill
        if t_years <= 0:
            return ExitFill(
                "expiration",
                intrinsic_spread(bar.close, short_k, long_k, right),
                when,
            )

    if last_when is None or last_spot is None:
        # No bar after the signal. Mark the signal close itself.
        anchor = bars[start_index]
        when = bar_end_fn(anchor)
        t_years = year_fraction(when, expiration)
        debit = (
            intrinsic_spread(anchor.close, short_k, long_k, right)
            if t_years <= 0
            else natural_debit(anchor.close, short_k, long_k, t_years, iv, right)
        )
        return ExitFill("open_mtm", debit, when)

    t_end = year_fraction(last_when, expiration)
    debit = (
        intrinsic_spread(last_spot, short_k, long_k, right)
        if t_end <= 0
        else natural_debit(last_spot, short_k, long_k, t_end, iv, right)
    )
    return ExitFill("open_mtm", debit, last_when)


def _exit_on_bar(
    *,
    bar: Bar,
    when: datetime,
    side: Side,
    short_k: float,
    long_k: float,
    credit: float,
    iv: float,
    t_years: float,
    right: str,
    invalidation: float,
    daily_close: Optional[float],
    limits: ReplayLimits,
) -> Optional[ExitFill]:
    adverse = bar.low if side is Side.BULLISH else bar.high
    favorable = bar.high if side is Side.BULLISH else bar.low

    def mid_at(spot: float) -> float:
        return spread_mid(spot, short_k, long_k, t_years, iv, right)

    def debit_at(spot: float) -> float:
        return natural_debit(spot, short_k, long_k, t_years, iv, right)

    # Gap through the stop on the open: fill at the open, not at 1.5×.
    if stop_hit(credit, mid_at(bar.open), limits.stop_mult):
        return ExitFill("stop_credit", debit_at(bar.open), when)

    if stop_hit(credit, mid_at(adverse), limits.stop_mult):
        mid_adv = mid_at(adverse)
        nat_adv = debit_at(adverse)
        slip = max(0.0, nat_adv - mid_adv)
        # Poll-level stop plus the spread, capped by the bar's worst debit.
        filled = min(nat_adv, limits.stop_mult * credit + slip)
        return ExitFill("stop_credit", filled, when)

    if take_profit_hit(credit, mid_at(favorable), limits.tp_frac):
        return ExitFill("take_profit", limits.tp_frac * credit, when)

    if daily_close is not None and _structure_broken(side, invalidation, daily_close):
        return ExitFill("structure_break", debit_at(daily_close), when)
    return None


def _structure_broken(side: Side, invalidation: float, close: float) -> bool:
    if side is Side.BULLISH:
        return close < invalidation
    if side is Side.BEARISH:
        return close > invalidation
    return False
