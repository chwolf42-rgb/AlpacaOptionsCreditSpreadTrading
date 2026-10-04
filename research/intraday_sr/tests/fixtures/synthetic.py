"""Synthetic 5m RTH paths.

Timestamps are America/New_York bar opens. ``available_at`` is the close
(open + 5 minutes). Prices are made up. Nothing here is a market print.

The long fixtures cover at least 30 sessions and the volume is not flat,
so ATR_d and RVOL have something to see. The W and IHS paths put a deeper
wick on each extreme bar so the pivot is strict.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from research.intraday_sr.types import ET

_FULL = 78  # 09:30 through 15:55
_EARLY = 42  # 09:30 through 12:55, session ends 13:00
_SESSIONS = 32


def session_opens(session: date, *, early_close: bool = False) -> list[datetime]:
    """5m bar-open timestamps for one RTH session."""
    count = _EARLY if early_close else _FULL
    start = datetime.combine(session, time(9, 30), tzinfo=ET)
    return [start + timedelta(minutes=5 * i) for i in range(count)]


def _volumes(n: int, day_index: int) -> np.ndarray:
    base = 8_000.0 + 1_500.0 * (day_index % 6)
    out = np.full(n, base, dtype=np.float64)
    out[::13] *= 3.0
    return out


def _frame_from_closes(
    session: date,
    closes: np.ndarray,
    *,
    symbol: str,
    first_open: float | None = None,
    adj_factor: float = 1.0,
    volume: float = 10_000.0,
    volumes: np.ndarray | None = None,
    early_close: bool = False,
    wick: float = 0.05,
    extra_low_at: tuple[int, ...] = (),
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
    # The next bar opens at this close, so a plain wick is shared with the
    # neighbour and is not a strict pivot. Deepen a close-trough and raise a
    # close-peak so the extreme bar is the unique high or low.
    for index in range(1, len(closes) - 1):
        if closes[index] < closes[index - 1] and closes[index] < closes[index + 1]:
            low[index] -= 0.25
        if closes[index] > closes[index - 1] and closes[index] > closes[index + 1]:
            high[index] += 0.25
    for index in extra_low_at:
        low[index] -= 0.5
    if volumes is None:
        volume_col = np.full(len(closes), volume, dtype=np.float64)
    else:
        volume_col = np.asarray(volumes, dtype=np.float64)
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
            "volume": volume_col,
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


def _weekdays_ending(end: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    return days


def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["symbol", "ts"]).reset_index(drop=True)


def trend_bars(symbol: str = "TREND") -> pd.DataFrame:
    """Rising sessions. Drift dominates a small oscillation."""
    frames = []
    for i, day in enumerate(_weekdays(date(2024, 6, 3), _SESSIONS)):
        t = np.arange(_FULL, dtype=np.float64)
        closes = 100.0 + i * 2.0 + 0.03 * t + 0.4 * np.sin(t / 4.0)
        frames.append(_frame_from_closes(day, closes, symbol=symbol, volumes=_volumes(_FULL, i)))
    return _concat(frames)


def range_bars(symbol: str = "RANGE") -> pd.DataFrame:
    """Sessions oscillating around 40. One session uses adj_factor 2."""
    frames = []
    for i, day in enumerate(_weekdays(date(2024, 6, 3), _SESSIONS)):
        t = np.arange(_FULL, dtype=np.float64)
        closes = 40.0 + 1.5 * np.sin(t / 6.0)
        factor = 2.0 if i == 1 else 1.0
        frames.append(
            _frame_from_closes(day, closes, symbol=symbol, adj_factor=factor, volumes=_volumes(_FULL, i))
        )
    return _concat(frames)


def _w_closes() -> np.ndarray:
    closes = np.empty(_FULL, dtype=np.float64)
    closes[0:11] = np.linspace(110.0, 100.0, 11)
    closes[11:31] = np.linspace(101.0, 110.0, 20)
    closes[31:51] = np.linspace(109.0, 100.2, 20)
    closes[51:] = np.linspace(101.2, 112.0, _FULL - 51)
    return closes


def w_bars(symbol: str = "WSHAPE") -> pd.DataFrame:
    """A W each session: lows near bars 10 and 50, neck near bar 30."""
    frames = []
    for i, day in enumerate(_weekdays(date(2024, 6, 3), _SESSIONS)):
        frames.append(
            _frame_from_closes(
                day,
                _w_closes(),
                symbol=symbol,
                wick=0.02,
                extra_low_at=(10, 50),
                volumes=_volumes(_FULL, i),
            )
        )
    return _concat(frames)


def _ihs_closes() -> np.ndarray:
    """Left shoulder, head, right shoulder, with two intervening peaks."""
    closes = np.empty(_FULL, dtype=np.float64)
    closes[0:13] = np.linspace(108.0, 102.0, 13)
    closes[13:23] = np.linspace(103.0, 107.0, 10)
    closes[23:33] = np.linspace(106.0, 98.0, 10)
    closes[33:43] = np.linspace(99.0, 107.2, 10)
    closes[43:53] = np.linspace(106.2, 102.2, 10)
    closes[53:] = np.linspace(103.0, 112.0, _FULL - 53)
    return closes


def ihs_bars(symbol: str = "IHSSHAPE") -> pd.DataFrame:
    """An inverse head-and-shoulders each session."""
    frames = []
    for i, day in enumerate(_weekdays(date(2024, 6, 3), _SESSIONS)):
        frames.append(
            _frame_from_closes(
                day,
                _ihs_closes(),
                symbol=symbol,
                wick=0.02,
                extra_low_at=(12, 32, 52),
                volumes=_volumes(_FULL, i),
            )
        )
    return _concat(frames)


def gap_bars(symbol: str = "GAP") -> pd.DataFrame:
    """Each session opens 4 points above the prior close, then trades flat."""
    frames = []
    days = _weekdays(date(2024, 6, 3), _SESSIONS)
    prior_close = 100.0
    for i, day in enumerate(days):
        closes = np.full(_FULL, prior_close + 4.0, dtype=np.float64)
        closes += np.linspace(0.0, 0.5, _FULL)
        frames.append(
            _frame_from_closes(
                day,
                closes,
                symbol=symbol,
                first_open=prior_close + 4.0,
                volumes=_volumes(_FULL, i),
            )
        )
        prior_close = float(closes[-1])
    return _concat(frames)


def early_close_bars(symbol: str = "EARLY") -> pd.DataFrame:
    """Full sessions through 2024-07-02, then the 13:00 ET close on 2024-07-03."""
    frames = []
    full_days = _weekdays_ending(date(2024, 7, 2), _SESSIONS - 1)
    for i, day in enumerate(full_days):
        t = np.arange(_FULL, dtype=np.float64)
        closes = 80.0 + i + 0.01 * t
        frames.append(_frame_from_closes(day, closes, symbol=symbol, volumes=_volumes(_FULL, i)))
    early = date(2024, 7, 3)
    t = np.arange(_EARLY, dtype=np.float64)
    frames.append(
        _frame_from_closes(
            early,
            85.0 + 0.02 * t,
            symbol=symbol,
            early_close=True,
            volumes=_volumes(_EARLY, len(full_days)),
        )
    )
    return _concat(frames)


def _flat_day(session: date, *, symbol: str, level: float, early_close: bool = False) -> pd.DataFrame:
    count = _EARLY if early_close else _FULL
    t = np.arange(count, dtype=np.float64)
    closes = level + 0.01 * np.sin(t / 5.0)
    return _frame_from_closes(
        session,
        closes,
        symbol=symbol,
        early_close=early_close,
        volumes=_volumes(count, session.toordinal() % 6),
    )


def dst_spring_bars(symbol: str = "DSTSPR") -> pd.DataFrame:
    """Friday 2024-03-08 (EST) and Monday 2024-03-11 (EDT)."""
    return _concat(
        [
            _flat_day(date(2024, 3, 8), symbol=symbol, level=50.0),
            _flat_day(date(2024, 3, 11), symbol=symbol, level=50.4),
        ]
    )


def dst_fall_bars(symbol: str = "DSTFAL") -> pd.DataFrame:
    """Friday 2024-11-01 (EDT) and Monday 2024-11-04 (EST)."""
    return _concat(
        [
            _flat_day(date(2024, 11, 1), symbol=symbol, level=60.0),
            _flat_day(date(2024, 11, 4), symbol=symbol, level=60.4),
        ]
    )


def thanksgiving_early_bars(symbol: str = "TGIVE") -> pd.DataFrame:
    """2024-11-29, the 13:00 ET close, which falls in EST."""
    return _flat_day(date(2024, 11, 29), symbol=symbol, level=70.0, early_close=True)


FIXTURES = {
    "trend": trend_bars,
    "range": range_bars,
    "W": w_bars,
    "IHS": ihs_bars,
    "gap": gap_bars,
    "early_close": early_close_bars,
    "dst_spring": dst_spring_bars,
    "dst_fall": dst_fall_bars,
    "thanksgiving": thanksgiving_early_bars,
}


def all_fixtures() -> dict[str, pd.DataFrame]:
    return {name: builder() for name, builder in FIXTURES.items()}
