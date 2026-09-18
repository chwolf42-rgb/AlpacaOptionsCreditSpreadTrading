"""US listed equity-options session. RTH only; no extended-hours options."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


def as_et(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    return now.astimezone(ET)


def is_weekday(now: datetime) -> bool:
    return as_et(now).weekday() < 5


def is_rth(now: datetime, *, open_t: time = RTH_OPEN, close_t: time = RTH_CLOSE) -> bool:
    """True during 9:30–16:00 America/New_York on weekdays.

    Equity and ETF options (SPY/QQQ/IWM and single names) on Alpaca do not
    accept extended_hours=true. Watchdog: the engine still heartbeats off-hours
    with status=idle_off_hours so overnight silence is not treated as a crash.
    """
    local = as_et(now)
    if local.weekday() >= 5:
        return False
    return open_t <= local.time() < close_t


def parse_hhmm(value: str, default: time) -> time:
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return default
