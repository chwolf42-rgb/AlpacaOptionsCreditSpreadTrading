"""Alpaca stock bars are oldest-first: `limit` keeps the oldest rows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from alpaca_options_credit.bar_quality import (
    STALE_BARS,
    newest_closed_bars,
    next_bar_close,
    stale_bars_detail,
)
from alpaca_options_credit.broker.alpaca import AlpacaMarketData, _bars_start
from alpaca_options_credit.broker.fixture_data import bullish_pullback_bars
from alpaca_options_credit.models import Bar

UTC = timezone.utc


class _RawBar:
    def __init__(self, ts: datetime, close: float):
        self.timestamp = ts
        self.open = close
        self.high = close + 1
        self.low = close - 1
        self.close = close
        self.volume = 1_000


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


class OldestFirstStockClient:
    """Keeps the oldest `limit` rows in [start, end], like Alpaca stock bars."""

    def __init__(self, series: list[_RawBar]):
        self.series = series
        self.requests = []

    def get_stock_bars(self, req):
        self.requests.append(req)
        symbols = req.symbol_or_symbols
        if isinstance(symbols, str):
            symbols = [symbols]
        start = _as_utc(req.start)
        end = _as_utc(req.end)
        window = [b for b in self.series if start <= _as_utc(b.timestamp) <= end]
        window.sort(key=lambda b: _as_utc(b.timestamp))
        limit = getattr(req, "limit", None)
        if limit:
            window = window[: int(limit)]
        return SimpleNamespace(data={symbol: list(window) for symbol in symbols})


def _market(client: OldestFirstStockClient) -> AlpacaMarketData:
    md = AlpacaMarketData.__new__(AlpacaMarketData)
    md.cfg = {"market_data": {"stock_feed": "iex"}}
    md._stock = client
    return md


def _weekdays(start: datetime, end: datetime) -> list[datetime]:
    day = start.date()
    last = end.date()
    out: list[datetime] = []
    while day <= last:
        if day.weekday() < 5:
            out.append(datetime(day.year, day.month, day.day, 4, 0, tzinfo=UTC))
        day += timedelta(days=1)
    return out


def test_oldest_first_client_keeps_oldest_limit():
    end = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)
    start = _bars_start("1Day", 60, end)
    series = [_RawBar(ts, 100.0 + i) for i, ts in enumerate(_weekdays(start, end))]
    client = OldestFirstStockClient(series)
    req = SimpleNamespace(symbol_or_symbols="AMAT", start=start, end=end, limit=60)
    window = [b for b in series if start <= b.timestamp <= end]
    got = client.get_stock_bars(req).data["AMAT"]
    assert len(got) == 60
    assert got[0].timestamp == window[0].timestamp
    assert got[-1].timestamp == window[59].timestamp
    assert got[-1].timestamp < window[-1].timestamp


def test_daily_bars_end_on_newest_closed_session(monkeypatch):
    """AMAT 2026-09-21: limit=60 over the lookback ended 2026-06-21, not September."""
    end = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)  # Monday 11:00 ET, session open
    monkeypatch.setattr("alpaca_options_credit.broker.alpaca._bars_end", lambda: end)
    start = _bars_start("1Day", 60, end)
    series = [_RawBar(ts, 100.0 + i) for i, ts in enumerate(_weekdays(start, end))]
    assert series[-1].timestamp.date().isoformat() == "2026-09-21"
    client = OldestFirstStockClient(series)
    bars = _market(client).bars("AMAT", "1Day", 60)

    assert len(client.requests) == 1
    assert client.requests[0].limit is None
    assert len(bars) == 60
    # Monday's daily bar is still forming. The series ends on Friday's close.
    assert bars[-1].ts.date().isoformat() == "2026-09-18"
    assert bars[0].ts > series[0].timestamp
    oldest_sixty = series[:60]
    assert bars[-1].ts > oldest_sixty[-1].timestamp


def test_hourly_limit_does_not_keep_the_oldest_rows(monkeypatch):
    end = datetime(2026, 9, 16, 15, 30, tzinfo=UTC)
    monkeypatch.setattr("alpaca_options_credit.broker.alpaca._bars_end", lambda: end)
    start = _bars_start("1Hour", 120, end)
    series: list[_RawBar] = []
    cursor = start.replace(minute=0, second=0, microsecond=0)
    i = 0
    while cursor <= end:
        series.append(_RawBar(cursor, 50.0 + (i % 7)))
        cursor += timedelta(hours=1)
        i += 1
    assert len(series) > 120
    client = OldestFirstStockClient(series)
    bars = _market(client).bars("AMAT", "1Hour", 120)

    assert client.requests[0].limit is None
    assert len(bars) == 120
    # 15:00 UTC bucket closes at 16:00 UTC; 14:00 UTC is the newest closed hour.
    assert bars[-1].ts == datetime(2026, 9, 16, 14, 0, tzinfo=UTC)
    truncated = series[:120]
    assert bars[-1].ts > truncated[-1].timestamp


def test_newest_closed_bars_drops_open_daily_and_keeps_right_edge():
    now = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)
    friday = datetime(2026, 9, 18, 4, 0, tzinfo=UTC)
    monday = datetime(2026, 9, 21, 4, 0, tzinfo=UTC)
    bars = [
        Bar(ts=friday - timedelta(days=10), open=1, high=2, low=0.5, close=1, volume=1),
        Bar(ts=friday, open=1, high=2, low=0.5, close=2, volume=1),
        Bar(ts=monday, open=1, high=2, low=0.5, close=3, volume=1),
    ]
    kept = newest_closed_bars(bars, "1Day", limit=1, now=now)
    assert len(kept) == 1
    assert kept[-1].ts == friday
    assert kept[-1].close == 2


def test_stale_detail_fails_closed_on_old_and_empty():
    now = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)
    old = [
        Bar(
            ts=datetime(2026, 6, 21, 4, 0, tzinfo=UTC),
            open=1,
            high=1,
            low=1,
            close=1,
            volume=1,
        )
    ]
    detail = stale_bars_detail(old, "1Day", now)
    assert detail is not None
    assert detail.startswith("1Day:")
    assert "2026-06-21" in detail
    assert stale_bars_detail([], "1Hour", now) == "1Hour:empty"
    fresh = [
        Bar(
            ts=now - timedelta(days=1),
            open=1,
            high=1,
            low=1,
            close=1,
            volume=1,
        )
    ]
    assert stale_bars_detail(fresh, "1Hour", now) is None
    assert STALE_BARS == "stale_bars"


def test_next_bar_close_matches_session_and_clock_hour():
    # Monday 11:00 ET (EDT) → today's 16:00 ET.
    monday_open = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)
    assert next_bar_close("1Day", monday_open) == datetime(2026, 9, 21, 20, 0, tzinfo=UTC)
    # Exactly 16:00 ET is already closed, so the next close is Tuesday.
    at_close = datetime(2026, 9, 21, 20, 0, tzinfo=UTC)
    assert next_bar_close("1Day", at_close) == datetime(2026, 9, 22, 20, 0, tzinfo=UTC)
    # Friday 16:00 ET → Monday.
    friday_close = datetime(2026, 9, 18, 20, 0, tzinfo=UTC)
    assert next_bar_close("1Day", friday_close) == datetime(2026, 9, 21, 20, 0, tzinfo=UTC)
    # Winter: Wednesday 10:00 EST → 16:00 EST = 21:00 UTC.
    winter = datetime(2026, 3, 4, 15, 0, tzinfo=UTC)
    assert next_bar_close("1Day", winter) == datetime(2026, 3, 4, 21, 0, tzinfo=UTC)
    hourly = datetime(2026, 9, 16, 15, 30, tzinfo=UTC)
    assert next_bar_close("1Hour", hourly) == datetime(2026, 9, 16, 16, 0, tzinfo=UTC)
    on_hour = datetime(2026, 9, 16, 16, 0, tzinfo=UTC)
    assert next_bar_close("1Hour", on_hour) == datetime(2026, 9, 16, 17, 0, tzinfo=UTC)


def test_multi_symbol_daily_bars_are_one_request(monkeypatch):
    end = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)
    monkeypatch.setattr("alpaca_options_credit.broker.alpaca._bars_end", lambda: end)
    start = _bars_start("1Day", 60, end)
    series = [_RawBar(ts, 100.0 + i) for i, ts in enumerate(_weekdays(start, end))]
    client = OldestFirstStockClient(series)
    batch = _market(client).bars_for_symbols(["AMAT", "SPY", "QQQ"], "1Day", 60, now=end)

    assert len(client.requests) == 1
    requested = client.requests[0].symbol_or_symbols
    assert set(requested) == {"AMAT", "SPY", "QQQ"}
    assert client.requests[0].limit is None
    assert batch.pages == 1
    assert set(batch.by_symbol) == {"AMAT", "SPY", "QQQ"}
    assert batch.by_symbol["AMAT"][-1].ts.date().isoformat() == "2026-09-18"
    assert batch.by_symbol["SPY"][-1].ts == batch.by_symbol["AMAT"][-1].ts


def test_closed_daily_bars_reused_until_the_next_session_close():
    end = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)  # Monday 11:00 ET
    start = _bars_start("1Day", 60, end)
    series = [_RawBar(ts, 100.0 + i) for i, ts in enumerate(_weekdays(start, end + timedelta(days=1)))]
    client = OldestFirstStockClient(series)
    md = _market(client)
    first = md.bars_for_symbols(["AMAT", "SPY"], "1Day", 60, now=end)
    assert len(client.requests) == 1
    assert first.by_symbol["AMAT"][-1].ts.date().isoformat() == "2026-09-18"

    later = end + timedelta(hours=4)  # 19:00 UTC, still before 20:00 UTC close
    second = md.bars_for_symbols(["AMAT", "SPY"], "1Day", 60, now=later)
    assert len(client.requests) == 1
    assert second.pages == 0
    assert second.by_symbol["AMAT"] == first.by_symbol["AMAT"]

    after_close = datetime(2026, 9, 21, 20, 0, tzinfo=UTC)
    third = md.bars_for_symbols(["AMAT", "SPY"], "1Day", 60, now=after_close)
    assert len(client.requests) == 2
    assert third.pages == 1
    assert third.by_symbol["AMAT"][-1].ts.date().isoformat() == "2026-09-21"
    assert third.by_symbol["SPY"][-1].ts > second.by_symbol["SPY"][-1].ts


def test_closed_hourly_bars_reused_until_the_next_hour():
    end = datetime(2026, 9, 16, 15, 30, tzinfo=UTC)
    start = _bars_start("1Hour", 120, end)
    series: list[_RawBar] = []
    cursor = start.replace(minute=0, second=0, microsecond=0)
    i = 0
    while cursor <= end + timedelta(hours=2):
        series.append(_RawBar(cursor, 50.0 + (i % 7)))
        cursor += timedelta(hours=1)
        i += 1
    client = OldestFirstStockClient(series)
    md = _market(client)
    first = md.bars("AMAT", "1Hour", 120, now=end)
    assert len(client.requests) == 1
    assert first[-1].ts == datetime(2026, 9, 16, 14, 0, tzinfo=UTC)

    second = md.bars("AMAT", "1Hour", 120, now=end + timedelta(minutes=20))
    assert len(client.requests) == 1
    assert second == first

    on_close = datetime(2026, 9, 16, 16, 0, tzinfo=UTC)
    third = md.bars("AMAT", "1Hour", 120, now=on_close)
    assert len(client.requests) == 2
    assert third[-1].ts == datetime(2026, 9, 16, 15, 0, tzinfo=UTC)


def test_empty_or_failed_bars_are_not_cached():
    end = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)
    client = OldestFirstStockClient([])
    md = _market(client)
    assert md.bars("AMAT", "1Day", 60, now=end) == []
    assert md.bars("AMAT", "1Day", 60, now=end) == []
    assert len(client.requests) == 2

    class Boom:
        def __init__(self):
            self.calls = 0

        def get_stock_bars(self, req):
            self.calls += 1
            raise RuntimeError("feed down")

    boom = Boom()
    broken = _market(boom)  # type: ignore[arg-type]
    try:
        broken.bars("AMAT", "1Day", 60, now=end)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the feed error")
    try:
        broken.bars("AMAT", "1Day", 60, now=end)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected a refetch, not a cached failure")
    assert boom.calls == 2


def test_page_count_uses_total_bars_across_symbols():
    end = datetime(2026, 9, 16, 15, 0, tzinfo=UTC)
    series = [_RawBar(end - timedelta(hours=i), 10.0) for i in range(6000)]
    client = OldestFirstStockClient(series)
    batch = _market(client).bars_for_symbols(["AAA", "BBB"], "1Hour", 10_000, now=end)
    assert len(client.requests) == 1
    assert batch.pages == 2

    symbols = [f"S{i}" for i in range(101)]
    wide = OldestFirstStockClient(series[:30])
    chunked = _market(wide).bars_for_symbols(symbols, "1Hour", 120, now=end)
    assert len(wide.requests) == 2
    assert chunked.pages == 2
    assert set(chunked.by_symbol) == set(symbols)


def test_fixture_bars_are_fresh_for_observe():
    now = datetime(2026, 9, 22, 1, 36, tzinfo=UTC)
    bars = bullish_pullback_bars("SPY", as_of=now)
    assert stale_bars_detail(bars, "1Day", now) is None
    assert stale_bars_detail(bars, "1Hour", now) is None
    assert bars[-1].ts == now - timedelta(hours=1)
