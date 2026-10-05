"""TimeOfDayLimiter with a fake clock. No real waiting."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from research.intraday_sr.data.ratelimit import (
    OFF_CAP,
    PEAK_CAP,
    PEAK_PACE,
    TimeOfDayLimiter,
)

CT = ZoneInfo("America/Chicago")


class FakeClock:
    def __init__(self, moment: datetime) -> None:
        self.mono = 0.0
        self.moment = moment
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.mono += float(seconds)
        self.moment += timedelta(seconds=float(seconds))

    def now(self) -> datetime:
        return self.moment


def _limiter(moment: datetime) -> tuple[TimeOfDayLimiter, FakeClock]:
    clock = FakeClock(moment)
    return TimeOfDayLimiter(mono=clock.monotonic, sleep=clock.sleep, now=clock.now), clock


def test_switch_points_and_holiday_is_a_weekday():
    peak, _ = _limiter(datetime(2026, 10, 5, 8, 15, tzinfo=CT))  # Monday
    assert peak.limits_at() == (30, 28)
    before, _ = _limiter(datetime(2026, 10, 5, 8, 14, tzinfo=CT))
    assert before.limits_at() == (60, 57)
    at_close, _ = _limiter(datetime(2026, 10, 5, 15, 15, tzinfo=CT))
    assert at_close.limits_at() == (60, 57)
    still, _ = _limiter(datetime(2026, 10, 5, 15, 14, tzinfo=CT))
    assert still.limits_at() == (30, 28)
    weekend, _ = _limiter(datetime(2026, 10, 3, 10, 0, tzinfo=CT))  # Saturday
    assert weekend.limits_at() == (OFF_CAP, 57)
    # Christmas 2025 is a Thursday. Holidays stay on the weekday cap.
    holiday, _ = _limiter(datetime(2025, 12, 25, 10, 0, tzinfo=CT))
    assert holiday.limits_at() == (PEAK_CAP, PEAK_PACE)


def test_pace_and_the_60_to_30_switch_blocks():
    limiter, clock = _limiter(datetime(2026, 10, 5, 7, 0, tzinfo=CT))
    for _ in range(40):
        limiter.acquire()
    assert limiter.grants_in_window() == 40
    # Jump the Chicago clock into the peak window without aging the grants.
    clock.moment = datetime(2026, 10, 5, 8, 15, tzinfo=CT)
    before = clock.mono
    limiter.acquire()
    assert clock.mono > before
    assert limiter.grants_in_window() <= PEAK_CAP


def test_429_backoff_doubles_from_30_to_the_cap():
    limiter, clock = _limiter(datetime(2026, 10, 5, 20, 0, tzinfo=CT))
    limiter.note_response(429)
    limiter.note_response(429)
    limiter.note_response(200)
    limiter.note_response(429)
    assert clock.sleeps[0] == 30.0
    assert clock.sleeps[1] == 60.0
    assert clock.sleeps[2] == 30.0  # reset after the 200

    fresh, clock2 = _limiter(datetime(2026, 10, 5, 20, 0, tzinfo=CT))
    delay = 30.0
    seen = []
    for _ in range(8):
        fresh.note_response(429)
        seen.append(clock2.sleeps[-1])
        delay = min(delay * 2, 600.0)
    assert seen[0] == 30.0
    assert max(seen) == 600.0
    assert seen[-1] == 600.0
