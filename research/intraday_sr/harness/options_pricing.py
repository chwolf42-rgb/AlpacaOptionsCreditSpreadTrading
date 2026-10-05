"""Black-Scholes quotes for the options overlay. MODEL-BASED, no OPRA, no network.

Vol, skew, time, rate and the spread follow SPEC §7 and the implementations already
in ``options.py`` (one pricer, so the existing parity tests and these agree).

Items §7 names without pinning down a formula are parameters on ``quote_package`` /
``OverlayParams`` and are listed as ``OPEN_TODOS`` in ``options_overlay``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional, Sequence

from research.intraday_sr.harness import options as O

# Same intrabar clock the equity-linked baseline in options.py uses.
HALF_BAR = O.HALF_BAR

# Reasons whose fill timestamp is already the bar open (no extra half-bar).
OPEN_FILL_REASONS = ("forced_eod", "daily_stop", "forced_eod_lastclose", "time_exit")


def bs_price(spot: float, strike: float, t_years: float, sigma: float, right: str, rate: float = 0.04) -> float:
    """European price, q = 0. ``options.bs_price`` is the implementation."""
    return O.bs_price(spot, strike, t_years, sigma, right, rate)


def year_fraction(now: datetime, expiry: date, cal: O.TradingCalendar, early_closes: set = frozenset(),
                  cfg: O.OptionsCfg = O.OptionsCfg()) -> float:
    """Trading minutes from ``now`` to the expiry close / (252 × 390), floored at 15 minutes (§7)."""
    return O.t_years(now, expiry, cal, early_closes, cfg)


def half_spread(symbol: str, mid: float, otm: bool, zero_dte_late: bool, cfg: O.OptionsCfg = O.OptionsCfg(),
                *, late_mult_after_clamp: bool = True) -> float:
    """Half-spread per leg (§7).

    TODO_LATE_SPREAD_CLAMP_ORDER: the ETF 0DTE-after-14:00 ×1.5 is applied *after* the
    ``[lo, hi]`` clamp, matching ``options.half_spread``, so the half-spread can exceed
    ``hi``. ``late_mult_after_clamp=False`` clamps the already-scaled width instead.
    Single names do not get the ×1.5 (the spec puts it on the ETF row only).
    """
    if symbol not in O.ETFS:
        return min(max(cfg.single_c * mid, cfg.single_lo), cfg.single_hi)
    c = cfg.etf_c_otm if otm else cfg.etf_c_atm
    raw = c * mid
    if zero_dte_late and not late_mult_after_clamp:
        raw *= cfg.etf_0dte_late_mult
    width = min(max(raw, cfg.etf_lo), cfg.etf_hi)
    if zero_dte_late and late_mult_after_clamp:
        width *= cfg.etf_0dte_late_mult
    return width


def zero_dte_late(expiry: date, session: date, when: datetime, *, only_on_expiry_session: bool = True) -> bool:
    """TODO_LATE_SPREAD_SCOPE: §7 says "×1.5 for 0DTE after 14:00".

    Default: the clock is at or after 14:00 ET and the contract expires on ``session``.
    A weekly held the same afternoon is not 0DTE, so it does not get the multiplier.
    """
    if when.time() < time(14, 0):
        return False
    if only_on_expiry_session and expiry != session:
        return False
    return True


@dataclass(frozen=True)
class LegQuote:
    mid: float
    half: float
    bid: float
    ask: float


def quote_leg(symbol: str, spot: float, strike: float, right: str, t_years: float, sigma_atm: float, otm: bool,
              zero_dte_late_flag: bool, cfg: O.OptionsCfg = O.OptionsCfg(), *, iv_mult: float = 1.0,
              late_mult_after_clamp: bool = True) -> LegQuote:
    """One leg. Bid = max(mid − half, 0), ask = mid + half. Skew uses §7 ``sigma_k``."""
    sigma = O.sigma_k(symbol, sigma_atm * iv_mult, spot, strike, t_years, cfg)
    mid = bs_price(spot, strike, t_years, sigma, right, cfg.rate)
    width = half_spread(symbol, mid, otm, zero_dte_late_flag, cfg, late_mult_after_clamp=late_mult_after_clamp)
    return LegQuote(mid=mid, half=width, bid=max(mid - width, 0.0), ask=mid + width)


def package_price(legs: Sequence[O.Leg], symbol: str, spot: float, t_years: float, sigma_atm: float, buy: bool,
                  zero_dte_late_flag: bool, cfg: O.OptionsCfg = O.OptionsCfg(), *, iv_mult: float = 1.0,
                  late_mult_after_clamp: bool = True) -> float:
    """Executable package price per share.

    Opening (``buy=True``): pay the ask on long legs, receive the bid on short legs.
    Closing (``buy=False``): sell longs at the bid, buy shorts back at the ask, floored at 0.
    """
    total = 0.0
    for leg in legs:
        quote = quote_leg(symbol, spot, leg.strike, leg.right, t_years, sigma_atm, leg.otm, zero_dte_late_flag,
                          cfg, iv_mult=iv_mult, late_mult_after_clamp=late_mult_after_clamp)
        if buy:
            total += quote.ask if leg.qty > 0 else -quote.bid
        else:
            total += quote.bid if leg.qty > 0 else -quote.ask
    return total if buy else max(total, 0.0)


def per_contract_cash(per_share: float, n_legs: int, fee_per_contract: float, *, opening: bool) -> float:
    """Dollars for one package. Opening pays the fee; closing receives the price net of the fee.

    §7: $0.05 per contract per side. A two-leg vertical is two contracts.
    """
    if opening:
        return per_share * 100.0 + fee_per_contract * n_legs
    return per_share * 100.0 - fee_per_contract * n_legs


def structure_legs(structure: str, symbol: str, direction: int, spot: float, target: float, *,
                   min_width_increments: int = 1) -> list[O.Leg]:
    """Baseline structures (§5 / O1.8). ``direction`` > 0 is a call, < 0 is a put.

    TODO_VERTICAL_SAME_STRIKE: the short strike is the equity target rounded to the
    strike increment, then pushed so it is at least ``min_width_increments`` away from
    the long strike. The spec says "nearest the equity target"; when that rounding
    lands on the long strike the vertical would be zero-width, so the default width is
    one increment (the same rule as ``options.legs_for``).
    """
    if min_width_increments < 1:
        raise ValueError(f"min_width_increments must be >= 1, got {min_width_increments}")
    right = "call" if direction > 0 else "put"
    increment = O.strike_increment(symbol, spot)
    atm = O.atm_strike(symbol, spot)
    if structure == "long_atm":
        return [O.Leg(atm, right, 1, False)]
    if structure == "long_otm_1":
        return [O.Leg(atm + direction * increment, right, 1, True)]
    if structure != "debit_vertical":
        raise ValueError(f"unknown structure {structure!r}")
    rounded = round(target / increment) * increment
    min_short = atm + direction * increment * min_width_increments
    short = max(rounded, min_short) if direction > 0 else min(rounded, min_short)
    return [O.Leg(atm, right, 1, False), O.Leg(short, right, -1, True)]


@dataclass(frozen=True)
class BarMark:
    """Bids of the package we are long, at three prints of one 5m bar. Prices are per share."""

    ts: datetime
    bid_open: float
    bid_adverse: float
    bid_favorable: float
    is_time_exit: bool


@dataclass(frozen=True)
class OptionExit:
    ts: datetime
    bid: float
    reason: str          # stop | take_profit | time_exit


def resolve_option_exit(path: Sequence[BarMark], entry_ask: float, stop_pct: float, take_profit_pct: float, *,
                        gap_uses_open: bool = True, stop_basis: str = "entry_ask",
                        time_exit_at_bar_open: bool = True) -> OptionExit:
    """Walk a 5m path. Stop and take-profit are fractions of the entry ask (``stop_pct`` is negative).

    A2: if a bar's open or its adverse/favorable prints make both the stop and the target
    reachable, the stop wins. A gap through the stop (open bid already at or below it)
    fills at that open bid; an intrabar touch fills at the stop level. The same split
    applies to the target.

    TODO_TIME_EXIT_BAR_RANGE: a bar flagged ``is_time_exit`` flattens at its open bid
    and that bar's high/low are not held. The caller sets the flag on the first bar
    whose open is at or after 15:45 ET (12:45 on a 13:00 session).

    TODO_STOP_BASIS: only ``entry_ask`` is implemented. The $0.05 fee is not inside the
    percentage; cash accounting adds it around this per-share price.

    TODO_INTRABAR_OPEN: ``gap_uses_open=False`` ignores the open bid and uses only the
    adverse and favorable prints (the high/low the spec names). The close is not a
    separate exit trigger — the position carries to the next bar or the time exit.
    """
    if stop_basis != "entry_ask":
        raise NotImplementedError(
            f"TODO_STOP_BASIS: stop/TP fractions are of the entry ask per share, not {stop_basis!r}")
    if not time_exit_at_bar_open:
        raise NotImplementedError(
            "TODO_TIME_EXIT_BAR_RANGE: the time exit is the 15:45 bar's open; holding that bar is not implemented")
    if not path:
        raise ValueError("option exit path is empty")
    stop_px = entry_ask * (1.0 + stop_pct)
    target_px = entry_ask * (1.0 + take_profit_pct)
    for mark in path:
        if mark.is_time_exit:
            return OptionExit(mark.ts, mark.bid_open, "time_exit")
        open_stop = gap_uses_open and mark.bid_open <= stop_px
        open_target = gap_uses_open and mark.bid_open >= target_px
        hit_stop = open_stop or mark.bid_adverse <= stop_px
        hit_target = open_target or mark.bid_favorable >= target_px
        if hit_stop and hit_target:
            if open_stop:
                return OptionExit(mark.ts, mark.bid_open, "stop")
            return OptionExit(mark.ts + HALF_BAR, stop_px, "stop")
        if hit_stop:
            if open_stop:
                return OptionExit(mark.ts, mark.bid_open, "stop")
            return OptionExit(mark.ts + HALF_BAR, stop_px, "stop")
        if hit_target:
            if open_target:
                return OptionExit(mark.ts, mark.bid_open, "take_profit")
            return OptionExit(mark.ts + HALF_BAR, target_px, "take_profit")
    last = path[-1]
    return OptionExit(last.ts, last.bid_open, "time_exit")


def fill_clock(ts: datetime, reason: str) -> datetime:
    """Bar open for forced/time exits; mid-bar for an intrabar stop or target."""
    if reason in OPEN_FILL_REASONS:
        return ts
    return ts + HALF_BAR


def atm_vol(symbol: str, session: date, expiry: date, vix9d_prev: float, vix_prev: float,
            rv20_symbol: float, rv20_spy: float, cfg: O.OptionsCfg = O.OptionsCfg()) -> float:
    """§7 ATM vol for this contract's calendar tenor. Same-day uses the VIX9D flat branch (T < 9).

    TODO_RV20_DEFINITION: QQQ, IWM and single names scale by RV20(symbol) / RV20(SPY)
    from completed daily bars. §7 does not define the estimator (close-to-close versus
    range, the 20-session window's alignment, or annualization). The overlay does not
    compute RV20; the caller passes the two numbers and a missing input comes back as NaN,
    which the book records as ``no_iv`` rather than as a trade.
    """
    calendar_days = max((expiry - session).days, 0)
    return O.sigma_atm(symbol, vix9d_prev, vix_prev, float(calendar_days), rv20_symbol, rv20_spy, cfg)
