"""Study loader and the gap puller."""

from __future__ import annotations

import json
from datetime import date, datetime

import pandas as pd
import pytest

from research.intraday_sr.data.cache import load_symbol, symbol_cache_path, validate_symbol
from research.intraday_sr.data.pull import HttpResponse, pull_gaps
from research.intraday_sr.data.ratelimit import TimeOfDayLimiter
from research.intraday_sr.types import ET


class _Transport:
    def __init__(self, pages: list[dict]):
        self.pages = pages
        self.calls: list[dict] = []

    def get(self, path, params):
        self.calls.append(dict(params))
        page = self.pages.pop(0)
        return HttpResponse(200, json.dumps(page).encode())


def _limiter() -> TimeOfDayLimiter:
    clock = {"m": 0.0}
    return TimeOfDayLimiter(
        mono=lambda: clock["m"],
        sleep=lambda seconds: clock.__setitem__("m", clock["m"] + seconds),
        now=lambda: datetime(2024, 6, 15, 12, 0, tzinfo=ET),
    )


def test_symbol_cache_path_requires_one_part_file(tmp_path):
    nested = tmp_path / "adj_factors"
    nested.mkdir()
    (tmp_path / "part_0003_SPY.parquet").write_bytes(b"")
    (tmp_path / "SPY.parquet").write_bytes(b"")
    (nested / "part_0003_SPY.parquet").write_bytes(b"")
    (tmp_path / "part_0009_BRK-B.parquet").write_bytes(b"")
    assert symbol_cache_path(tmp_path, "SPY").name == "part_0003_SPY.parquet"
    assert symbol_cache_path(tmp_path, "BRK.B").name == "part_0009_BRK-B.parquet"
    with pytest.raises(FileNotFoundError):
        symbol_cache_path(tmp_path, "QQQ")
    (tmp_path / "part_0004_SPY.parquet").write_bytes(b"")
    with pytest.raises(FileNotFoundError):
        symbol_cache_path(tmp_path, "SPY")


def test_load_symbol_reads_a_local_file(tmp_path):
    path = tmp_path / "SPY.parquet"
    pd.DataFrame({"symbol": ["SPY", "QQQ"], "close": [1.0, 2.0]}).to_parquet(path)
    frame = load_symbol(path, "SPY")
    assert list(frame["symbol"]) == ["SPY"]
    assert float(frame["close"].iloc[0]) == 1.0


def test_short_session_is_reported_and_still_valid():
    stamps = pd.date_range("2024-06-12 09:30", periods=10, freq="5min")
    raw = pd.DataFrame(
        {
            "symbol": "SPY",
            "ts": stamps,
            "open": 1.0,
            "high": 1.1,
            "low": 0.9,
            "close": 1.0,
            "volume": 10.0,
        }
    )
    report = validate_symbol(raw, "SPY")
    assert report.ok
    assert report.count_mismatches


def test_pull_pages_uses_rfc3339_and_refuses_the_trading_root(tmp_path):
    trading = tmp_path / "m5rth_fixed33"
    trading.mkdir()
    with pytest.raises(RuntimeError):
        pull_gaps(
            cache_root=trading,
            symbols=["SPY"],
            start=date(2024, 6, 12),
            end=date(2024, 6, 12),
            transport=_Transport([]),
            limiter=_limiter(),
        )
    root = tmp_path / "research-cache"
    transport = _Transport(
        [
            {
                "bars": {
                    "SPY": [
                        {
                            "t": "2024-06-12T13:30:00Z",
                            "o": 1,
                            "h": 1,
                            "l": 1,
                            "c": 1,
                            "v": 1,
                            "n": 1,
                            "vw": 1,
                        }
                    ]
                },
                "next_page_token": "p2",
            },
            {"bars": {"SPY": []}, "next_page_token": None},
        ]
    )
    written = pull_gaps(
        cache_root=root,
        symbols=["SPY"],
        start=date(2024, 6, 12),
        end=date(2024, 6, 12),
        transport=transport,
        limiter=_limiter(),
    )
    assert len(transport.calls) == 2
    assert transport.calls[1]["page_token"] == "p2"
    assert transport.calls[0]["end"].startswith("2024-06-13T00:00:00")
    assert "T" in transport.calls[0]["end"]
    assert written[0].is_file()


def test_page_token_is_not_a_credential_and_a_secret_key_is(tmp_path):
    from research.intraday_sr.data.pull import _reject_secret_params

    _reject_secret_params({"page_token": "p2", "symbols": "SPY"})
    with pytest.raises(ValueError):
        _reject_secret_params({"api_key": "nope"})


def test_a_short_session_is_pulled_again(tmp_path):
    root = tmp_path / "research-cache"
    root.mkdir()
    stamps = pd.date_range("2024-06-12 09:30", periods=10, freq="5min")
    pd.DataFrame(
        {
            "symbol": "SPY",
            "ts": stamps,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
            "trades": 1,
            "vwap": 1.0,
        }
    ).to_parquet(root / "SPY.parquet", index=False)
    transport = _Transport(
        [
            {
                "bars": {
                    "SPY": [
                        {
                            "t": "2024-06-12T13:30:00Z",
                            "o": 2,
                            "h": 2,
                            "l": 2,
                            "c": 2,
                            "v": 2,
                            "n": 2,
                            "vw": 2,
                        }
                    ]
                },
                "next_page_token": None,
            }
        ]
    )
    pull_gaps(
        cache_root=root,
        symbols=["SPY"],
        start=date(2024, 6, 12),
        end=date(2024, 6, 12),
        transport=transport,
        limiter=_limiter(),
    )
    assert len(transport.calls) == 1
    merged = pd.read_parquet(root / "SPY.parquet")
    assert len(merged) == 1
    assert float(merged["close"].iloc[0]) == 2.0
