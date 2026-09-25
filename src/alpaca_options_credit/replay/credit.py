"""Black-Scholes vertical credit, listed-strike grid, and exit fills.

The live sleeve sells a credit spread off the underlying's invalidation.
Historical option quotes are not in this repo, so the replay prices both
legs with Black-Scholes and then applies the same gates the engine uses:

- short strike clears ``min_short_inv_gap`` on the modeled strike grid
- natural credit is short bid − long ask (not the mid)
- credit must be at least ``min_credit_pct_of_width`` of the width
- take-profit and stop are judged on the spread mid, matching ``spread_mark``
- the take-profit fill is a debit of ``(1 - tp_frac)`` × credit (a poll
  that catches the cross, not the far side of an hourly wick). At 50%
  that debit is half the credit
- ``stop_check="intrabar"`` (the live-style default): a stop that gaps
  through the open fills at the open's natural debit; a stop that trades
  through fills at ``stop_mult`` × credit plus the bid/ask at that spot,
  and never better than the bar's worst natural debit
- ``stop_check="close"`` fires only when the bar's close mid is through
  the stop. ``stop_check="none"`` leaves the price stop off and keeps
  take-profit, structure break, and expiration
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
    # intrabar: open and the adverse extreme. close: bar close only.
    # none: structure break, take-profit, and expiration, with no price stop.
    stop_check: str = "intrabar"
    # When set, the short is the listed strike beyond the gap whose |delta|
    # is closest to this value. None keeps the nearest-strike rule.
    target_abs_delta: Optional[float] = None
    # If set, reject the delta short when the closest listed strike beyond
    # the anchor is closer to the money than target + this tolerance.
    # A strike further out than the target is kept: the shelf won, and the
    # short is still protected. None keeps the closest strike either way.
    delta_tol: Optional[float] = None
    # Close when calendar days to expiration are at or under this. None
    # holds to the other exits. 21 is the managed-premium rule.
    close_dte: Optional[int] = None
    # invalidation: daily close through the structure level.
    # short: daily close through the short strike.
    # shelf: daily close through the shelf passed to simulate_exit.
    # none: no underlying-close stop.
    spot_stop: str = "invalidation"
    # natural: short bid − long ask in, short ask − long bid out.
    # mid: both sides at the model mid (no bid/ask).
    # nickel: natural, then another $0.05 worse on the credit and on
    # any debit that is not the formula take-profit fill. The take-profit
    # fill also pays that extra nickel.
    fill_mode: str = "natural"
    # When set, the width is this fraction of spot, snapped to the listed
    # strike step, at least one step. None uses ``width``.
    width_pct: Optional[float] = None
    # 0 disables. Skip a new open when earnings fall within this many
    # calendar days of the entry or the expiration.
    earnings_blackout_days: int = 0
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
    short_delta: float = 0.0


@dataclass(frozen=True)
class ExitFill:
    reason: str
    debit: float
    when: datetime
    # Mids and natural debits on the bar that produced a credit stop.
    # Zero on every other exit. Used to separate a wick from a close.
    open_mid: float = 0.0
    adverse_mid: float = 0.0
    close_mid: float = 0.0
    open_natural: float = 0.0
    adverse_natural: float = 0.0
    close_natural: float = 0.0


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


def bs_delta(
    spot: float,
    strike: float,
    t_years: float,
    sigma: float,
    right: str,
    *,
    rate: float = RATE,
    div: float = DIVIDEND,
) -> float:
    """Black-Scholes delta. A put is negative. Expired options are ±1 or 0."""
    if spot <= 0 or strike <= 0:
        return 0.0
    if t_years <= 1e-8 or sigma <= 1e-8:
        intrinsic = _intrinsic_leg(spot, strike, right)
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


def resolved_width(symbol: str, spot: float, limits: ReplayLimits) -> float:
    """Dollar width. ``width_pct`` snaps to the listed strike step."""
    pct = limits.width_pct
    if pct is None or pct <= 0:
        return float(limits.width)
    step = strike_increment(symbol, spot)
    if step <= 0 or spot <= 0:
        return float(limits.width)
    steps = max(1, int(round((spot * float(pct)) / step)))
    return round(steps * step, 2)


def calendar_dte(when: datetime, expiration: date) -> int:
    return (expiration - as_et(when).date()).days


def earnings_near(dates: Optional[Sequence[date]], day: date, days: int) -> bool:
    """True when any event falls within ``days`` calendar days of ``day``."""
    if days <= 0 or not dates:
        return False
    window = {day + timedelta(days=offset) for offset in range(-days, days + 1)}
    return any(item in window for item in dates)


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
    width = resolved_width(symbol, spot, limits)
    right = "put" if side is Side.BULLISH else "call"
    if limits.target_abs_delta is not None:
        chosen = _strike_for_delta(
            symbol=symbol,
            side=side,
            invalidation=invalidation,
            spot=spot,
            t_years=t_years,
            iv=iv,
            right=right,
            limits=limits,
        )
        strikes = []
        if chosen is not None:
            long_k = chosen - width if right == "put" else chosen + width
            strikes = [chosen, round(long_k, 2)]
    else:
        lo = invalidation - width * 3
        hi = invalidation + width * 3
        strikes = strike_grid(max(step, lo), hi, step)
    quotes: list[ContractQuote] = []
    for strike in strikes:
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
        width=width,
        min_credit_pct=limits.min_credit_pct,
        today=today,
        dte_min=limits.dte_min,
        dte_max=limits.dte_max,
        max_credit_pct_of_width=limits.max_credit_pct,
        min_short_inv_gap=limits.min_short_inv_gap,
    )
    delta = 0.0
    if not proposal.skip:
        delta = bs_delta(spot, proposal.short.strike, t_years, iv, right)
    return PricedSpread(
        proposal=proposal,
        iv=iv,
        expiration=expiration,
        right=right,
        spot=spot,
        short_delta=delta,
    )


def _strike_for_delta(
    *,
    symbol: str,
    side: Side,
    invalidation: float,
    spot: float,
    t_years: float,
    iv: float,
    right: str,
    limits: ReplayLimits,
) -> Optional[float]:
    """Listed strike past the gap whose absolute delta is closest to the target."""
    target = limits.target_abs_delta
    if target is None:
        return None
    step = strike_increment(symbol, spot)
    span = max(limits.width * 12.0, spot * 0.20)
    gap = float(limits.min_short_inv_gap or 0.0)
    if side is Side.BULLISH:
        lo = max(step, min(invalidation, spot) - span)
        hi = invalidation - gap
    else:
        lo = invalidation + gap
        hi = max(invalidation, spot) + span
    best: Optional[float] = None
    best_key: Optional[tuple[float, float]] = None
    for strike in strike_grid(lo, hi, step):
        if side is Side.BULLISH and strike > invalidation - gap + 1e-9:
            continue
        if side is Side.BEARISH and strike < invalidation + gap - 1e-9:
            continue
        delta = abs(bs_delta(spot, strike, t_years, iv, right))
        key = (abs(delta - target), abs(strike - invalidation))
        if best_key is None or key < best_key:
            best_key = key
            best = strike
    if best is None:
        return None
    if limits.delta_tol is not None:
        got = abs(bs_delta(spot, best, t_years, iv, right))
        # Too close to the money. Further out than the target is allowed.
        if got > float(target) + float(limits.delta_tol) + 1e-9:
            return None
    return best


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
    shelf: Optional[float] = None,
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
            expiration=expiration,
            shelf=shelf,
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
            else _terminal_debit(anchor.close, short_k, long_k, t_years, iv, right, limits)
        )
        return ExitFill("open_mtm", debit, when)

    t_end = year_fraction(last_when, expiration)
    debit = (
        intrinsic_spread(last_spot, short_k, long_k, right)
        if t_end <= 0
        else _terminal_debit(last_spot, short_k, long_k, t_end, iv, right, limits)
    )
    return ExitFill("open_mtm", debit, last_when)


def _terminal_debit(spot, short_k, long_k, t_years, iv, right, limits: ReplayLimits) -> float:
    natural = natural_debit(spot, short_k, long_k, t_years, iv, right)
    if limits.fill_mode == "mid":
        return max(0.0, spread_mid(spot, short_k, long_k, t_years, iv, right))
    if limits.fill_mode == "nickel":
        return natural + 0.05
    return natural


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
    expiration: date,
    shelf: Optional[float],
) -> Optional[ExitFill]:
    adverse = bar.low if side is Side.BULLISH else bar.high
    favorable = bar.high if side is Side.BULLISH else bar.low

    def mid_at(spot: float) -> float:
        return spread_mid(spot, short_k, long_k, t_years, iv, right)

    def debit_at(spot: float) -> float:
        return natural_debit(spot, short_k, long_k, t_years, iv, right)

    check = limits.stop_check
    if check not in {"intrabar", "close", "none"}:
        raise ValueError(f"unknown stop_check {check!r}")
    if limits.fill_mode not in {"natural", "mid", "nickel"}:
        raise ValueError(f"unknown fill_mode {limits.fill_mode!r}")
    if limits.spot_stop not in {"invalidation", "short", "shelf", "none"}:
        raise ValueError(f"unknown spot_stop {limits.spot_stop!r}")

    def _priced(natural_fill: float, mid_fill: float) -> float:
        """Exit debit under the fill assumption. Natural is unchanged."""
        if limits.fill_mode == "mid":
            return max(0.0, mid_fill)
        if limits.fill_mode == "nickel":
            return natural_fill + 0.05
        return natural_fill

    open_mid = mid_at(bar.open)
    adverse_mid = mid_at(adverse)
    close_mid_ = mid_at(bar.close)
    open_nat = debit_at(bar.open)
    adverse_nat = debit_at(adverse)
    close_nat = debit_at(bar.close)

    def _stop(debit: float) -> ExitFill:
        return ExitFill(
            "stop_credit",
            debit,
            when,
            open_mid=open_mid,
            adverse_mid=adverse_mid,
            close_mid=close_mid_,
            open_natural=open_nat,
            adverse_natural=adverse_nat,
            close_natural=close_nat,
        )

    if check == "intrabar":
        # Gap through the stop on the open: fill at the open, not at the multiple.
        if stop_hit(credit, open_mid, limits.stop_mult):
            return _stop(_priced(open_nat, open_mid))
        if stop_hit(credit, adverse_mid, limits.stop_mult):
            slip = max(0.0, adverse_nat - adverse_mid)
            # Poll-level stop plus the spread, capped by the bar's worst debit.
            filled = min(adverse_nat, limits.stop_mult * credit + slip)
            return _stop(_priced(filled, min(adverse_mid, limits.stop_mult * credit)))
    elif check == "close" and stop_hit(credit, close_mid_, limits.stop_mult):
        slip = max(0.0, close_nat - close_mid_)
        filled = min(close_nat, limits.stop_mult * credit + slip)
        return _stop(_priced(filled, min(close_mid_, limits.stop_mult * credit)))

    level, level_reason = _spot_stop_level(
        limits.spot_stop, short_k, invalidation, shelf
    )
    # A close through the short or the shelf is a stop, so it beats take-profit
    # on the same bar. The legacy invalidation break stays after take-profit,
    # which is the PR #10 order.
    if (
        level_reason == "underlying_stop"
        and level is not None
        and daily_close is not None
        and _structure_broken(side, level, daily_close)
    ):
        return ExitFill(
            level_reason,
            _priced(debit_at(daily_close), mid_at(daily_close)),
            when,
        )

    if take_profit_hit(credit, mid_at(favorable), limits.tp_frac):
        # Capturing tp_frac of the credit leaves a debit of the rest.
        # At the default 50% the debit is half the credit.
        tp_debit = (1.0 - limits.tp_frac) * credit
        if limits.fill_mode == "nickel":
            tp_debit += 0.05
        return ExitFill("take_profit", tp_debit, when)

    if (
        level_reason == "structure_break"
        and level is not None
        and daily_close is not None
        and _structure_broken(side, level, daily_close)
    ):
        return ExitFill(
            level_reason,
            _priced(debit_at(daily_close), mid_at(daily_close)),
            when,
        )

    if limits.close_dte is not None and calendar_dte(when, expiration) <= int(limits.close_dte):
        return ExitFill(
            "dte_exit",
            _priced(close_nat, close_mid_),
            when,
        )
    return None


def _spot_stop_level(
    mode: str,
    short_k: float,
    invalidation: float,
    shelf: Optional[float],
) -> tuple[Optional[float], str]:
    if mode == "none":
        return None, ""
    if mode == "short":
        return short_k, "underlying_stop"
    if mode == "shelf":
        return (shelf if shelf is not None else invalidation), "underlying_stop"
    return invalidation, "structure_break"


def _structure_broken(side: Side, invalidation: float, close: float) -> bool:
    if side is Side.BULLISH:
        return close < invalidation
    if side is Side.BEARISH:
        return close > invalidation
    return False


def filled_debit(natural_fill: float, mid_fill: float, limits: ReplayLimits) -> float:
    """Debit paid to close under ``limits.fill_mode``."""
    if limits.fill_mode == "mid":
        return max(0.0, mid_fill)
    if limits.fill_mode == "nickel":
        return natural_fill + 0.05
    return natural_fill


def simulate_condor_exit(
    *,
    put_short: float,
    put_long: float,
    call_short: float,
    call_long: float,
    credit: float,
    iv: float,
    expiration: date,
    put_level: float,
    call_level: float,
    bars: Sequence[Bar],
    start_index: int,
    bar_end_fn,
    daily_close_at,
    limits: ReplayLimits,
) -> ExitFill:
    """Iron condor. The mark is the sum of the two verticals.

    The sum is highest at the wings, so a stop looks at the worse extreme
    and a take-profit looks at the best spot inside the bar, including the
    midpoint of the two shorts when that price printed.
    """
    last_when: Optional[datetime] = None
    last_spot: Optional[float] = None
    for i in range(start_index + 1, len(bars)):
        bar = bars[i]
        when = bar_end_fn(bar)
        last_when = when
        last_spot = bar.close
        t_years = year_fraction(when, expiration)
        fill = _condor_on_bar(
            bar=bar,
            when=when,
            put_short=put_short,
            put_long=put_long,
            call_short=call_short,
            call_long=call_long,
            credit=credit,
            iv=iv,
            t_years=t_years,
            expiration=expiration,
            put_level=put_level,
            call_level=call_level,
            daily_close=daily_close_at(when),
            limits=limits,
        )
        if fill is not None:
            return fill
        if t_years <= 0:
            return ExitFill(
                "expiration",
                _condor_intrinsic(bar.close, put_short, put_long, call_short, call_long),
                when,
            )
    if last_when is None or last_spot is None:
        anchor = bars[start_index]
        when = bar_end_fn(anchor)
        t_years = year_fraction(when, expiration)
        debit = (
            _condor_intrinsic(anchor.close, put_short, put_long, call_short, call_long)
            if t_years <= 0
            else _condor_terminal(
                anchor.close, put_short, put_long, call_short, call_long, t_years, iv, limits
            )
        )
        return ExitFill("open_mtm", debit, when)
    t_end = year_fraction(last_when, expiration)
    debit = (
        _condor_intrinsic(last_spot, put_short, put_long, call_short, call_long)
        if t_end <= 0
        else _condor_terminal(
            last_spot, put_short, put_long, call_short, call_long, t_end, iv, limits
        )
    )
    return ExitFill("open_mtm", debit, last_when)


def _condor_intrinsic(spot, put_short, put_long, call_short, call_long) -> float:
    return intrinsic_spread(spot, put_short, put_long, "put") + intrinsic_spread(
        spot, call_short, call_long, "call"
    )


def _condor_marks(spot, put_short, put_long, call_short, call_long, t_years, iv):
    put_mid = spread_mid(spot, put_short, put_long, t_years, iv, "put")
    call_mid = spread_mid(spot, call_short, call_long, t_years, iv, "call")
    put_nat = natural_debit(spot, put_short, put_long, t_years, iv, "put")
    call_nat = natural_debit(spot, call_short, call_long, t_years, iv, "call")
    return put_mid + call_mid, put_nat + call_nat


def _condor_terminal(spot, put_short, put_long, call_short, call_long, t_years, iv, limits) -> float:
    mid, natural = _condor_marks(spot, put_short, put_long, call_short, call_long, t_years, iv)
    return filled_debit(natural, mid, limits)


def _condor_on_bar(
    *,
    bar: Bar,
    when: datetime,
    put_short: float,
    put_long: float,
    call_short: float,
    call_long: float,
    credit: float,
    iv: float,
    t_years: float,
    expiration: date,
    put_level: float,
    call_level: float,
    daily_close: Optional[float],
    limits: ReplayLimits,
) -> Optional[ExitFill]:
    center = (put_short + call_short) / 2.0
    spots = [bar.open, bar.high, bar.low, bar.close]
    if bar.low <= center <= bar.high:
        spots.append(center)

    def marks(spot: float) -> tuple[float, float]:
        return _condor_marks(spot, put_short, put_long, call_short, call_long, t_years, iv)

    priced = [marks(spot) for spot in spots]
    open_mid, open_nat = marks(bar.open)
    worst_mid, worst_nat = max(priced, key=lambda item: item[0])
    best_mid, _best_nat = min(priced, key=lambda item: item[0])
    close_mid, close_nat = marks(bar.close)

    check = limits.stop_check
    if check == "intrabar":
        if stop_hit(credit, open_mid, limits.stop_mult):
            return ExitFill("stop_credit", filled_debit(open_nat, open_mid, limits), when)
        if stop_hit(credit, worst_mid, limits.stop_mult):
            slip = max(0.0, worst_nat - worst_mid)
            filled = min(worst_nat, limits.stop_mult * credit + slip)
            return ExitFill(
                "stop_credit",
                filled_debit(filled, min(worst_mid, limits.stop_mult * credit), limits),
                when,
            )
    elif check == "close" and stop_hit(credit, close_mid, limits.stop_mult):
        slip = max(0.0, close_nat - close_mid)
        filled = min(close_nat, limits.stop_mult * credit + slip)
        return ExitFill(
            "stop_credit",
            filled_debit(filled, min(close_mid, limits.stop_mult * credit), limits),
            when,
        )

    if (
        limits.spot_stop != "none"
        and daily_close is not None
        and (daily_close < put_level or daily_close > call_level)
    ):
        reason = "structure_break" if limits.spot_stop == "invalidation" else "underlying_stop"
        spot_mid, spot_nat = marks(daily_close)
        return ExitFill(reason, filled_debit(spot_nat, spot_mid, limits), when)

    if take_profit_hit(credit, best_mid, limits.tp_frac):
        tp_debit = (1.0 - limits.tp_frac) * credit
        if limits.fill_mode == "nickel":
            tp_debit += 0.05
        return ExitFill("take_profit", tp_debit, when)

    if limits.close_dte is not None and calendar_dte(when, expiration) <= int(limits.close_dte):
        return ExitFill("dte_exit", filled_debit(close_nat, close_mid, limits), when)
    return None
