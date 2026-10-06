"""Shared causal tape helpers for levels, zones, and signals."""

from __future__ import annotations

from datetime import date, datetime, time

import numpy as np
import pandas as pd

from research.intraday_sr.data.calendar import session_close
from research.intraday_sr.types import ET, as_et


def session_day(value) -> date:
    """Session date from a date, a timestamp, or an int32 YYYYMMDD."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, np.integer)):
        number = int(value)
        return date(number // 10000, (number // 100) % 100, number % 100)
    return pd.Timestamp(value).date()


def atr_through_yesterday(frame: pd.DataFrame, as_of: datetime, length: int) -> float | None:
    """Wilder ATR on completed sessions strictly before the session of ``as_of``."""
    today = as_of.astimezone(ET).date()
    daily: list[tuple[float, float, float]] = []
    for session, group in frame.groupby("session", sort=True):
        day = session_day(session)
        if day >= today:
            continue
        ordered = group.sort_values("ts")
        daily.append(
            (
                float(ordered["high"].max()),
                float(ordered["low"].min()),
                float(ordered["close"].iloc[-1]),
            )
        )
    if len(daily) < length + 1:
        return None
    true_ranges: list[float] = []
    for index in range(1, len(daily)):
        high, low, close = daily[index]
        prev_close = daily[index - 1][2]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr = sum(true_ranges[:length]) / length
    for true_range in true_ranges[length:]:
        atr = (atr * (length - 1) + true_range) / length
    if not np.isfinite(atr) or atr <= 0.0:
        return None
    return float(atr)


def floor_15m(as_of: datetime, session: date | None = None) -> datetime | None:
    """Latest 15m close at or before ``as_of``, capped at the session close.

    The first bucket closes at 09:45. Before that there is no zone set.
    """
    local = as_et(as_of, "as_of")
    if session is not None:
        close_at = session_close(session)
        if close_at is not None and local > close_at:
            local = close_at
    minute = local.hour * 60 + local.minute
    start = 9 * 60 + 30
    if minute < start + 15:
        return None
    steps = (minute - start) // 15
    close_min = start + steps * 15
    # 16:00 is the last RTH 15m boundary. Later stamps stay on 16:00.
    last = 16 * 60
    if close_min > last:
        close_min = last
    return local.replace(hour=close_min // 60, minute=close_min % 60, second=0, microsecond=0)


def at_time(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=ET)


def minute_of_day(stamps: pd.Series) -> np.ndarray:
    local = pd.to_datetime(stamps)
    if getattr(local.dt, "tz", None) is not None:
        local = local.dt.tz_convert(ET)
    return (local.dt.hour * 60 + local.dt.minute).to_numpy()
