"""Time-of-day cap for any future Alpaca market-data GET.

Spec §2.3. This process does not install the limiter on a live client and
does not open a socket.

- Weekdays 08:15 inclusive to 15:15 exclusive, America/Chicago: hard cap 30
  requests per rolling 60 s, paced at 28 per minute.
- All other times, including weekends: hard cap 60, paced at 57.
- Holidays are ordinary weekdays. The exchange calendar is not consulted.
- The cap is read on every ``acquire``, so a 60→30 switch blocks immediately
  when the rolling window is already over the new cap.
- HTTP 429: global backoff of 30 s, doubling up to 600 s. A non-429 response
  resets the next backoff to 30 s.

Clocks are injectable so tests do not wait.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, time as clock_time
from typing import Callable
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
WINDOW_SECONDS = 60.0
PEAK_START = clock_time(8, 15)
PEAK_END = clock_time(15, 15)
PEAK_CAP = 30
PEAK_PACE = 28
OFF_CAP = 60
OFF_PACE = 57
BACKOFF_START = 30.0
BACKOFF_MAX = 600.0


class TimeOfDayLimiter:
    """Rolling-window limiter whose budget follows the Chicago clock."""

    def __init__(
        self,
        *,
        mono: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._mono = mono or time.monotonic
        self._sleep = sleep or time.sleep
        self._now = now or (lambda: datetime.now(CT))
        self._grants: deque[float] = deque()
        self._backoff_until = 0.0
        self._next_backoff = BACKOFF_START

    def limits_at(self, moment: datetime | None = None) -> tuple[int, int]:
        """``(hard_cap, paced_per_minute)`` at ``moment`` (default: the clock)."""
        moment = self._now() if moment is None else moment
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo("UTC"))
        local = moment.astimezone(CT)
        if local.weekday() >= 5:
            return OFF_CAP, OFF_PACE
        clock = local.time()
        if PEAK_START <= clock < PEAK_END:
            return PEAK_CAP, PEAK_PACE
        return OFF_CAP, OFF_PACE

    def acquire(self) -> None:
        """Block until a request is inside the cap, the pace, and any 429 backoff."""
        while True:
            cap, pace = self.limits_at()
            now = self._mono()
            self._expire(now)
            wait = 0.0
            if self._backoff_until > now:
                wait = self._backoff_until - now
            if len(self._grants) >= cap:
                wait = max(wait, WINDOW_SECONDS - (now - self._grants[0]))
            if self._grants:
                spaced = self._grants[-1] + (WINDOW_SECONDS / float(pace)) - now
                if spaced > 0:
                    wait = max(wait, spaced)
            if wait <= 0 and len(self._grants) < cap:
                self._grants.append(self._mono())
                return
            before = self._mono()
            self._sleep(wait if wait > 0 else 0.0)
            if self._mono() <= before:
                raise RuntimeError("limiter clock did not advance during sleep")

    def note_response(self, status: int) -> None:
        """Record an HTTP status. 429 sleeps on the injected clock and doubles."""
        if int(status) != 429:
            self._next_backoff = BACKOFF_START
            return
        delay = self._next_backoff
        self._next_backoff = min(self._next_backoff * 2.0, BACKOFF_MAX)
        now = self._mono()
        self._backoff_until = max(self._backoff_until, now + delay)
        self._sleep(delay)

    def grants_in_window(self) -> int:
        now = self._mono()
        self._expire(now)
        return len(self._grants)

    def _expire(self, now: float) -> None:
        while self._grants and now - self._grants[0] >= WINDOW_SECONDS:
            self._grants.popleft()
