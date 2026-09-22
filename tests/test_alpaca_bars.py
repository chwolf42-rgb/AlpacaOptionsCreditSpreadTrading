"""Alpaca stock bars are oldest-first: `limit` keeps the oldest rows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from alpaca_options_credit.bar_quality import STALE_BARS, newest_closed_bars, stale_bars_detail
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
        symbol = req.symbol_or_symbols
        if isinstance(symbol, list):
            symbol = symbol[0]
        start = _as_utc(req.start)
        end = _as_utc(req.end)
        window = [b for b in self.series if start <= _as_utc(b.timestamp) <= end]
        window.sort(key=lambda b: _as_utc(b.timestamp))
        limit = getattr(req, "limit", None)
        if limit:
            window = window[: int(limit)]
        return SimpleNamespace(data={symbol: window})


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


def test_fixture_bars_are_fresh_for_observe():
    now = datetime(2026, 9, 22, 1, 36, tzinfo=UTC)
    bars = bullish_pullback_bars("SPY", as_of=now)
    assert stale_bars_detail(bars, "1Day", now) is None
    assert stale_bars_detail(bars, "1Hour", now) is None
    assert bars[-1].ts == now - timedelta(hours=1)
