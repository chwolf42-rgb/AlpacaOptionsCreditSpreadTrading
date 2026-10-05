"""NYSE sessions for 2019-01-01 through 2026-12-31.

The sets below are the XNYS calendar (weekdays closed, and 13:00 ET early
closes). They were taken from ``pandas_market_calendars`` so research runs
do not need that package and do not need a network. Holidays include the
2025-01-09 closure. A weekday outside this window is treated as a session
only if it is not already listed; the study window sits inside the table.

Early closes are 13:00 ET. Regular closes are 16:00 ET. The last 5m bar
opens five minutes earlier (12:55 or 15:55).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from research.intraday_sr.types import ET

# Weekdays the NYSE is closed, 2019-01-01 .. 2026-12-31.
HOLIDAYS: frozenset[date] = frozenset(
    date.fromisoformat(day)
    for day in (
        "2019-01-01",
        "2019-01-21",
        "2019-02-18",
        "2019-04-19",
        "2019-05-27",
        "2019-07-04",
        "2019-09-02",
        "2019-11-28",
        "2019-12-25",
        "2020-01-01",
        "2020-01-20",
        "2020-02-17",
        "2020-04-10",
        "2020-05-25",
        "2020-07-03",
        "2020-09-07",
        "2020-11-26",
        "2020-12-25",
        "2021-01-01",
        "2021-01-18",
        "2021-02-15",
        "2021-04-02",
        "2021-05-31",
        "2021-07-05",
        "2021-09-06",
        "2021-11-25",
        "2021-12-24",
        "2022-01-17",
        "2022-02-21",
        "2022-04-15",
        "2022-05-30",
        "2022-06-20",
        "2022-07-04",
        "2022-09-05",
        "2022-11-24",
        "2022-12-26",
        "2023-01-02",
        "2023-01-16",
        "2023-02-20",
        "2023-04-07",
        "2023-05-29",
        "2023-06-19",
        "2023-07-04",
        "2023-09-04",
        "2023-11-23",
        "2023-12-25",
        "2024-01-01",
        "2024-01-15",
        "2024-02-19",
        "2024-03-29",
        "2024-05-27",
        "2024-06-19",
        "2024-07-04",
        "2024-09-02",
        "2024-11-28",
        "2024-12-25",
        "2025-01-01",
        "2025-01-09",
        "2025-01-20",
        "2025-02-17",
        "2025-04-18",
        "2025-05-26",
        "2025-06-19",
        "2025-07-04",
        "2025-09-01",
        "2025-11-27",
        "2025-12-25",
        "2026-01-01",
        "2026-01-19",
        "2026-02-16",
        "2026-04-03",
        "2026-05-25",
        "2026-06-19",
        "2026-07-03",
        "2026-09-07",
        "2026-11-26",
        "2026-12-25",
    )
)

# Sessions that close at 13:00 ET instead of 16:00.
EARLY_CLOSES: frozenset[date] = frozenset(
    date.fromisoformat(day)
    for day in (
        "2019-07-03",
        "2019-11-29",
        "2019-12-24",
        "2020-11-27",
        "2020-12-24",
        "2021-11-26",
        "2022-11-25",
        "2023-07-03",
        "2023-11-24",
        "2024-07-03",
        "2024-11-29",
        "2024-12-24",
        "2025-07-03",
        "2025-11-28",
        "2025-12-24",
        "2026-11-27",
        "2026-12-24",
    )
)

RTH_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
BAR_MINUTES = 5


def is_session(day: date) -> bool:
    """True on a weekday that is not an NYSE holiday."""
    return day.weekday() < 5 and day not in HOLIDAYS


def close_time(day: date) -> time | None:
    if not is_session(day):
        return None
    if day in EARLY_CLOSES:
        return EARLY_CLOSE
    return REGULAR_CLOSE


def session_close(day: date) -> datetime | None:
    """Tz-aware close of ``day``, or None if the NYSE is shut."""
    clock = close_time(day)
    if clock is None:
        return None
    return datetime.combine(day, clock, tzinfo=ET)


def session_open(day: date) -> datetime | None:
    if not is_session(day):
        return None
    return datetime.combine(day, RTH_OPEN, tzinfo=ET)


def last_bar_open(day: date) -> time | None:
    """Open time of the last 5m bar (15:55, or 12:55 on an early close)."""
    clock = close_time(day)
    if clock is None:
        return None
    close_dt = datetime.combine(day, clock)
    return (close_dt - timedelta(minutes=BAR_MINUTES)).time()


def sessions_between(start: date, end: date) -> list[date]:
    """Inclusive NYSE sessions from ``start`` through ``end``."""
    if end < start:
        return []
    out: list[date] = []
    cursor = start
    while cursor <= end:
        if is_session(cursor):
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def next_session(day: date) -> date | None:
    """The next session strictly after ``day``. None past the vendored window."""
    cursor = day + timedelta(days=1)
    limit = date(2026, 12, 31)
    while cursor <= limit:
        if is_session(cursor):
            return cursor
        cursor += timedelta(days=1)
    return None
