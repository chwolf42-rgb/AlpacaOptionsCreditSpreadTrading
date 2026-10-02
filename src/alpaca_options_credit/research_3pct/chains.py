"""Listed-option credit/width from CBOE delayed quotes and Yahoo daily bars.

CBOE is one delayed cross-section (bid, ask, and the exchange delta). Yahoo
daily bars are last trades of contracts that are still listed. Expired
contracts are not on that chart endpoint. Neither source is a historical
NBBO. The summary says which one each number came from.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

UA = "Mozilla/5.0 (compatible; research-3pct/1.0)"
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1y"

CHAIN_SYMBOLS = (
    "SPY",
    "QQQ",
    "IWM",
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "META",
    "GOOGL",
    "TSLA",
    "PFE",
    "T",
    "KRE",
    "XLF",
)
INDEX = frozenset({"SPY", "QQQ", "IWM"})
# Historical last-trade panel. Two expirations, three deltas, the $5 wing.
HIST_UNDERLYINGS = ("SPY", "QQQ", "IWM", "AAPL", "MSFT")
HIST_DELTAS = (0.16, 0.30, 0.40)
WIDTHS = (5.0, 10.0)


def _get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def parse_option_symbol(symbol: str) -> Optional[tuple[date, str, float]]:
    """CBOE/OCC symbol without a space-padded root: ROOT + YYMMDD + C/P + strike*1000."""
    i = 0
    while i < len(symbol) and symbol[i].isalpha():
        i += 1
    rest = symbol[i:]
    if len(rest) < 7 or rest[6] not in {"C", "P"}:
        return None
    try:
        yy = int(rest[0:2])
        mm = int(rest[2:4])
        dd = int(rest[4:6])
        strike = int(rest[7:]) / 1000.0
        expiration = date(2000 + yy, mm, dd)
    except ValueError:
        return None
    right = "call" if rest[6] == "C" else "put"
    return expiration, right, strike


def load_cboe(cache: Path, symbol: str) -> Optional[dict]:
    path = cache / f"cboe_{symbol}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        payload = _get(CBOE_URL.format(symbol=symbol))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _dte_bucket(dte: int) -> Optional[str]:
    if 7 <= dte <= 14:
        return "7-14"
    if 21 <= dte <= 30:
        return "21-30"
    if 30 < dte <= 45:
        return "30-45"
    return None


def _delta_bucket(delta: float) -> Optional[float]:
    for target in (0.10, 0.16, 0.20, 0.25, 0.30, 0.40):
        if abs(delta - target) <= 0.02:
            return target
    return None


def verticals_from_chain(payload: dict, *, asof: date) -> list[dict]:
    """Every two-sided vertical of width 5 and 10 on this chain."""
    data = payload.get("data") or {}
    spot = float(data.get("current_price") or 0.0)
    by_key: dict[tuple, dict] = {}
    for row in data.get("options") or []:
        parsed = parse_option_symbol(str(row.get("option") or ""))
        if parsed is None:
            continue
        expiration, right, strike = parsed
        by_key[(expiration, right, round(strike, 2))] = row
    out: list[dict] = []
    for (expiration, right, strike), short in by_key.items():
        dte = (expiration - asof).days
        bucket = _dte_bucket(dte)
        if bucket is None:
            continue
        delta = abs(float(short.get("delta") or 0.0))
        delta_name = _delta_bucket(delta)
        if delta_name is None:
            continue
        short_bid = float(short.get("bid") or 0.0)
        short_ask = float(short.get("ask") or 0.0)
        if short_bid <= 0 or short_ask <= 0 or short_bid > short_ask:
            continue
        for width in WIDTHS:
            long_k = round(strike - width, 2) if right == "put" else round(strike + width, 2)
            long = by_key.get((expiration, right, long_k))
            if long is None:
                continue
            long_bid = float(long.get("bid") or 0.0)
            long_ask = float(long.get("ask") or 0.0)
            if long_bid <= 0 or long_ask <= 0 or long_bid > long_ask:
                continue
            natural = short_bid - long_ask
            mid = ((short_bid + short_ask) / 2.0) - ((long_bid + long_ask) / 2.0)
            out.append(
                {
                    "spot": spot,
                    "right": right,
                    "dte": dte,
                    "dte_bucket": bucket,
                    "delta": delta,
                    "delta_bucket": delta_name,
                    "width": width,
                    "natural": natural,
                    "mid": mid,
                    "natural_frac": natural / width,
                    "mid_frac": mid / width,
                    "clears_20": natural / width + 1e-12 >= 0.20,
                    "oi": float(short.get("open_interest") or 0.0),
                }
            )
    return out


def summarize_verticals(rows: list[dict], *, group: str) -> list[dict]:
    buckets: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row["delta_bucket"], row["dte_bucket"], row["width"])
        buckets.setdefault(key, []).append(row)
    out = []
    for key in sorted(buckets):
        group_rows = buckets[key]
        fracs = sorted(row["natural_frac"] for row in group_rows)
        mids = sorted(row["mid_frac"] for row in group_rows)
        clears = sum(1 for row in group_rows if row["clears_20"])
        out.append(
            {
                "group": group,
                "delta": key[0],
                "dte": key[1],
                "width": key[2],
                "n": len(group_rows),
                "median_natural_frac": fracs[len(fracs) // 2],
                "median_mid_frac": mids[len(mids) // 2],
                "pct_clear_20": clears / len(group_rows),
            }
        )
    return out


def snapshot_summary(cache: Path, *, asof: Optional[date] = None) -> dict:
    """Download (or read) the CBOE chains and summarize credit/width."""
    asof = asof or date.today()
    index_rows: list[dict] = []
    single_rows: list[dict] = []
    symbols_ok: list[str] = []
    symbols_miss: list[str] = []
    spots: dict[str, float] = {}
    for symbol in CHAIN_SYMBOLS:
        payload = load_cboe(cache, symbol)
        if not payload or not (payload.get("data") or {}).get("options"):
            symbols_miss.append(symbol)
            continue
        symbols_ok.append(symbol)
        spots[symbol] = float((payload.get("data") or {}).get("current_price") or 0.0)
        rows = verticals_from_chain(payload, asof=asof)
        if symbol in INDEX:
            index_rows.extend(rows)
        else:
            single_rows.extend(rows)
    return {
        "asof": asof.isoformat(),
        "source": "CBOE delayed quotes (bid/ask and exchange delta), one cross-section",
        "symbols_ok": symbols_ok,
        "symbols_miss": symbols_miss,
        "spots": spots,
        "index": summarize_verticals(index_rows, group="SPY/QQQ/IWM"),
        "singles": summarize_verticals(single_rows, group="single names"),
        "index_n": len(index_rows),
        "singles_n": len(single_rows),
    }


def _yahoo_daily(cache: Path, symbol: str) -> list[tuple[date, float, float]]:
    path = cache / f"yahoo_{symbol}.json"
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        try:
            payload = _get(YAHOO_CHART.format(symbol=symbol))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return []
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    result = (payload.get("chart") or {}).get("result") or []
    if not result or not result[0].get("timestamp"):
        return []
    quote = result[0]["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(result[0]["timestamp"]):
        close = quote["close"][i]
        volume = quote["volume"][i]
        if close is None or close <= 0:
            continue
        day = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
        out.append((day, float(close), float(volume or 0.0)))
    return out


def _pick_contract(rows: list[dict], expiration: date, right: str, target: float) -> Optional[dict]:
    best = None
    best_gap = None
    for row in rows:
        parsed = parse_option_symbol(str(row.get("option") or ""))
        if parsed is None:
            continue
        exp, side, strike = parsed
        if exp != expiration or side != right:
            continue
        delta = abs(float(row.get("delta") or 0.0))
        if delta <= 0:
            continue
        gap = abs(delta - target)
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best = row
    if best is None or best_gap is None or best_gap > 0.03:
        return None
    return best


def _expirations(payload: dict, asof: date) -> list[date]:
    found = set()
    for row in (payload.get("data") or {}).get("options") or []:
        parsed = parse_option_symbol(str(row.get("option") or ""))
        if parsed is not None:
            found.add(parsed[0])
    return sorted(exp for exp in found if exp >= asof)


def historical_panel(cache: Path, *, asof: Optional[date] = None) -> dict:
    """Last-trade credit/width on still-listed contracts, joined to underlying days.

    Delta on each past day is not the exchange delta (that is only known
    today). The day is bucketed with the short's *current* exchange delta,
    which is the strike we meant to sample, and the report says so. DTE is
    the true calendar DTE on that day.
    """
    asof = asof or date.today()
    samples: list[dict] = []
    downloaded = 0
    for symbol in HIST_UNDERLYINGS:
        payload = load_cboe(cache, symbol)
        if not payload:
            continue
        options = (payload.get("data") or {}).get("options") or []
        exps = _expirations(payload, asof)
        wanted: list[date] = []
        near = [exp for exp in exps if 21 <= (exp - asof).days <= 45]
        far = [exp for exp in exps if 60 <= (exp - asof).days <= 130]
        if near:
            wanted.append(near[0])
        if far:
            wanted.append(far[0])
        rights = ("put", "call") if symbol in {"SPY", "QQQ"} else ("put",)
        for expiration in wanted:
            for right in rights:
                for target in HIST_DELTAS:
                    short = _pick_contract(options, expiration, right, target)
                    if short is None:
                        continue
                    parsed = parse_option_symbol(str(short["option"]))
                    if parsed is None:
                        continue
                    _exp, _right, strike = parsed
                    long_k = strike - 5.0 if right == "put" else strike + 5.0
                    long_sym = _format_symbol(symbol, expiration, right, long_k)
                    # The long has to be a listed strike. Find it.
                    long_row = None
                    for row in options:
                        got = parse_option_symbol(str(row.get("option") or ""))
                        if got is None:
                            continue
                        if got == (expiration, right, round(long_k, 2)) or (
                            got[0] == expiration and got[1] == right and abs(got[2] - long_k) < 0.01
                        ):
                            long_row = row
                            long_sym = str(row["option"])
                            break
                    if long_row is None:
                        continue
                    short_bars = _yahoo_daily(cache, str(short["option"]))
                    long_bars = _yahoo_daily(cache, long_sym)
                    downloaded += 2
                    long_by = {day: (close, vol) for day, close, vol in long_bars}
                    for day, s_close, s_vol in short_bars:
                        other = long_by.get(day)
                        if other is None or s_vol <= 0 or other[1] <= 0:
                            continue
                        dte = (expiration - day).days
                        bucket = _dte_bucket(dte)
                        if bucket is None:
                            continue
                        credit = s_close - other[0]
                        samples.append(
                            {
                                "symbol": symbol,
                                "dte_bucket": bucket,
                                "delta_bucket": target,
                                "credit_frac": credit / 5.0,
                                "clears_20": credit / 5.0 + 1e-12 >= 0.20,
                            }
                        )
    return {
        "source": (
            "Yahoo daily last trades of contracts still listed on the CBOE chain. "
            "Short and long closes are not a simultaneous print. Delta bucket is the "
            "strike's exchange delta on the snapshot day, not the delta on the historical day. "
            "Expired contracts are absent."
        ),
        "n": len(samples),
        "chart_requests": downloaded,
        "rows": _hist_summary(samples),
    }


def _format_symbol(root: str, expiration: date, right: str, strike: float) -> str:
    cp = "C" if right == "call" else "P"
    return f"{root}{expiration.strftime('%y%m%d')}{cp}{int(round(strike * 1000)):08d}"


def _hist_summary(samples: list[dict]) -> list[dict]:
    buckets: dict[tuple, list[dict]] = {}
    for row in samples:
        key = (row["delta_bucket"], row["dte_bucket"])
        buckets.setdefault(key, []).append(row)
    out = []
    for key in sorted(buckets):
        group = buckets[key]
        fracs = sorted(row["credit_frac"] for row in group)
        clears = sum(1 for row in group if row["clears_20"])
        out.append(
            {
                "delta": key[0],
                "dte": key[1],
                "width": 5.0,
                "n": len(group),
                "median_last_frac": fracs[len(fracs) // 2],
                "pct_clear_20": clears / len(group),
            }
        )
    return out


def collect(cache: Path, *, asof: Optional[date] = None) -> dict:
    cache.mkdir(parents=True, exist_ok=True)
    snap = snapshot_summary(cache, asof=asof)
    hist = historical_panel(cache, asof=asof or date.fromisoformat(snap["asof"]))
    return {"snapshot": snap, "history": hist}
