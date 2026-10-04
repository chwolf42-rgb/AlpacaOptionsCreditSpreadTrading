"""Cache cleaning and the gap puller. The puller is exercised with a fake transport only."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from research.intraday_sr.data.cache import (
    SymbolValidationError,
    cache_filename,
    expected_bar_count,
    load_universe_bars,
    normalize_bars,
    symbol_cache_path,
    symbol_from_cache_name,
    validate_symbol,
)
from research.intraday_sr.data.calendar import last_bar_open
from research.intraday_sr.data.pull import (
    BARS_PATH,
    HttpResponse,
    PullLocked,
    pull_gaps,
    pull_lock,
)
from research.intraday_sr.data.ratelimit import TimeOfDayLimiter
from research.intraday_sr.types import ET


def _bar(ts: datetime, close: float, *, high: float | None = None, low: float | None = None, symbol="TEST"):
    return {
        "ts": ts,
        "symbol": symbol,
        "open": close,
        "high": close if high is None else high,
        "low": close if low is None else low,
        "close": close,
        "volume": 1000.0,
        "trades": 10,
        "vwap": close,
    }


def _rth(day: date, minutes: int, price: float) -> dict:
    ts = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET) + timedelta(minutes=minutes)
    return _bar(ts, price)


def test_rth_filter_bad_bars_and_no_forward_fill():
    day = date(2024, 6, 3)
    rows = [
        _bar(datetime(2024, 6, 3, 8, 0, tzinfo=ET), 10.0),  # premarket
        _bar(datetime(2024, 6, 3, 16, 0, tzinfo=ET), 10.0),  # auction / after the last RTH open
        _rth(day, 0, 10.0),
        _rth(day, 5, 10.1),
        # 09:40 is missing on purpose
        _rth(day, 15, 10.2),
        _bar(
            datetime(2024, 6, 3, 9, 50, tzinfo=ET),
            10.0,
            high=9.0,
            low=11.0,
        ),
        _bar(datetime(2024, 6, 3, 9, 55, tzinfo=ET), -1.0),
    ]
    # Enough quiet bars for ATR, then a spike, then a normal bar.
    quiet_day = date(2024, 6, 4)
    for i in range(20):
        rows.append(_rth(quiet_day, 5 * i, 20.0 + 0.01 * i))
    rows.append(_rth(quiet_day, 5 * 20, 80.0))
    rows.append(_rth(quiet_day, 5 * 21, 20.3))
    frame, report = normalize_bars(pd.DataFrame(rows), start=date(2024, 6, 3), end=date(2024, 6, 4))
    assert report.off_session == 2
    assert report.high_below_low == 1
    assert report.nonpositive == 1
    assert report.spike == 1
    june3 = frame[frame["session"] == day]
    # 09:30, 09:35, 09:45 kept. 09:40 was never invented.
    opens = [ts.tz_convert(ET).strftime("%H:%M") for ts in june3["ts"]]
    assert opens == ["09:30", "09:35", "09:45"]
    assert list(june3["available_at"].dt.tz_convert(ET).dt.strftime("%H:%M")) == ["09:35", "09:40", "09:50"]
    assert 80.0 not in set(frame["close"].astype(float))
    assert float(frame.iloc[-1]["close"]) == pytest.approx(20.3)


def test_early_close_drops_the_afternoon():
    rows = []
    for minutes in range(0, 400, 5):
        rows.append(_rth(date(2024, 7, 3), minutes, 50.0))
    frame, _ = normalize_bars(pd.DataFrame(rows), start=date(2024, 7, 3), end=date(2024, 7, 3))
    assert len(frame) == 42
    last = frame["ts"].iloc[-1].tz_convert(ET)
    assert last.hour == 12 and last.minute == 55


def _complete_session(day: date, symbol: str = "SPY") -> list[dict]:
    last = last_bar_open(day)
    assert last is not None
    rows = []
    ts = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET)
    end = datetime(day.year, day.month, day.day, last.hour, last.minute, tzinfo=ET)
    price = 10.0
    while ts <= end:
        rows.append(_bar(ts, price, symbol=symbol))
        ts += timedelta(minutes=5)
        price += 0.01
    return rows


def test_validate_symbol_accepts_a_full_session_and_an_early_close():
    full = date(2024, 6, 3)
    early = date(2024, 7, 3)
    assert expected_bar_count(full) == 78
    assert expected_bar_count(early) == 42
    full_rows = _complete_session(full, "ADBE")
    early_rows = _complete_session(early, "ADBE")
    assert len(full_rows) == 78 and len(early_rows) == 42
    ok = validate_symbol(pd.DataFrame(full_rows + early_rows), "ADBE")
    assert ok.ok
    assert ok.rows == 120


def test_validate_symbol_rejects_duplicates_rth_bounds_bad_bars_and_short_sessions():
    day = date(2024, 6, 3)
    rows = _complete_session(day, "SPY")
    dup = pd.DataFrame(rows + [rows[0]])
    assert not validate_symbol(dup, "SPY").ok
    assert validate_symbol(dup, "SPY").duplicate_timestamps == 1

    late = rows + [_bar(datetime(2024, 6, 3, 16, 0, tzinfo=ET), 11.0, symbol="SPY")]
    outside = validate_symbol(pd.DataFrame(late), "SPY")
    assert outside.outside_rth == 1
    assert outside.count_mismatches

    bad = list(rows)
    bad[3] = _bar(bad[3]["ts"], 10.0, high=9.0, low=11.0, symbol="SPY")
    broken = validate_symbol(pd.DataFrame(bad), "SPY")
    assert broken.bad_bars == 1
    assert not broken.ok

    short = validate_symbol(pd.DataFrame(rows[:-1]), "SPY")
    assert short.count_mismatches == (("2024-06-03", 77, 78),)


def test_cache_filename_maps_brk_b_and_blocks_a_bad_symbol_before_normalize(tmp_path: Path, monkeypatch):
    assert cache_filename("BRK.B") == "BRK-B"
    assert symbol_from_cache_name("BRK-B.parquet") == "BRK.B"
    assert symbol_from_cache_name("FB") == "META"
    day = date(2024, 6, 3)
    good = pd.DataFrame(_complete_session(day, "SPY"))
    bad_rows = _complete_session(day, "QQQ")[:-1]
    good.to_parquet(symbol_cache_path(tmp_path, "SPY"))
    pd.DataFrame(bad_rows).to_parquet(symbol_cache_path(tmp_path, "QQQ"))
    calls: list[str] = []
    real = normalize_bars

    def _wrapped(*args, **kwargs):
        calls.append("normalize")
        return real(*args, **kwargs)

    monkeypatch.setattr("research.intraday_sr.data.cache.normalize_bars", _wrapped)
    with pytest.raises(SymbolValidationError) as caught:
        load_universe_bars(tmp_path, symbols=("SPY", "QQQ"), start=day, end=day)
    assert "QQQ" in str(caught.value)
    assert calls == []


def test_symbol_aliases():
    ts = datetime(2024, 6, 3, 9, 30, tzinfo=ET)
    raw = pd.DataFrame([_bar(ts, 1.0, symbol="BRK-B"), _bar(ts, 2.0, symbol="FB")])
    frame, _ = normalize_bars(raw, start=date(2024, 6, 3), end=date(2024, 6, 3))
    assert set(frame["symbol"]) == {"BRK.B", "META"}


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get(self, path: str, params: dict) -> HttpResponse:
        self.calls.append((path, dict(params)))
        assert path == BARS_PATH
        body = {
            "bars": [
                {
                    "t": "2024-06-03T13:30:00Z",
                    "o": 10,
                    "h": 11,
                    "l": 9,
                    "c": 10.5,
                    "v": 100,
                    "n": 4,
                    "vw": 10.2,
                }
            ]
        }
        return HttpResponse(status=200, content=json.dumps(body).encode())


class _Clock:
    def __init__(self) -> None:
        self.mono = 0.0
        self.moment = datetime(2026, 10, 3, 22, 0, tzinfo=ET)  # Saturday evening CT-ish

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.mono += float(seconds)

    def now(self) -> datetime:
        return self.moment


def test_pull_gaps_logs_without_secrets_and_skips_a_second_pass(tmp_path: Path):
    transport = _FakeTransport()
    clock = _Clock()
    limiter = TimeOfDayLimiter(mono=clock.monotonic, sleep=clock.sleep, now=clock.now)
    written = pull_gaps(
        cache_root=tmp_path,
        symbols=["TEST"],
        start=date(2024, 6, 3),
        end=date(2024, 6, 3),
        transport=transport,
        limiter=limiter,
    )
    assert written and written[0].is_file()
    log = (tmp_path / "requests.csv").read_text(encoding="utf-8")
    assert "secret" not in log.lower()
    assert "apikey" not in log.lower()
    assert BARS_PATH in log
    assert transport.calls[0][0] == BARS_PATH
    assert "apikey" not in transport.calls[0][1]
    transport.calls.clear()
    again = pull_gaps(
        cache_root=tmp_path,
        symbols=["TEST"],
        start=date(2024, 6, 3),
        end=date(2024, 6, 3),
        transport=transport,
        limiter=limiter,
    )
    assert again == []
    assert transport.calls == []


def test_lock_is_exclusive(tmp_path: Path):
    with pull_lock(tmp_path):
        with pytest.raises(PullLocked):
            with pull_lock(tmp_path):
                pass
    assert not (tmp_path / ".pull.lock").exists()


def test_adbe_file_is_rth_only_when_present():
    candidates = [
        Path("/tmp/adbe_5m/part_0001.parquet"),
        Path("/home/ubuntu/.cursor/projects/workspace/uploads/adbe_5m.tar_795a.gz"),
    ]
    parquet = next((path for path in candidates if path.suffix == ".parquet" and path.is_file()), None)
    if parquet is None:
        pytest.skip("ADBE parquet is not on this machine")
    import time

    import numpy as np

    started = time.perf_counter()
    frame, report = normalize_bars(pd.read_parquet(parquet), symbol="ADBE")
    elapsed = time.perf_counter() - started
    assert report.kept == len(frame)
    assert frame["ts"].min().tz_convert(ET).date() >= date(2019, 1, 2)
    opens = frame["ts"].dt.tz_convert(ET)
    assert (opens.dt.time >= pd.Timestamp("09:30").time()).all()
    assert (opens.dt.time <= pd.Timestamp("15:55").time()).all()
    early = frame[frame["session"] == date(2024, 7, 3)]
    assert len(early) > 0
    last = early["ts"].iloc[-1].tz_convert(ET)
    assert (last.hour, last.minute) <= (12, 55)
    full = frame[frame["session"] == date(2024, 6, 3)]
    assert len(full) == 78
    assert full["ts"].iloc[-1].tz_convert(ET).strftime("%H:%M") == "15:55"
    # No forward-filled holes: a duplicated open would mean we invented a bar.
    assert not frame.duplicated(subset=["symbol", "ts"]).any()
    nbytes = frame[["open", "high", "low", "close", "vwap"]].to_numpy(dtype=np.float32).nbytes
    print(f"ADBE normalize {elapsed:.2f}s rows={len(frame)} price_bytes={nbytes} dropped_spike={report.spike}")
