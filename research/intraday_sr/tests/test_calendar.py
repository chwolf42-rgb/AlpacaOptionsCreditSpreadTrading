"""NYSE calendar: holidays, the 13:00 early close, and a normal Wednesday."""

from __future__ import annotations

from datetime import date

from research.intraday_sr.data.calendar import (
    close_time,
    is_session,
    last_bar_open,
    next_session,
    sessions_between,
)


def test_regular_early_and_closed():
    assert is_session(date(2019, 1, 2))
    assert close_time(date(2019, 1, 2)).hour == 16
    assert last_bar_open(date(2019, 1, 2)).hour == 15
    assert last_bar_open(date(2019, 1, 2)).minute == 55

    assert is_session(date(2024, 7, 3))
    assert close_time(date(2024, 7, 3)).hour == 13
    assert last_bar_open(date(2024, 7, 3)).hour == 12
    assert last_bar_open(date(2024, 7, 3)).minute == 55

    assert not is_session(date(2024, 7, 4))
    assert not is_session(date(2025, 1, 9))  # national day of mourning
    assert not is_session(date(2024, 6, 1))  # Saturday
    assert close_time(date(2024, 7, 4)) is None


def test_sessions_skip_the_holiday_and_the_weekend():
    days = sessions_between(date(2024, 7, 2), date(2024, 7, 8))
    assert days == [
        date(2024, 7, 2),
        date(2024, 7, 3),
        date(2024, 7, 5),
        date(2024, 7, 8),
    ]
    assert next_session(date(2024, 7, 3)) == date(2024, 7, 5)
