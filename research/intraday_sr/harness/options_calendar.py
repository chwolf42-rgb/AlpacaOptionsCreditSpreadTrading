"""NYSE session calendar and the two O1 expiry books. Research only; no network.

The listing schedule (which weekday expiries existed on which date) is the verified
table in ``options.py`` (``EXPIRY_SOURCES``). SPEC §7's "[P]" line "QQQ and IWM:
Fridays until 2022-12-31" is the pre-verification placeholder. O1.10 keeps the
verified calendars as the authority, so this module does not re-derive listings.

Holiday adjustment of the expiry itself is ``options.expiry_on``: a Monday expiry
moves to the next session, a Tue–Fri expiry moves to the prior session. This module
only decides which of those listed dates each book takes.

Book choice (O1.3–O1.4):
- **daily** — nearest listed expiry with calendar DTE ≤ ``daily_calendar_dte_max``
  (default 1). Calendar DTE is ``(expiry − session).days``.
- **weekly** — nearest Friday weekly (holiday-shifted settlement included) whose
  trading DTE is in ``[weekly_dte_min, weekly_dte_max]`` (default 2–10). Trading
  DTE counts sessions in ``(session, expiry]``. Mid-week listings are not substituted
  when no Friday falls in the window: O1.4 says to skip.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from research.intraday_sr.harness.options import TradingCalendar, expiry_on, listed_weekdays

# Full closures that are not on the repeating holiday rule.
# 2018-12-05: National Day of Mourning, George H.W. Bush.
# 2025-01-09: National Day of Mourning, Jimmy Carter.
_SPECIAL_CLOSURES = frozenset({date(2018, 12, 5), date(2025, 1, 9)})

# NYSE began observing Juneteenth in 2022 (June 19 fell on Sunday; closed June 20).
_JUNETEENTH_START = 2022


def easter(year: int) -> date:
    """Gregorian Easter Sunday (Anonymous algorithm). Good Friday is two days earlier."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def good_friday(year: int) -> date:
    return easter(year) - timedelta(days=2)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """``n``-th ``weekday`` in ``month`` (Monday=0). ``n`` is 1-based."""
    first = date(year, month, 1)
    shift = (weekday - first.weekday()) % 7
    return first + timedelta(days=shift + 7 * (n - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year, 12, 31)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    return cursor - timedelta(days=(cursor.weekday() - weekday) % 7)


def observed(day: date) -> date:
    """Saturday → Friday, Sunday → Monday. Weekday holidays stay put."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def nyse_holidays(first_year: int, last_year: int) -> frozenset[date]:
    """Full-day NYSE closures from ``first_year`` through ``last_year`` inclusive.

    New Year's Day of ``last_year + 1`` is included when it is observed on December 31.
    Unscheduled closures other than the two mourning days in ``_SPECIAL_CLOSURES`` are
    not listed.
    """
    closed: set[date] = set()
    for year in range(first_year, last_year + 1):
        closed.add(observed(date(year, 1, 1)))
        closed.add(nth_weekday(year, 1, 0, 3))       # MLK Day
        closed.add(nth_weekday(year, 2, 0, 3))       # Washington's Birthday
        closed.add(good_friday(year))
        closed.add(last_weekday(year, 5, 0))         # Memorial Day
        if year >= _JUNETEENTH_START:
            closed.add(observed(date(year, 6, 19)))
        closed.add(observed(date(year, 7, 4)))
        closed.add(nth_weekday(year, 9, 0, 1))       # Labor Day
        closed.add(nth_weekday(year, 11, 3, 4))      # Thanksgiving
        closed.add(observed(date(year, 12, 25)))
    closed.add(observed(date(last_year + 1, 1, 1)))
    closed.update(day for day in _SPECIAL_CLOSURES if first_year <= day.year <= last_year)
    return frozenset(closed)


def nyse_early_closes(first_year: int, last_year: int, holidays: frozenset[date]) -> frozenset[date]:
    """1:00 p.m. ET sessions.

    - The Friday after Thanksgiving.
    - July 3, when that date is a weekday and not itself the observed July 4 holiday.
    - The Friday before Independence Day when July 4 falls on Sunday (observed Monday).
      2021-07-02 is the case inside the study window; 2022-07-01 was a full session.
    - December 24, when that date is a weekday and not the observed Christmas holiday.
    """
    out: set[date] = set()
    for year in range(first_year, last_year + 1):
        black_friday = nth_weekday(year, 11, 3, 4) + timedelta(days=1)
        if black_friday.weekday() < 5 and black_friday not in holidays:
            out.add(black_friday)
        july4 = date(year, 7, 4)
        if july4.weekday() == 6:
            friday = date(year, 7, 2)
            if friday not in holidays:
                out.add(friday)
        else:
            july3 = date(year, 7, 3)
            if july3.weekday() < 5 and july3 not in holidays:
                out.add(july3)
        eve = date(year, 12, 24)
        if eve.weekday() < 5 and eve not in holidays:
            out.add(eve)
    return frozenset(out)


@dataclass(frozen=True)
class NyseCalendar:
    """Weekday sessions with the holidays above removed, plus the 13:00 early closes."""

    trading: TradingCalendar
    holidays: frozenset[date]
    early_closes: frozenset[date]
    start: date
    end: date

    def is_open(self, day: date) -> bool:
        return self.trading.is_open(day)


def nyse_calendar(start: date, end: date) -> NyseCalendar:
    """Deterministic session calendar covering ``start`` … ``end`` inclusive. No network."""
    if end < start:
        raise ValueError(f"calendar end {end} is before start {start}")
    holidays = nyse_holidays(start.year - 1, end.year + 1)
    early = nyse_early_closes(start.year - 1, end.year + 1, holidays)
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5 and cursor not in holidays:
            days.append(cursor)
        cursor += timedelta(days=1)
    if not days:
        raise ValueError(f"no NYSE sessions between {start} and {end}")
    return NyseCalendar(TradingCalendar(days), holidays, early, start, end)


def study_calendar() -> NyseCalendar:
    """Sessions from 2016 through 2027, wide enough for listing-history tests and the study window."""
    return nyse_calendar(date(2016, 1, 1), date(2027, 12, 31))


def calendar_dte(session: date, expiry: date) -> int:
    """Calendar days from ``session`` to ``expiry``. Negative if the expiry is already past."""
    return (expiry - session).days


def trading_dte(session: date, expiry: date, cal: TradingCalendar) -> int:
    """Trading sessions in ``(session, expiry]`` — the expiry session counts, the decision session does not."""
    if expiry <= session or not cal.is_open(expiry):
        return 0
    return cal.sessions_between(session, expiry) + 1


def daily_book_expiry(symbol: str, session: date, cal: TradingCalendar, *,
                      max_calendar_dte: int = 1) -> Optional[date]:
    """Nearest listed expiry with calendar DTE in ``0 … max_calendar_dte``. None → skip the daily book.

    A date is eligible only when ``expiry_on`` says a real listing settled that day, so a Tuesday
    before Tuesday expiries existed is not invented. The next calendar day is eligible when it
    itself was a listed expiry (O1.3, DTE ≤ 1), including a Friday weekly on a Thursday that had
    no Thursday listing.
    """
    if max_calendar_dte < 0:
        raise ValueError(f"max_calendar_dte must be >= 0, got {max_calendar_dte}")
    if not cal.is_open(session):
        return None
    for step in range(0, max_calendar_dte + 1):
        expiry = session + timedelta(days=step)
        if expiry_on(symbol, expiry, cal):
            return expiry
    return None


def friday_weekly_settlement(symbol: str, nominal_friday: date, cal: TradingCalendar) -> Optional[date]:
    """Settlement session of the Friday weekly whose nominal Friday is ``nominal_friday``.

    Returns None when Friday weeklies were not listed yet, or when the holiday-shifted
    session falls outside ``cal``. A Friday holiday settles on the prior open session.
    """
    if nominal_friday.weekday() != 4:
        raise ValueError(f"nominal Friday required, got {nominal_friday} ({nominal_friday.strftime('%A')})")
    if 4 not in listed_weekdays(symbol, nominal_friday):
        return None
    if not cal.days:
        return None
    if cal.is_open(nominal_friday):
        settle = nominal_friday
    else:
        if nominal_friday < cal.days[0]:
            return None
        settle = cal.prev_open(nominal_friday)
        if not cal.is_open(settle):
            return None
    if not expiry_on(symbol, settle, cal):
        return None
    return settle


def is_friday_weekly_settlement(symbol: str, session: date, cal: TradingCalendar) -> bool:
    """True when ``session`` is a Friday weekly, or the prior-session shift of a Friday holiday."""
    if not cal.is_open(session):
        return False
    if session.weekday() == 4:
        return friday_weekly_settlement(symbol, session, cal) == session
    for step in range(1, 5):
        nominal = session + timedelta(days=step)
        if nominal.weekday() != 4:
            continue
        if cal.is_open(nominal):
            return False
        return friday_weekly_settlement(symbol, nominal, cal) == session
    return False


def weekly_book_expiry(symbol: str, session: date, cal: TradingCalendar, *,
                       dte_min: int = 2, dte_max: int = 10) -> Optional[date]:
    """Nearest Friday weekly with trading DTE in ``[dte_min, dte_max]``. None → skip the weekly book.

    "Nearest" is the smallest trading DTE. A Thursday native daily is not a candidate unless that
    Thursday is the holiday shift of the Friday weekly. When two Fridays both sit in the window,
    the closer one is taken.
    """
    if dte_min > dte_max or dte_min < 0:
        raise ValueError(f"weekly DTE window {dte_min}..{dte_max} is empty")
    if not cal.is_open(session):
        return None
    days_to_friday = (4 - session.weekday()) % 7
    first = session + timedelta(days=days_to_friday)
    best: Optional[tuple[int, date]] = None
    for step in range(6):
        nominal = first + timedelta(days=7 * step)
        settle = friday_weekly_settlement(symbol, nominal, cal)
        if settle is None or settle <= session:
            continue
        dte = trading_dte(session, settle, cal)
        if dte_min <= dte <= dte_max and (best is None or dte < best[0]):
            best = (dte, settle)
    return None if best is None else best[1]
