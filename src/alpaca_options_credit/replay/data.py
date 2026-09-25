"""Yahoo chart bars for the replay. Cached under var/ so reruns stay offline.

Prices are the chart's split-adjusted OHLC. The 16:00 empty print Yahoo
appends after the cash close is dropped. Only regular-session bars are kept.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Iterable, Optional

from alpaca_options_credit.models import Bar
from alpaca_options_credit.rth import ET, as_et

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={range}&events=history"

# Hourly history Yahoo will actually return (about Oct 2023 onward).
HOURLY_RANGE = "730d"
DAILY_RANGE = "10y"
MINUTE15_RANGE = "60d"


def fetch_yahoo_bars(symbol: str, interval: str, range_: str) -> list[Bar]:
    url = CHART_URL.format(symbol=symbol, interval=interval, range=range_)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    result = (payload.get("chart") or {}).get("result") or []
    if not result or not result[0] or not result[0].get("timestamp"):
        return []
    block = result[0]
    quote = block["indicators"]["quote"][0]
    bars: list[Bar] = []
    for i, ts in enumerate(block["timestamp"]):
        o = quote["open"][i]
        h = quote["high"][i]
        l = quote["low"][i]
        c = quote["close"][i]
        v = quote["volume"][i]
        if None in (o, h, l, c) or c is None or c <= 0 or h < l:
            continue
        when = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        volume = float(v or 0.0)
        bars.append(
            Bar(
                ts=when,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=volume,
            )
        )
    bars.sort(key=lambda b: b.ts)
    return _regular_session(bars, interval)


def _regular_session(bars: list[Bar], interval: str) -> list[Bar]:
    """Drop the 16:00 stamp and anything outside 09:30–16:00 ET."""
    kept: list[Bar] = []
    seen: set[datetime] = set()
    for bar in bars:
        local = as_et(bar.ts)
        if local.weekday() >= 5:
            continue
        start = local.time()
        if interval in {"1d", "1wk"}:
            if bar.ts in seen:
                continue
            seen.add(bar.ts)
            kept.append(bar)
            continue
        if start < time(9, 30) or start >= time(16, 0):
            continue
        if interval in {"1h", "60m"} and start > time(15, 30):
            continue
        if bar.volume <= 0 and start >= time(15, 45):
            continue
        if bar.ts in seen:
            continue
        seen.add(bar.ts)
        kept.append(bar)
    return kept


def cache_path(cache_dir: Path, symbol: str, interval: str) -> Path:
    return cache_dir / f"{symbol}_{interval}.json"


def save_bars(path: Path, symbol: str, interval: str, bars: list[Bar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": symbol,
        "interval": interval,
        "bars": [
            [b.ts.astimezone(timezone.utc).isoformat(), b.open, b.high, b.low, b.close, b.volume]
            for b in bars
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def load_bars(path: Path) -> list[Bar]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    out: list[Bar] = []
    for row in payload.get("bars") or []:
        ts = datetime.fromisoformat(row[0])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append(Bar(ts=ts, open=row[1], high=row[2], low=row[3], close=row[4], volume=row[5]))
    return out


def load_or_fetch(
    cache_dir: Path,
    symbol: str,
    interval: str,
    range_: str,
    *,
    refresh: bool = False,
) -> list[Bar]:
    path = cache_path(cache_dir, symbol, interval)
    if path.is_file() and not refresh:
        return load_bars(path)
    try:
        bars = fetch_yahoo_bars(symbol, interval, range_)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        if path.is_file():
            return load_bars(path)
        raise
    save_bars(path, symbol, interval, bars)
    return bars


def ensure_universe(
    cache_dir: Path,
    symbols: Iterable[str],
    *,
    refresh: bool = False,
    include_15m: bool = True,
) -> dict[str, dict[str, list[Bar]]]:
    """Download daily, hourly, and (optionally) 15-minute bars for each name."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    jobs: list[tuple[str, str, str]] = []
    for symbol in symbols:
        jobs.append((symbol, "1d", DAILY_RANGE))
        jobs.append((symbol, "1h", HOURLY_RANGE))
        if include_15m:
            jobs.append((symbol, "15m", MINUTE15_RANGE))

    found: dict[tuple[str, str], list[Bar]] = {}

    def _one(job: tuple[str, str, str]) -> tuple[tuple[str, str], list[Bar]]:
        symbol, interval, range_ = job
        bars = load_or_fetch(cache_dir, symbol, interval, range_, refresh=refresh)
        return (symbol, interval), bars

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_one, job) for job in jobs]
        for fut in as_completed(futures):
            key, bars = fut.result()
            found[key] = bars

    out: dict[str, dict[str, list[Bar]]] = {}
    for symbol in symbols:
        out[symbol] = {
            "1d": found.get((symbol, "1d"), []),
            "1h": found.get((symbol, "1h"), []),
            "15m": found.get((symbol, "15m"), []) if include_15m else [],
        }
    return out
