"""Synthetic 5m RTH paths: trend, range, W, inverse H&S, gap, early close.

Timestamps are America/New_York bar opens. ``available_at`` is the close
(open + 5 minutes). Prices are made up. Nothing here is a market print.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

from research.intraday_sr.types import ET, TZ

_FULL = 78  # 09:30 through 15:55
_EARLY = 42  # 09:30 through 12:55, session ends 13:00


def session_opens(session: date, *, early_close: bool = False) -> list[datetime]:
    """5m bar-open timestamps for one RTH session."""
    count = _EARLY if early_close else _FULL
    start = datetime.combine(session, time(9, 30), tzinfo=ET)
    return [start + timedelta(minutes=5 * i) for i in range(count)]


def _frame_from_closes(
    session: date,
    closes: np.ndarray,
    *,
    symbol: str,
    first_open: float | None = None,
    adj_factor: float = 1.0,
    volume: float = 10_000.0,
    early_close: bool = False,
    wick: float = 0.05,
) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=np.float64)
    opens_ts = session_opens(session, early_close=early_close)
    if len(closes) != len(opens_ts):
        raise ValueError(f"{symbol} {session}: {len(closes)} closes for {len(opens_ts)} bars")
    bar_open = np.empty(len(closes), dtype=np.float64)
    bar_open[0] = float(closes[0] if first_open is None else first_open)
    bar_open[1:] = closes[:-1]
    high = np.maximum(bar_open, closes) + wick
    low = np.minimum(bar_open, closes) - wick
    # Keep a strict pivot when the close is a local extreme: the wick
    # follows the close so the extreme is not flattened by the open.
    typical = (high + low + closes) / 3.0
    available = [ts + timedelta(minutes=5) for ts in opens_ts]
    return pd.DataFrame(
        {
            "symbol": symbol,
            "tf": "5m",
            "ts": opens_ts,
            "available_at": available,
            "open": bar_open.astype(np.float32),
            "high": high.astype(np.float32),
            "low": low.astype(np.float32),
            "close": closes.astype(np.float32),
            "volume": np.full(len(closes), volume, dtype=np.float32),
            "vwap": typical.astype(np.float32),
            "trades": np.full(len(closes), 100, dtype=np.int32),
            "session": [session] * len(closes),
            "adj_factor": np.full(len(closes), adj_factor, dtype=np.float32),
        }
    )


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["symbol", "ts"]).reset_index(drop=True)


def trend_bars(symbol: str = "TREND") -> pd.DataFrame:
    """Four rising sessions. Drift dominates a small oscillation."""
    frames = []
    for i, day in enumerate(_weekdays(date(2024, 6, 3), 4)):
        t = np.arange(_FULL, dtype=np.float64)
        closes = 100.0 + i * 2.0 + 0.03 * t + 0.4 * np.sin(t / 4.0)
        frames.append(_frame_from_closes(day, closes, symbol=symbol))
    return _concat(frames)


def range_bars(symbol: str = "RANGE") -> pd.DataFrame:
    """Four sessions oscillating around 40, one of them with adj_factor 2."""
    frames = []
    for i, day in enumerate(_weekdays(date(2024, 6, 3), 4)):
        t = np.arange(_FULL, dtype=np.float64)
        closes = 40.0 + 1.5 * np.sin(t / 6.0)
        factor = 2.0 if i == 1 else 1.0
        frames.append(_frame_from_closes(day, closes, symbol=symbol, adj_factor=factor))
    return _concat(frames)


def _w_closes() -> np.ndarray:
    closes = np.empty(_FULL, dtype=np.float64)
    closes[0:11] = np.linspace(110.0, 100.0, 11)
    closes[11:31] = np.linspace(101.0, 110.0, 20)
    closes[31:51] = np.linspace(109.0, 100.2, 20)
    closes[51:] = np.linspace(101.2, 112.0, _FULL - 51)
    return closes


def w_bars(symbol: str = "WSHAPE") -> pd.DataFrame:
    """Four copies of a W: lows near 100 at bars 10 and 50, neck near 110."""
    frames = []
    for day in _weekdays(date(2024, 6, 3), 4):
        frames.append(_frame_from_closes(day, _w_closes(), symbol=symbol, wick=0.02))
    return _concat(frames)


def _ihs_closes() -> np.ndarray:
    """Left shoulder, head, right shoulder, with two intervening peaks."""
    closes = np.empty(_FULL, dtype=np.float64)
    closes[0:13] = np.linspace(108.0, 102.0, 13)  # trough at index 12
    closes[13:23] = np.linspace(103.0, 107.0, 10)  # peak near 22
    closes[23:33] = np.linspace(106.0, 98.0, 10)  # head at index 32
    closes[33:43] = np.linspace(99.0, 107.2, 10)  # peak near 42
    closes[43:53] = np.linspace(106.2, 102.2, 10)  # right shoulder at index 52
    closes[53:] = np.linspace(103.0, 112.0, _FULL - 53)
    return closes


def ihs_bars(symbol: str = "IHSSHAPE") -> pd.DataFrame:
    """Four copies of an inverse head-and-shoulders."""
    frames = []
    for day in _weekdays(date(2024, 6, 3), 4):
        frames.append(_frame_from_closes(day, _ihs_closes(), symbol=symbol, wick=0.02))
    return _concat(frames)


def gap_bars(symbol: str = "GAP") -> pd.DataFrame:
    """Session opens 4 points above the prior close, then trades flat."""
    frames = []
    days = _weekdays(date(2024, 6, 3), 4)
    prior_close = 100.0
    for day in days:
        closes = np.full(_FULL, prior_close + 4.0, dtype=np.float64)
        closes += np.linspace(0.0, 0.5, _FULL)
        frames.append(
            _frame_from_closes(
                day,
                closes,
                symbol=symbol,
                first_open=prior_close + 4.0,
            )
        )
        prior_close = float(closes[-1])
    return _concat(frames)


def early_close_bars(symbol: str = "EARLY") -> pd.DataFrame:
    """Full sessions plus 2024-07-03, which is a 13:00 ET close (42 bars)."""
    frames = []
    full_days = [date(2024, 6, 27), date(2024, 6, 28), date(2024, 7, 1), date(2024, 7, 2)]
    for i, day in enumerate(full_days):
        t = np.arange(_FULL, dtype=np.float64)
        closes = 80.0 + i + 0.01 * t
        frames.append(_frame_from_closes(day, closes, symbol=symbol))
    early = date(2024, 7, 3)
    t = np.arange(_EARLY, dtype=np.float64)
    frames.append(
        _frame_from_closes(
            early,
            85.0 + 0.02 * t,
            symbol=symbol,
            early_close=True,
        )
    )
    return _concat(frames)


FIXTURES = {
    "trend": trend_bars,
    "range": range_bars,
    "W": w_bars,
    "IHS": ihs_bars,
    "gap": gap_bars,
    "early_close": early_close_bars,
}


def all_fixtures() -> dict[str, pd.DataFrame]:
    return {name: builder() for name, builder in FIXTURES.items()}
