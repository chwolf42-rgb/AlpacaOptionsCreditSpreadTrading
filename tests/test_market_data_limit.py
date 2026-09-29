"""Rolling 60s market-data cap, exit priority, and 429-only backoff."""

from __future__ import annotations

import math
import threading
from email.utils import formatdate

import pytest

from alpaca_options_credit.config import load_config
from alpaca_options_credit.market_data_limit import (
    PRIORITY_EXIT,
    PRIORITY_WATCH,
    MarketDataLimiter,
    backoff_seconds,
    estimate_rth_scan_pages,
    get_limiter,
    pages_for_bar_rows,
    install_market_data_session,
    replace_limiter,
    reset_limiter,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.wall = 1_700_000_000.0
        self.slept: list[float] = []
        self._lock = threading.Lock()
        self.gate: threading.Event | None = None

    def mono(self) -> float:
        with self._lock:
            return self.t

    def wall_now(self) -> float:
        return self.wall

    def sleep(self, dt: float) -> None:
        if self.gate is not None:
            assert self.gate.wait(2), "sleep started before both waiters queued"
        with self._lock:
            self.slept.append(float(dt))
            nxt = self.t + float(dt)
            # A delay smaller than the float spacing of `t` must still move time.
            self.t = nxt if nxt > self.t else math.nextafter(self.t, math.inf)


def _limiter(budget: int, clock: _Clock | None = None) -> tuple[MarketDataLimiter, _Clock]:
    clock = clock or _Clock()
    return (
        MarketDataLimiter(
            budget,
            mono=clock.mono,
            sleep=clock.sleep,
            wall=clock.wall_now,
        ),
        clock,
    )


def _in_window(times: list[float], t: float, window: float = 60.0) -> int:
    return sum(1 for x in times if 0 <= t - x < window)


def test_rolling_cap_never_exceeded_and_requests_are_spaced(caplog):
    lim, clock = _limiter(60)
    with caplog.at_level("INFO"):
        for _ in range(180):
            lim.acquire()
    assert "market-data budget 60/60 pages in the rolling 60s" in caplog.text
    times = lim.acquired_at()
    assert len(times) == 180
    for t in times:
        assert _in_window(times, t) <= 60
    gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
    assert min(gaps) >= 1.0 - 1e-9
    assert clock.t == pytest.approx(179.0)

    odd, _ = _limiter(7)
    for _ in range(80):
        odd.acquire()
    odd_times = odd.acquired_at()
    for t in odd_times:
        assert _in_window(odd_times, t) <= 7


def test_threaded_acquires_stay_inside_the_cap():
    lim, _clock = _limiter(10)
    threads = [
        threading.Thread(target=lambda: [lim.acquire() for _ in range(15)])
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    times = lim.acquired_at()
    assert len(times) == 45
    for t in times:
        assert _in_window(times, t) <= 10


def test_exit_waiter_runs_before_scan_when_the_only_slot_is_busy():
    clock = _Clock()
    clock.gate = threading.Event()
    lim, _ = _limiter(1, clock)
    lim.acquire(priority=PRIORITY_WATCH)
    queued = {"n": 0}

    def _queued() -> None:
        queued["n"] += 1
        if queued["n"] >= 2:
            assert clock.gate is not None
            clock.gate.set()

    lim.set_on_queued(_queued)
    order: list[str] = []

    def _run(priority: int, name: str) -> None:
        lim.acquire(priority=priority)
        order.append(name)

    scan = threading.Thread(target=_run, args=(PRIORITY_WATCH, "scan"))
    exit_ = threading.Thread(target=_run, args=(PRIORITY_EXIT, "exit"))
    scan.start()
    exit_.start()
    scan.join(3)
    exit_.join(3)
    assert not scan.is_alive()
    assert not exit_.is_alive()
    assert order == ["exit", "scan"]


def test_low_remaining_does_not_wait_but_429_does():
    lim, clock = _limiter(60)

    class Resp:
        def __init__(self, status: int, headers: dict[str, str]):
            self.status_code = status
            self.headers = headers

    lim.acquire()
    lim.note_response(
        Resp(
            200,
            {
                "X-RateLimit-Remaining": "1",
                "X-RateLimit-Reset": "1899999999",
            },
        )
    )
    assert clock.slept == []
    assert lim.last_remaining == 1

    lim.note_response(Resp(429, {"Retry-After": "12", "X-RateLimit-Reset": "8"}))
    assert clock.slept == [12.0]

    lim.note_response(Resp(429, {"X-Ratelimit-Reset": str(clock.wall + 9)}))
    assert clock.slept[-1] == pytest.approx(9.0)


def test_backoff_honors_retry_after_and_reset_header():
    wall = 1_700_000_000.0
    assert backoff_seconds({"Retry-After": "4"}, wall) == 4.0
    assert backoff_seconds({"X-RateLimit-Reset": str(wall + 15)}, wall) == pytest.approx(15.0)
    assert backoff_seconds({"X-Ratelimit-Reset": "6"}, wall) == 6.0
    http_date = formatdate(wall + 30, usegmt=True)
    assert backoff_seconds({"Retry-After": http_date}, wall) == pytest.approx(30.0, abs=1.0)
    assert backoff_seconds({"X-RateLimit-Remaining": "0"}, wall) is None


def test_session_counts_each_data_page_and_ignores_trading_api(monkeypatch):
    clock = _Clock()
    lim, _ = _limiter(60, clock)
    replace_limiter(lim)

    class Resp:
        status_code = 200
        headers = {"X-RateLimit-Remaining": "50"}

    class Session:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def request(self, method, url, *args, **kwargs):
            self.urls.append(url)
            return Resp()

    try:
        session = Session()
        install_market_data_session(session, "https://data.alpaca.markets")
        session.request("GET", "https://data.alpaca.markets/v2/stocks/bars")
        session.request(
            "GET",
            "https://data.alpaca.markets/v2/stocks/bars",
            params={"page_token": "next"},
        )
        session.request("GET", "https://paper-api.alpaca.markets/v2/orders")
        assert len(lim.acquired_at()) == 2
        assert len(session.urls) == 3
        assert get_limiter() is lim
    finally:
        reset_limiter()


def test_session_429_backs_off_before_returning():
    clock = _Clock()
    lim, _ = _limiter(60, clock)
    replace_limiter(lim)

    class Resp:
        def __init__(self, status: int, headers: dict[str, str]):
            self.status_code = status
            self.headers = headers

    class Session:
        def request(self, method, url, *args, **kwargs):
            return Resp(429, {"Retry-After": "7"})

    try:
        session = Session()
        install_market_data_session(session, "https://data.alpaca.markets")
        session.request("GET", "https://data.alpaca.markets/v1beta1/options/snapshots")
        assert 7.0 in clock.slept
    finally:
        reset_limiter()


def test_full_universe_scan_fits_at_60_per_min():
    cfg = load_config()
    symbols = list(cfg["universe"]["symbols"])
    assert len(symbols) == 57
    # One multi-symbol daily page + one multi-symbol hourly page.
    assert pages_for_bar_rows(57 * 126) == 1
    assert pages_for_bar_rows(10_000) == 1
    assert pages_for_bar_rows(10_001) == 2
    pages = estimate_rth_scan_pages(len(symbols), 0)
    assert pages == 2
    assert pages <= 60
    # Five open spreads: one fresh snapshot each. Structure bars are in the batch.
    assert estimate_rth_scan_pages(57, 5) == 7
    # Every clock hour across the 23-day window is still one loop.
    assert pages_for_bar_rows(57 * 552) == 4
    assert estimate_rth_scan_pages(57, 5, hourly_bars_each=552) == 10
    assert estimate_rth_scan_pages(57, 5, hourly_bars_each=552) <= 60
    # Later loop in the same hour: closed bars reused, snapshots only.
    assert estimate_rth_scan_pages(57, 5, reuse_closed_bars=True) == 5


def test_data_clients_are_wrapped_and_trading_client_is_not(monkeypatch):
    monkeypatch.delenv("OPTIONS_DATA_BUDGET_PER_MIN", raising=False)
    reset_limiter()
    from alpaca_options_credit.broker.alpaca import AlpacaMarketData
    from alpaca_options_credit.credentials import Credentials

    creds = Credentials(
        api_key_id="test-key",
        api_secret_key="test-secret",
        base_url="https://paper-api.alpaca.markets",
        data_url="https://data.alpaca.markets",
        expected_account_number=None,
    )
    data = AlpacaMarketData(creds, {})
    assert data.limits_market_data is True
    assert getattr(data._stock._session, "_options_md_limited", False)
    assert getattr(data._opt_data._session, "_options_md_limited", False)
    assert not getattr(data._trading._session, "_options_md_limited", False)
    reset_limiter()
