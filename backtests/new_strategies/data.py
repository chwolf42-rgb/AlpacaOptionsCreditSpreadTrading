"""Free daily history. Yahoo chart API for underlyings, FRED for vol indexes.

No Alpaca keys. Prices are split-adjusted and not dividend-adjusted, so a
split does not look like a crash and dividends are not counted twice
(the pricer applies a constant dividend yield).

Cache lives under docs/research/new_strategies_options/data so a rerun
does not need the network. Tests build synthetic markets and do not read
this cache.
"""

from __future__ import annotations

import csv
import json
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from backtests.new_strategies.specs import UNDERLYINGS, WARMUP_START

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "docs" / "research" / "new_strategies_options" / "data"

# VIX close is the FRED VIXCLS series (already a long public history).
# The 9-day, 3-month, Nasdaq, and Russell indexes are CBOE's own daily
# history files. Yahoo does not publish RVX. The rate is the 13-week
# T-bill (^IRX) when FRED DGS3MO is unreachable.
FRED_SERIES = {"vix": "VIXCLS", "rate": "DGS3MO"}
CBOE_SERIES = {
    "vix9d": "VIX9D_History.csv",
    "vix3m": "VIX3M_History.csv",
    "vxn": "VXN_History.csv",
    "rvx": "RVX_History.csv",
}

YAHOO_UA = "Mozilla/5.0 (compatible; research-backtest/1.0)"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": YAHOO_UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _parse_date(text: str) -> date | None:
    text = text.strip()
    if not text or text == ".":
        return None
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def download_yahoo(symbol: str, start: date, end: date) -> list[dict]:
    """Split-adjusted daily OHLC. Dividends are left in the price level."""
    period1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    period2 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{symbol}?period1={period1}&period2={period2}&interval=1d&events=splits"
    )
    payload = json.loads(_get(url))
    result = payload["chart"]["result"][0]
    stamps = result.get("timestamp") or []
    quote = result["indicators"]["quote"][0]
    opens = quote["open"]
    highs = quote["high"]
    lows = quote["low"]
    closes = quote["close"]
    # Chart `close` is already split-adjusted and is not the dividend-adjusted
    # `adjclose`. Applying the split events again would divide history twice.
    rows: list[dict] = []
    for i, stamp in enumerate(stamps):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if o is None or h is None or l is None or c is None:
            continue
        rows.append(
            {
                "date": datetime.fromtimestamp(int(stamp), timezone.utc).date().isoformat(),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
            }
        )
    rows.sort(key=lambda r: r["date"])
    return rows


def download_cboe(filename: str) -> list[tuple[str, float]]:
    url = f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{filename}"
    text = _get(url).decode("utf-8", errors="replace")
    out: list[tuple[str, float]] = []
    reader = csv.DictReader(text.splitlines())
    for row in reader:
        raw_date = (row.get("DATE") or row.get("Date") or "").strip()
        raw_close = (row.get("CLOSE") or row.get("Close") or "").strip()
        if not raw_date or not raw_close:
            continue
        day = datetime.strptime(raw_date, "%m/%d/%Y").date()
        out.append((day.isoformat(), float(raw_close)))
    return out


def download_yahoo_close(symbol: str, start: date, end: date) -> list[tuple[str, float]]:
    period1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    period2 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.request.quote(symbol)}?period1={period1}&period2={period2}&interval=1d"
    )
    payload = json.loads(_get(url))
    result = payload["chart"]["result"][0]
    stamps = result.get("timestamp") or []
    closes = result["indicators"]["quote"][0]["close"]
    out: list[tuple[str, float]] = []
    for stamp, close in zip(stamps, closes):
        if close is None:
            continue
        day = datetime.fromtimestamp(int(stamp), timezone.utc).date().isoformat()
        out.append((day, float(close)))
    return out


def download_fred(series_id: str) -> list[tuple[str, float]]:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    text = _get(url).decode("utf-8", errors="replace")
    out: list[tuple[str, float]] = []
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        return out
    date_key = reader.fieldnames[0]
    value_key = reader.fieldnames[1]
    for row in reader:
        d = _parse_date(row.get(date_key) or "")
        raw = (row.get(value_key) or "").strip()
        if d is None or raw in {"", "."}:
            continue
        out.append((d.isoformat(), float(raw)))
    return out


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def ensure_cache(refresh: bool = False) -> Path:
    """Download anything missing. Returns the cache directory."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    end = date(2026, 10, 4)
    for symbol in UNDERLYINGS:
        path = DATA_DIR / f"{symbol}.csv"
        if path.exists() and not refresh:
            continue
        rows = download_yahoo(symbol, WARMUP_START, end)
        _write_csv(path, rows, ["date", "open", "high", "low", "close"])
    for name, series_id in FRED_SERIES.items():
        path = DATA_DIR / f"{name}.csv"
        if path.exists() and not refresh:
            continue
        try:
            pairs = download_fred(series_id)
        except Exception:
            if name != "rate":
                raise
            pairs = download_yahoo_close("^IRX", WARMUP_START, end)
        rows = [{"date": d, "value": v} for d, v in pairs]
        _write_csv(path, rows, ["date", "value"])
    for name, filename in CBOE_SERIES.items():
        path = DATA_DIR / f"{name}.csv"
        if path.exists() and not refresh:
            continue
        rows = [{"date": d, "value": v} for d, v in download_cboe(filename)]
        _write_csv(path, rows, ["date", "value"])
    return DATA_DIR


def _read_bars(path: Path) -> list[dict]:
    with path.open() as fh:
        return list(csv.DictReader(fh))


def load_market(data_dir: Path | None = None) -> dict:
    """In-memory market. Vol indexes are floats; rate is a decimal yield."""
    folder = data_dir or DATA_DIR
    underlyings = {}
    for symbol in UNDERLYINGS:
        rows = _read_bars(folder / f"{symbol}.csv")
        underlyings[symbol] = [
            {
                "date": datetime.strptime(r["date"], "%Y-%m-%d").date(),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
            }
            for r in rows
            if r.get("open") and r.get("close")
        ]
    series = {}
    for name in ("vix", "vix9d", "vix3m", "vxn", "rvx", "rate"):
        rows = _read_bars(folder / f"{name}.csv")
        parsed = []
        for r in rows:
            if not r.get("value"):
                continue
            parsed.append(
                (datetime.strptime(r["date"], "%Y-%m-%d").date(), float(r["value"]))
            )
        series[name] = parsed
    return {"underlyings": underlyings, "series": series}
