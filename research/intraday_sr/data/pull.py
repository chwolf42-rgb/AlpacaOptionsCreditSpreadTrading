"""Gap puller for ``/v2/stocks/bars``. Not invoked against Alpaca here.

Pages follow ``next_page_token``. ``end`` is the next session's midnight ET,
not a bare date. A partial session is pulled again. Writes under Trading's
``m5rth_fixed33`` tree are refused.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterator, Mapping, Protocol

import pandas as pd
import pyarrow.parquet as pq

from research.intraday_sr.data.cache import expected_bar_count
from research.intraday_sr.data.calendar import next_session, sessions_between
from research.intraday_sr.data.ratelimit import TimeOfDayLimiter
from research.intraday_sr.io import read_bytes
from research.intraday_sr.types import ET

BARS_PATH = "/v2/stocks/bars"
_SECRET_TOKENS = ("apikey", "api_key", "secret", "authorization", "token")
_TRADING_ROOT = "m5rth_fixed33"


class PullLocked(RuntimeError):
    """Another puller holds ``<cache_root>/.pull.lock``."""


@dataclass
class HttpResponse:
    status: int
    content: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def get(self, path: str, params: Mapping[str, str]) -> HttpResponse:
        """Perform one GET. ``params`` must not contain credentials."""


def _refuse_trading_root(root: Path) -> None:
    if _TRADING_ROOT in root.resolve().parts:
        raise RuntimeError(f"refusing to write under {_TRADING_ROOT}")


@contextmanager
def pull_lock(cache_root: Path) -> Iterator[Path]:
    cache_root.mkdir(parents=True, exist_ok=True)
    path = cache_root / ".pull.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        raise PullLocked(str(path)) from exc
    try:
        os.write(fd, f"pid={os.getpid()} at={datetime.now(timezone.utc).isoformat()}\n".encode())
    finally:
        os.close(fd)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def append_request_log(log_path: Path, *, path: str, status: int, nbytes: int) -> None:
    safe = _strip_secrets(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not log_path.exists()
    stamp = datetime.now(timezone.utc).isoformat()
    with log_path.open("a", encoding="utf-8") as handle:
        if new_file:
            handle.write("timestamp,path,status,bytes\n")
        handle.write(f"{stamp},{safe},{int(status)},{int(nbytes)}\n")


def pull_gaps(
    *,
    cache_root: str | Path,
    symbols: list[str],
    start: date,
    end: date,
    transport: Transport,
    limiter: TimeOfDayLimiter,
    log_path: str | Path | None = None,
) -> list[Path]:
    """Fetch missing or partial sessions. Returns the files that were written."""
    root = Path(cache_root)
    _refuse_trading_root(root)
    log = Path(log_path) if log_path is not None else root / "requests.csv"
    written: list[Path] = []
    with pull_lock(root):
        for symbol in symbols:
            target = root / f"{_file_symbol(symbol)}.parquet"
            have = _complete_sessions(target)
            missing = [day for day in sessions_between(start, end) if day not in have]
            if not missing:
                continue
            for gap_start, gap_end in _contiguous(missing):
                fresh = _fetch_gap(symbol, gap_start, gap_end, transport, limiter, log)
                _write_merge(target, fresh)
            written.append(target)
    return written


def _fetch_gap(symbol, gap_start, gap_end, transport, limiter, log) -> pd.DataFrame:
    end_day = next_session(gap_end)
    if end_day is None:
        end_day = gap_end + timedelta(days=1)
    start_at = datetime.combine(gap_start, time(0, 0), tzinfo=ET)
    end_at = datetime.combine(end_day, time(0, 0), tzinfo=ET)
    page = None
    frames: list[pd.DataFrame] = []
    while True:
        params = {
            "symbols": symbol,
            "timeframe": "5Min",
            "start": start_at.isoformat(),
            "end": end_at.isoformat(),
            "adjustment": "all",
            "feed": "sip",
        }
        if page:
            params["page_token"] = page
        _reject_secret_params(params)
        limiter.acquire()
        response = transport.get(BARS_PATH, params)
        append_request_log(log, path=BARS_PATH, status=response.status, nbytes=len(response.content))
        limiter.note_response(response.status)
        if response.status != 200:
            raise RuntimeError(f"{BARS_PATH} returned HTTP {response.status}")
        frame, page = _parse_bars(response.content, symbol)
        if not frame.empty:
            frames.append(frame)
        if not page:
            break
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _file_symbol(symbol: str) -> str:
    return "BRK-B" if symbol == "BRK.B" else symbol.replace("/", "-")


def _complete_sessions(path: Path) -> set[date]:
    """Sessions that already have every RTH bar. A short session is not complete."""
    if not path.is_file():
        return set()
    frame = pq.read_table(io.BytesIO(read_bytes(path)), columns=["ts"]).to_pandas()
    stamps = pd.to_datetime(frame["ts"])
    if getattr(stamps.dt, "tz", None) is not None:
        stamps = stamps.dt.tz_convert(ET)
    else:
        stamps = stamps.dt.tz_localize(ET)
    counts = stamps.dt.date.value_counts()
    done: set[date] = set()
    for day, actual in counts.items():
        expected = expected_bar_count(day)
        if expected is not None and int(actual) == expected:
            done.add(day)
    return done


def _contiguous(days: list[date]) -> list[tuple[date, date]]:
    if not days:
        return []
    ordered = sorted(days)
    ranges: list[tuple[date, date]] = []
    start = prev = ordered[0]
    for day in ordered[1:]:
        between = sessions_between(prev + timedelta(days=1), day - timedelta(days=1))
        if not between:
            prev = day
            continue
        ranges.append((start, prev))
        start = prev = day
    ranges.append((start, prev))
    return ranges


def _parse_bars(content: bytes, symbol: str) -> tuple[pd.DataFrame, str | None]:
    payload = json.loads(content.decode("utf-8"))
    rows = payload.get("bars") or []
    if isinstance(rows, dict):
        rows = rows.get(symbol) or []
    records = []
    for row in rows:
        stamp = pd.Timestamp(row["t"])
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        stamp = stamp.tz_convert(ET).tz_localize(None)
        records.append(
            {
                "ts": stamp,
                "symbol": symbol,
                "open": row["o"],
                "high": row["h"],
                "low": row["l"],
                "close": row["c"],
                "volume": row.get("v", 0),
                "trades": row.get("n", 0),
                "vwap": row.get("vw", row["c"]),
            }
        )
    token = payload.get("next_page_token") or None
    return pd.DataFrame.from_records(records), token


def _write_merge(path: Path, fresh: pd.DataFrame) -> None:
    _refuse_trading_root(path.parent)
    if path.is_file() and not fresh.empty:
        old = pq.read_table(io.BytesIO(read_bytes(path))).to_pandas()
        fresh_days = set(pd.to_datetime(fresh["ts"]).dt.date)
        old_days = pd.to_datetime(old["ts"]).dt.date
        old = old.loc[~old_days.isin(fresh_days)]
        combined = pd.concat([old, fresh], ignore_index=True)
    else:
        combined = fresh
    if combined.empty:
        combined.to_parquet(path, index=False)
        return
    combined = combined.drop_duplicates(subset=["symbol", "ts"], keep="last")
    combined = combined.sort_values("ts")
    combined.to_parquet(path, index=False)


def _strip_secrets(path: str) -> str:
    base = path.split("?", 1)[0]
    lowered = base.lower()
    for token in _SECRET_TOKENS:
        if token in lowered:
            return BARS_PATH
    return base


def _reject_secret_params(params: Mapping[str, str]) -> None:
    for key in params:
        lowered = key.lower()
        if lowered == "page_token":
            continue
        if any(token in lowered for token in _SECRET_TOKENS):
            raise ValueError("pull parameters must not contain credentials")
