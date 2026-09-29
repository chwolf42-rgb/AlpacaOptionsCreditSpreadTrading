"""Process-wide cap for Alpaca market-data HTTP.

The equity, options, and crypto paper keys share one Alpaca market-data
budget (200 requests/min). This bot's share is a hard rolling 60-second cap,
default 60 pages, overridden with OPTIONS_DATA_BUDGET_PER_MIN. Requests are
spaced across the minute. A low X-RateLimit-Remaining does not add a wait.
Only an HTTP 429 does, using Retry-After or X-RateLimit-Reset.

Trading-API calls (paper-api.alpaca.markets) are a different limit and are
not counted here.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Iterator, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

ENV_BUDGET = "OPTIONS_DATA_BUDGET_PER_MIN"
DEFAULT_BUDGET_PER_MIN = 60
WINDOW_SECONDS = 60.0

# Lower runs first when several callers are waiting on one slot.
PRIORITY_EXIT = 0
PRIORITY_ARMED = 1
PRIORITY_WATCH = 2

# One full symbol scan is a daily bar page plus an hourly bar page.
# alpaca-py asks for up to 10_000 bars per page; both of this bot's windows
# are far smaller, so each get_stock_bars call is one HTTP page.
PAGES_PER_SYMBOL = 2
# Exit check: option snapshot for the two legs, plus one daily bar page
# for the structure-break read. Fetched again later if the name is scanned;
# the second read is fresh, not a cache.
PAGES_PER_OPEN_SPREAD = 2

_priority: ContextVar[int] = ContextVar("options_data_priority", default=PRIORITY_WATCH)

_install_lock = threading.Lock()
_limiter: Optional["MarketDataLimiter"] = None


def estimate_rth_scan_pages(n_symbols: int, n_open_spreads: int, n_chains: int = 0) -> int:
    """Data-API pages for one full pass over the universe.

    ``n_chains`` is entry-ready option-snapshot requests (one per 100
    contracts). Contract listing pages go to the trading API and are not
    included. Armed names cost the same two bar pages as the rest of the
    watchlist until a chain is actually pulled.
    """
    return (
        int(n_symbols) * PAGES_PER_SYMBOL
        + int(n_open_spreads) * PAGES_PER_OPEN_SPREAD
        + int(n_chains)
    )


def parse_budget(raw: Optional[str]) -> int:
    if raw is None or str(raw).strip() == "":
        return DEFAULT_BUDGET_PER_MIN
    try:
        value = int(str(raw).strip())
    except ValueError:
        log.warning(
            "%s=%r is not an integer; using %d",
            ENV_BUDGET,
            raw,
            DEFAULT_BUDGET_PER_MIN,
        )
        return DEFAULT_BUDGET_PER_MIN
    if value < 1:
        log.warning("%s=%s is below 1; using 1", ENV_BUDGET, value)
        return 1
    return value


def budget_from_env(env: Optional[dict[str, str]] = None) -> int:
    source = os.environ if env is None else env
    return parse_budget(source.get(ENV_BUDGET))


@contextmanager
def data_priority(level: int) -> Iterator[None]:
    token = _priority.set(int(level))
    try:
        yield
    finally:
        _priority.reset(token)


def current_priority() -> int:
    return int(_priority.get())


class MarketDataLimiter:
    """Hard rolling-window cap with smooth spacing and priority waiters.

    ``mono`` and ``sleep`` are injectable so tests can prove the cap without
    waiting a real minute. Production uses time.monotonic and time.sleep.
    """

    def __init__(
        self,
        budget_per_min: int,
        *,
        mono: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        wall: Optional[Callable[[], float]] = None,
    ) -> None:
        if budget_per_min < 1:
            raise ValueError("budget_per_min must be >= 1")
        self.budget = int(budget_per_min)
        self._mono = mono or time.monotonic
        self._sleep = sleep or time.sleep
        self._wall = wall or time.time
        self._min_interval = WINDOW_SECONDS / float(self.budget)
        self._times: deque[float] = deque()
        self._history: list[float] = []
        self._waiters: list[tuple[int, int]] = []
        self._seq = 0
        self._backoff_until = 0.0
        self._last_log = 0.0
        self._logged_once = False
        self.last_remaining: Optional[int] = None
        self._cv = threading.Condition()
        self._on_queued: Optional[Callable[[], None]] = None
        self._wait_hook: Optional[Callable[[], None]] = None

    def set_on_queued(self, hook: Optional[Callable[[], None]]) -> None:
        """Test hook. Runs after the waiter is registered, without the limiter lock."""
        self._on_queued = hook

    def set_wait_hook(self, hook: Optional[Callable[[], None]]) -> None:
        """Called before a wait of at least 5s (heartbeat while pacing)."""
        self._wait_hook = hook

    def acquired_at(self) -> list[float]:
        """Every grant, including ones that have aged out of the rolling window."""
        with self._cv:
            return list(self._history)

    def pages_in_window(self) -> int:
        with self._cv:
            now = self._mono()
            self._expire(now)
            return len(self._times)

    def acquire(self, priority: Optional[int] = None) -> None:
        if priority is None:
            priority = current_priority()
        with self._cv:
            ticket = (int(priority), self._seq)
            self._seq += 1
            self._waiters.append(ticket)
            hook = self._on_queued
        if hook is not None:
            hook()
        try:
            while True:
                with self._cv:
                    wait, ready = self._ready(ticket)
                    if ready:
                        self._waiters.remove(ticket)
                        now = self._mono()
                        self._expire(now)
                        self._times.append(now)
                        self._history.append(now)
                        self._maybe_log(now)
                        self._cv.notify_all()
                        return
                    mine = min(self._waiters) == ticket if self._waiters else True
                    if not mine:
                        # Another caller is ahead. Wait for it to grant; do not
                        # burn the shared clock or the rate window.
                        self._cv.wait(timeout=0.05)
                        continue
                    before = self._mono()
                self._sleep_paced(wait)
                if self._mono() <= before:
                    # Sub-ulp delay: a float clock (and a too-short real sleep)
                    # would otherwise spin without opening the window.
                    step = math.nextafter(before, math.inf) - before
                    self._sleep(step if step > 0 else 1e-6)
        finally:
            with self._cv:
                if ticket in self._waiters:
                    self._waiters.remove(ticket)
                    self._cv.notify_all()

    def note_response(self, response: object) -> None:
        """Record rate-limit headers. Sleep only on HTTP 429.

        X-RateLimit-Remaining is stored and logged at debug. It never adds a wait.
        """
        status = getattr(response, "status_code", None)
        headers = getattr(response, "headers", None) or {}
        remaining = _header(headers, "X-RateLimit-Remaining", "X-Ratelimit-Remaining")
        if remaining is not None:
            parsed = _parse_int(remaining)
            if parsed is not None:
                self.last_remaining = parsed
                log.debug(
                    "market-data X-RateLimit-Remaining=%s (not a wait)",
                    parsed,
                )
        if status != 429:
            return
        delay = backoff_seconds(headers, self._wall())
        if delay is None or delay <= 0:
            log.warning(
                "market-data HTTP 429 with no usable Retry-After or X-RateLimit-Reset"
            )
            return
        with self._cv:
            now = self._mono()
            self._backoff_until = max(self._backoff_until, now + delay)
            self._cv.notify_all()
        log.warning("market-data HTTP 429; backing off %.2fs", delay)
        self._sleep_paced(delay)

    def _ready(self, ticket: tuple[int, int]) -> tuple[float, bool]:
        now = self._mono()
        self._expire(now)
        best = min(self._waiters) if self._waiters else ticket
        wait = self._seconds_until_slot(now)
        if best != ticket:
            return 0.01, False
        if wait > 0:
            return wait, False
        return 0.0, True

    def _seconds_until_slot(self, now: float) -> float:
        wait = 0.0
        if self._backoff_until > now:
            wait = self._backoff_until - now
        if len(self._times) >= self.budget:
            wait = max(wait, WINDOW_SECONDS - (now - self._times[0]))
        if self._times:
            spaced = self._times[-1] + self._min_interval - now
            if spaced > 0:
                wait = max(wait, spaced)
        return wait

    def _expire(self, now: float) -> None:
        while self._times and now - self._times[0] >= WINDOW_SECONDS:
            self._times.popleft()

    def _maybe_log(self, now: float) -> None:
        if not self._logged_once:
            self._last_log = now
            self._logged_once = True
            return
        if now - self._last_log < WINDOW_SECONDS:
            return
        log.info(
            "market-data budget %d/%d pages in the rolling 60s",
            len(self._times),
            self.budget,
        )
        self._last_log = now

    def _sleep_paced(self, seconds: float) -> None:
        left = float(seconds)
        while left > 0:
            step = min(left, 30.0)
            if step >= 5.0 and self._wait_hook is not None:
                self._wait_hook()
            self._sleep(step)
            left -= step


def backoff_seconds(headers: object, wall_now: float) -> Optional[float]:
    """Seconds to wait after HTTP 429. None when neither header is usable.

    Retry-After wins. Otherwise X-RateLimit-Reset, as a unix timestamp when
    the value is large and as a relative delay when it is small.
    """
    retry_after = _header(headers, "Retry-After")
    if retry_after is not None and str(retry_after).strip() != "":
        return _parse_retry_after(str(retry_after).strip(), wall_now)
    reset = _header(headers, "X-RateLimit-Reset", "X-Ratelimit-Reset")
    if reset is None or str(reset).strip() == "":
        return None
    return _parse_reset(str(reset).strip(), wall_now)


def _parse_retry_after(value: str, wall_now: float) -> float:
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return 0.0
    if when is None:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = datetime.fromtimestamp(wall_now, timezone.utc)
    return max(0.0, (when - now).total_seconds())


def _parse_reset(value: str, wall_now: float) -> float:
    try:
        reset = float(value)
    except ValueError:
        return 0.0
    if reset > 1_000_000_000:
        return max(0.0, reset - wall_now)
    return max(0.0, reset)


def _header(headers: object, *names: str) -> Optional[str]:
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    for name in names:
        raw = getter(name)
        if raw is not None and str(raw).strip() != "":
            return str(raw)
    return None


def _parse_int(raw: str) -> Optional[int]:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def get_limiter() -> MarketDataLimiter:
    global _limiter
    with _install_lock:
        if _limiter is None:
            _limiter = MarketDataLimiter(budget_from_env())
        return _limiter


def budget_per_min() -> int:
    return get_limiter().budget


def replace_limiter(limiter: Optional[MarketDataLimiter]) -> None:
    """Swap the process limiter. Tests pass None to rebuild from the env."""
    global _limiter
    with _install_lock:
        _limiter = limiter


def reset_limiter() -> MarketDataLimiter:
    limiter = MarketDataLimiter(budget_from_env())
    replace_limiter(limiter)
    return limiter


_WRAPPED = "_options_md_limited"


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def install_market_data_session(session: object, base_url: str) -> None:
    """Count every HTTP call this data-client session makes, including pages and retries."""
    if getattr(session, _WRAPPED, False):
        return
    original = session.request
    data_host = _host(str(base_url))

    def request(method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        target = _host(str(url))
        if target != data_host and "data.alpaca.markets" not in target:
            return original(method, url, *args, **kwargs)
        limiter = get_limiter()
        limiter.acquire()
        response = original(method, url, *args, **kwargs)
        limiter.note_response(response)
        return response

    session.request = request  # type: ignore[method-assign]
    setattr(session, _WRAPPED, True)


def install_market_data_client(client: object) -> None:
    session = getattr(client, "_session", None)
    base = getattr(client, "_base_url", "") or "https://data.alpaca.markets"
    if hasattr(base, "value"):
        base = base.value
    if session is None:
        return
    install_market_data_session(session, str(base))
