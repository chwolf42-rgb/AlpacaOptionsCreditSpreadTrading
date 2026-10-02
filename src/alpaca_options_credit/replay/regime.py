"""IV-proxy rank, extension-into-shelf, and range checks for the redesign.

The replay has no listed implied-vol history. The IV proxy is the same
Black-Scholes volatility the pricer uses: 20-session close-to-close realized
vol times 1.15, clamped. "IV above realized" compares that proxy with
60-session realized vol. A test of proxy versus its own 20-day realized vol
would always pass, so it is not a filter.

IV percentile is the share of the prior 252 proxy readings strictly below
the current one. IV rank is (current − low) / (high − low) over that window.
VIX percentile is the same idea on the VIX close.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Sequence

from alpaca_options_credit.models import Bar, Side
from alpaca_options_credit.replay.credit import implied_vol, realized_vol
from alpaca_options_credit.replay.stats import ReplayTrade
from alpaca_options_credit.rth import as_et
from alpaca_options_credit.strategy.structure import atr
from alpaca_options_credit.strategy.volume_profile import hvn_shelves

IV_WINDOW = 252


@dataclass(frozen=True)
class RegimeSnap:
    iv_pct: Optional[float]
    iv_rank: Optional[float]
    rv20: Optional[float]
    rv60: Optional[float]
    iv: Optional[float]
    vix: Optional[float]
    vix_pct: Optional[float]
    spy_rv20: Optional[float]
    ema50: Optional[float]
    atr: Optional[float]
    close: float


@dataclass(frozen=True)
class ShelfTouch:
    side: Side
    shelf_low: float
    shelf_high: float
    anchor: float


def ema_series(values: Sequence[float], period: int = 50) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if period < 1 or len(values) < period:
        return out
    k = 2.0 / (period + 1.0)
    acc = sum(values[:period]) / period
    out[period - 1] = acc
    for i in range(period, len(values)):
        acc = values[i] * k + acc * (1.0 - k)
        out[i] = acc
    return out


def _rv_series(closes: Sequence[float], lookback: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(closes)
    for i in range(lookback, len(closes)):
        out[i] = realized_vol(closes[: i + 1], lookback)
    return out


def percentile_and_rank(
    values: Sequence[Optional[float]],
    window: int = IV_WINDOW,
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    """Trailing percentile and rank. The current point is not inside the window."""
    pct: list[Optional[float]] = [None] * len(values)
    rank: list[Optional[float]] = [None] * len(values)
    for i, cur in enumerate(values):
        if cur is None or i < window:
            continue
        hist = [v for v in values[i - window : i] if v is not None]
        if len(hist) < int(window * 0.9):
            continue
        pct[i] = sum(1 for v in hist if v < cur) / len(hist)
        lo = min(hist)
        hi = max(hist)
        rank[i] = 0.5 if hi <= lo else (cur - lo) / (hi - lo)
    return pct, rank


def build_symbol_regime(
    daily: Sequence[Bar],
    vix_by_date: dict[date, float],
    vix_pct_by_date: dict[date, Optional[float]],
    spy_rv_by_date: dict[date, Optional[float]],
) -> dict[date, RegimeSnap]:
    closes = [bar.close for bar in daily]
    rv20 = _rv_series(closes, 20)
    rv60 = _rv_series(closes, 60)
    ivs: list[Optional[float]] = [None if rv is None else implied_vol(rv) for rv in rv20]
    pct, rank = percentile_and_rank(ivs, IV_WINDOW)
    emas = ema_series(closes, 50)
    out: dict[date, RegimeSnap] = {}
    for i, bar in enumerate(daily):
        day = as_et(bar.ts).date()
        vol = atr(list(daily[: i + 1]), 14) if i >= 14 else None
        out[day] = RegimeSnap(
            iv_pct=pct[i],
            iv_rank=rank[i],
            rv20=rv20[i],
            rv60=rv60[i],
            iv=ivs[i],
            vix=vix_by_date.get(day),
            vix_pct=vix_pct_by_date.get(day),
            spy_rv20=spy_rv_by_date.get(day),
            ema50=emas[i],
            atr=vol,
            close=bar.close,
        )
    return out


def build_regime(
    daily_by_symbol: dict[str, Sequence[Bar]],
    vix_bars: Sequence[Bar],
    spy_symbol: str = "SPY",
) -> dict[str, dict[date, RegimeSnap]]:
    vix_by_date = {as_et(bar.ts).date(): bar.close for bar in vix_bars}
    vix_values: list[Optional[float]] = []
    vix_days: list[date] = []
    for bar in vix_bars:
        vix_days.append(as_et(bar.ts).date())
        vix_values.append(bar.close)
    vix_pct, _vix_rank = percentile_and_rank(vix_values, IV_WINDOW)
    vix_pct_by_date = {day: vix_pct[i] for i, day in enumerate(vix_days)}

    spy = list(daily_by_symbol.get(spy_symbol) or [])
    spy_closes = [bar.close for bar in spy]
    spy_rv = _rv_series(spy_closes, 20)
    spy_rv_by_date = {as_et(spy[i].ts).date(): spy_rv[i] for i in range(len(spy))}

    return {
        symbol: build_symbol_regime(bars, vix_by_date, vix_pct_by_date, spy_rv_by_date)
        for symbol, bars in daily_by_symbol.items()
    }


def extension_touch(
    *,
    prior_close: float,
    close_5: float,
    atr_value: float,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    shelves: Sequence[tuple[float, float]],
) -> Optional[ShelfTouch]:
    """Sell into a shelf after a one-ATR push. A close through the shelf is not a touch.

    Bear call: the prior five sessions rose at least one ATR, the prior close
    was still under the shelf, this bar traded up into it, and the close held
    at or below the shelf high.

    Bull put: the mirror image, and the close held at or above the shelf low.
    """
    if atr_value <= 0 or prior_close <= 0 or close_5 <= 0 or not shelves:
        return None
    move = prior_close - close_5
    best: Optional[ShelfTouch] = None
    best_dist: Optional[float] = None
    for low, high in shelves:
        if (
            move >= atr_value
            and prior_close < low
            and bar_high >= low
            and bar_close <= high
            and bar_close > prior_close
        ):
            touch = ShelfTouch(Side.BEARISH, low, high, high)
        elif (
            -move >= atr_value
            and prior_close > high
            and bar_low <= high
            and bar_close >= low
            and bar_close < prior_close
        ):
            touch = ShelfTouch(Side.BULLISH, low, high, low)
        else:
            continue
        mid = (low + high) / 2.0
        dist = abs(mid - bar_close)
        if best_dist is None or dist < best_dist:
            best = touch
            best_dist = dist
    return best


def range_anchors(
    prior: Sequence[Bar],
    close: float,
    ema: Optional[float],
    atr_value: Optional[float],
    *,
    lookback: int = 20,
) -> Optional[tuple[float, float]]:
    """(range low, range high) when price is mid-range and within one ATR of the EMA.

    ``prior`` is completed sessions before the decision bar, so today's range
    expansion is not part of the box the shorts have to sit beyond.
    """
    if ema is None or atr_value is None or atr_value <= 0 or len(prior) < lookback:
        return None
    window = list(prior[-lookback:])
    hi = max(bar.high for bar in window)
    lo = min(bar.low for bar in window)
    if hi <= lo:
        return None
    if abs(close - ema) > atr_value:
        return None
    pos = (close - lo) / (hi - lo)
    if pos < 0.25 or pos > 0.75:
        return None
    return lo, hi


def max_drawdown(trades: Sequence[ReplayTrade], equity: float = 100_000.0) -> tuple[float, float]:
    """Dollars and fraction of peak equity. Trades are ordered by exit time."""
    ordered = sorted(trades, key=lambda t: (t.exit_time, t.entry_time, t.symbol))
    wealth = equity
    peak = equity
    worst = 0.0
    for trade in ordered:
        wealth += trade.pnl
        if wealth > peak:
            peak = wealth
        drop = peak - wealth
        if drop > worst:
            worst = drop
    frac = (worst / peak) if peak > 0 else 0.0
    return worst, frac


def spy_buy_hold(
    daily: Sequence[Bar],
    start: date,
    end: date,
    equity: float = 100_000.0,
) -> Optional[dict[str, float]]:
    """Close-to-close SPY over the dates, plus the path's peak-to-trough fraction."""
    bars = [bar for bar in daily if start <= as_et(bar.ts).date() <= end]
    if len(bars) < 2 or bars[0].close <= 0:
        return None
    ret = bars[-1].close / bars[0].close - 1.0
    peak = bars[0].close
    worst = 0.0
    for bar in bars:
        if bar.close > peak:
            peak = bar.close
        if peak > 0:
            worst = max(worst, (peak - bar.close) / peak)
    return {
        "return": ret,
        "pnl": equity * ret,
        "max_dd_frac": worst,
        "start_close": bars[0].close,
        "end_close": bars[-1].close,
    }
