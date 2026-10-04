"""Cached bar loading, Alpaca start-time conversion, and closed resampling.

The algorithmic bodies land after this interface cut. Signatures and
the conversion rules are the contract; see LEVELS-INTERFACE.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

from research.intraday_sr.levels.types import BarTimeConvention

BAR_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
OPTIONAL_BAR_COLUMNS: tuple[str, ...] = ("vwap", "trade_count")

# Session tags stored on the ``session`` column after a cache load.
SESSION_PREMARKET = "premarket"
SESSION_RTH = "rth"
SESSION_AFTERHOURS = "afterhours"
SESSION_SETTLEMENT = "settlement"


def load_cached_bars(
    root: str | Path,
    symbol: str,
    timeframe: str,
    *,
    bar_time: BarTimeConvention = BarTimeConvention.START,
    tz: str = "America/New_York",
) -> pd.DataFrame:
    """Load one symbol/timeframe from a cache root.

    The directory layout is not fixed yet. The loader tries, in order:

    - ``{root}/{timeframe}/{symbol}.parquet``
    - ``{root}/{timeframe}/{symbol}.csv``
    - ``{root}/{symbol}/{timeframe}.parquet``
    - ``{root}/{symbol}/{timeframe}.csv``
    - ``{root}/{symbol}_{timeframe}.parquet``
    - ``{root}/{symbol}_{timeframe}.csv``

    Returns a float32 frame indexed by tz-aware bar **close** time in
    ``tz``, with columns ``open, high, low, close, volume`` plus ``vwap``
    and ``trade_count`` when the file has them, and a ``session`` column.
    The closing-auction print is not a row in this frame; it is attached
    as ``df.attrs["settlement"]`` (see the interface doc).
    """
    raise NotImplementedError


def load_bars_file(
    path: str | Path,
    timeframe: str,
    *,
    bar_time: BarTimeConvention = BarTimeConvention.START,
    tz: str = "America/New_York",
    symbol: str | None = None,
) -> pd.DataFrame:
    """Load one parquet or csv file and normalize it the same way as the cache loader."""
    raise NotImplementedError


def to_bar_close_index(
    frame: pd.DataFrame,
    timeframe: str,
    *,
    bar_time: BarTimeConvention = BarTimeConvention.START,
    tz: str = "America/New_York",
    timestamp_column: str = "ts",
    treat_1600_as_settlement: bool = True,
) -> pd.DataFrame:
    """Convert a raw Alpaca-style frame to a close-indexed research frame.

    When ``bar_time`` is ``START`` and the timestamps are naive, they are
    interpreted as ``tz`` wall time (not UTC), localized, then shifted
    forward by the bar width. A bar whose start is exactly 16:00 ET is the
    closing auction when ``treat_1600_as_settlement`` is set: it is removed
    from the frame and stored on ``attrs["settlement"]`` with ``known_at``
    16:00 ET, not as a bar that closes at 16:05.
    """
    raise NotImplementedError


def cached_bar_path(root: str | Path, symbol: str, timeframe: str) -> Path:
    """First existing cache path for ``symbol`` and ``timeframe``."""
    raise NotImplementedError


def resample_closed(rth_bars: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Session-aligned resample of RTH bars.

    ``timeframe`` is ``15m`` or ``1h`` (also accepts ``5m``, which returns
    the RTH rows unchanged). A bucket is emitted only when the source bar
    that closes on the bucket end is present. No partial bucket is emitted
    before that close. 1h bars are anchored at 09:30 ET and end at
    10:30, 11:30, 12:30, 13:30, 14:30, and 15:30. The 15:30-16:00 remainder
    is not a 1h bar.
    """
    raise NotImplementedError


def rth_mask(index: pd.DatetimeIndex) -> pd.Series:
    """True for bar closes in (09:30, 16:00] America/New_York."""
    raise NotImplementedError


def settlement_prints(bars: pd.DataFrame) -> pd.DataFrame:
    """Auction prints stored on ``bars.attrs['settlement']`` (possibly empty)."""
    raise NotImplementedError


def derive_daily_from_intraday(
    bars: pd.DataFrame,
    settlement: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """One RTH daily bar per completed session.

    Index and the session's knowable time are 16:00 ET. A session that
    does not contain the bar closing at 16:00 is omitted. The official
    close is the settlement print when that print exists, otherwise the
    last RTH close. High and low include the settlement print. After-hours
    and premarket bars are not included.
    """
    raise NotImplementedError


def iter_symbol_frames(
    frames: dict[str, pd.DataFrame],
) -> Iterable[tuple[str, pd.DataFrame]]:
    """Yield ``(symbol, frame)`` one symbol at a time, in symbol order."""
    raise NotImplementedError
