"""Entry filters borrowed from the equity and crypto sleeves.

Each predicate is true when the filter would *allow* the spread. The replay
keeps the arm when a filter says no, and tries again on a later bar. Nothing
here changes take-profit, the 1.5× credit stop, or the structure-break close.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Optional, Sequence

from alpaca_options_credit.models import Bar, Side
from alpaca_options_credit.rth import ET, as_et
from alpaca_options_credit.strategy.volume_profile import hvn_shelves

# Decision time falls inside one of these windows → skip the entry.
# First 30 minutes after the 09:30 open, and the midday lull.
OPEN_SKIP = (time(9, 30), time(10, 0))
MIDDAY_LULL = (time(11, 30), time(13, 30))

RS_LOOKBACK = 10
EMA_PERIOD = 50
VOLUME_LOOKBACK = 20


def bar_overlaps_skip_window(start: datetime, end: datetime) -> bool:
    """True when the confirmation bar overlaps the open or the midday lull.

    Judged in America/New_York. A 60-minute bar that *starts* at 09:30 overlaps
    the first half hour even though it closes at 10:30, so the skip is not
    inert on the hourly tape. Windows are half-open: a bar that merely touches
    10:00 or 13:30 is allowed.
    """
    start_et = as_et(start)
    end_et = as_et(end)
    if end_et <= start_et:
        return False
    day = start_et.date()
    for begin, finish in (OPEN_SKIP, MIDDAY_LULL):
        block_start = datetime.combine(day, begin, tzinfo=ET)
        block_end = datetime.combine(day, finish, tzinfo=ET)
        if start_et < block_end and end_et > block_start:
            return True
    return False


def session_allows(start: datetime, end: datetime) -> bool:
    return not bar_overlaps_skip_window(start, end)


def relative_strength_allows(
    side: Side,
    symbol_return: Optional[float],
    spy_return: Optional[float],
) -> bool:
    """Bull puts need the name to have beaten SPY; bear calls need it to have lagged.

    Both returns are the last ``RS_LOOKBACK`` completed sessions. A tie or a
    missing print fails closed.
    """
    if symbol_return is None or spy_return is None:
        return False
    if side is Side.BULLISH:
        return symbol_return > spy_return
    if side is Side.BEARISH:
        return symbol_return < spy_return
    return False


def trailing_return(closes: Sequence[float], lookback: int = RS_LOOKBACK) -> Optional[float]:
    if lookback < 1 or len(closes) < lookback + 1:
        return None
    start = closes[-(lookback + 1)]
    end = closes[-1]
    if start <= 0:
        return None
    return end / start - 1.0


def two_hvn_allows(
    bars: Sequence[Bar],
    short_strike: float,
    atr: float,
    *,
    bin_size: float = 0.5,
    percentile: float = 0.70,
) -> bool:
    """At least two distinct HVN shelves sit on the short strike.

    A shelf counts when its price range comes within ``max(ATR, $1)`` of the
    short. One shelf is the baseline zone; this filter wants a second node
    in the same area.
    """
    if short_strike <= 0 or len(bars) < 5:
        return False
    shelves = hvn_shelves(list(bars), bin_size=bin_size, percentile=percentile)
    pad = max(float(atr or 0.0), 1.0)
    near = 0
    for low, high in shelves:
        if high >= short_strike - pad and low <= short_strike + pad:
            near += 1
    return near >= 2


def ema_last(values: Sequence[float], period: int = EMA_PERIOD) -> Optional[float]:
    """EMA seeded with the first ``period`` observations. None until warm."""
    if period < 1 or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    acc = sum(values[:period]) / period
    for value in values[period:]:
        acc = value * k + acc * (1.0 - k)
    return acc


def trend_allows(side: Side, close: Optional[float], ema: Optional[float]) -> bool:
    """Bull put: last close above the EMA. Bear call: last close below it."""
    if close is None or ema is None:
        return False
    if side is Side.BULLISH:
        return close > ema
    if side is Side.BEARISH:
        return close < ema
    return False


def volume_allows(bar_volume: float, prior_average: Optional[float]) -> bool:
    """Confirmation bar volume strictly above the prior average."""
    if prior_average is None or prior_average <= 0:
        return False
    return bar_volume > prior_average


def prior_average(volumes: Sequence[float], lookback: int = VOLUME_LOOKBACK) -> Optional[float]:
    if len(volumes) < lookback:
        return None
    window = volumes[-lookback:]
    return sum(window) / float(lookback)
