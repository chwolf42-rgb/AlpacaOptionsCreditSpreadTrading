"""Gap puller for ``/v2/stocks/bars``. Not invoked against Alpaca here.

``transport`` performs the GET. Tests pass a fake. The puller refuses any
path other than ``/v2/stocks/bars``, takes an exclusive lock file, and
appends a request log of timestamp, path, status, and byte count. Query
values that look like credentials are not written.

No API key is read, printed, or logged by this module.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Mapping, Protocol

import pandas as pd

from research.intraday_sr.data.calendar import sessions_between
from research.intraday_sr.data.ratelimit import TimeOfDayLimiter
from research.intraday_sr.types import ET

BARS_PATH = "/v2/stocks/bars"
_SECRET_TOKENS = ("apikey", "api_key", "secret", "authorization", "token")


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


@contextmanager
def pull_lock(cache_root: Path) -> Iterator[Path]:
    """Exclusive lock. The file is removed on the way out, including on error."""
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
    """Append one request. The line cannot carry a credential."""
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
    """Fetch missing sessions for each symbol into ``cache_root/<symbol>.parquet``.

    Existing rows are kept. Only sessions in ``[start, end]`` that are absent
    from the file are requested, as one contiguous range per gap. Returns the
    files that were written.
    """
    root = Path(cache_root)
    log = Path(log_path) if log_path is not None else root / "requests.csv"
    written: list[Path] = []
    with pull_lock(root):
        for symbol in symbols:
            target = root / f"{_file_symbol(symbol)}.parquet"
            have = _sessions_on_disk(target)
            missing = [day for day in sessions_between(start, end) if day not in have]
            if not missing:
                continue
            for gap_start, gap_end in _contiguous(missing):
                params = {
                    "symbols": symbol,
                    "timeframe": "5Min",
                    "start": gap_start.isoformat(),
                    "end": gap_end.isoformat(),
                    "adjustment": "all",
                    "feed": "sip",
                }
                _reject_secret_params(params)
                limiter.acquire()
                response = transport.get(BARS_PATH, params)
                append_request_log(log, path=BARS_PATH, status=response.status, nbytes=len(response.content))
                limiter.note_response(response.status)
                if response.status != 200:
                    raise RuntimeError(f"{BARS_PATH} returned HTTP {response.status}")
                fresh = _parse_bars(response.content, symbol)
                _write_merge(target, fresh)
            written.append(target)
    return written


def _file_symbol(symbol: str) -> str:
    return symbol.replace("/", "-")


def _sessions_on_disk(path: Path) -> set[date]:
    if not path.is_file():
        return set()
    frame = pd.read_parquet(path, columns=["ts"])
    stamps = pd.to_datetime(frame["ts"])
    if getattr(stamps.dt, "tz", None) is not None:
        stamps = stamps.dt.tz_convert(ET)
    return set(stamps.dt.date)

def _contiguous(days: list[date]) -> list[tuple[date, date]]:
    """Group missing sessions that have no kept session between them.

    Friday and the following Monday are one request. A present Wednesday
    between two missing sessions splits the range.
    """
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


def _parse_bars(content: bytes, symbol: str) -> pd.DataFrame:
    """Parse a JSON body ``{"bars": [{t,o,h,l,c,v,n,vw}, ...]}`` into cache columns.

    ``t`` is an ISO timestamp. It is stored as ET-naive bar open, matching
    Trading's parquet. This parser does not speak to Alpaca.
    """
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
    return pd.DataFrame.from_records(records)


def _write_merge(path: Path, fresh: pd.DataFrame) -> None:
    if path.is_file():
        old = pd.read_parquet(path)
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
        if any(token in key.lower() for token in _SECRET_TOKENS):
            raise ValueError("pull parameters must not contain credentials")
