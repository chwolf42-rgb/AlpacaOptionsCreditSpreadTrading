"""Compare modeled spread marks with the hourly close and with market prints.

The replay stops on the Black-Scholes mid of the underlying bar. That mid
has no NBBO flicker: the half-spread is a smooth function of the mid, and
the natural debit (short ask − long bid) is always at least the mid. A stop
on the mid is therefore later, not earlier, than a stop on the natural debit.

What the model can fake is the path inside the hour. ``classify_stop`` splits
credit stops into a close that itself is through the stop, an open that gaps
through and then recovers, and a wick that neither the open nor the close
confirms.

Market prints are a second check. Alpaca historical option quotes are used
when OPTIONS_APCA_* (or inherited APCA_*) keys are present. Without them,
Yahoo daily option bars are the available traded prices for contracts that
are still listed. Expired contracts are not on that chart. Yahoo's daily
bar is a last trade, not an NBBO, so bid/ask width is reported only from
Alpaca quotes.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

from alpaca_options_credit.replay.stats import ReplayTrade
from alpaca_options_credit.rth import as_et
from alpaca_options_credit.strategy.spreads import stop_hit

YAHOO_CHART = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?interval=1d&period1={start}&period2={end}"
)


@dataclass(frozen=True)
class StopCensus:
    n_stops: int
    close_confirmed: int
    gap_recovered: int
    wick_only: int
    # Close mid is inside the stop, but paying the bid/ask is already through it.
    close_mid_inside_natural_through: int

    @property
    def wick_share(self) -> float:
        if self.n_stops <= 0:
            return 0.0
        return self.wick_only / self.n_stops


def classify_stop(trade: ReplayTrade, *, stop_mult: float = 1.5) -> str:
    """``close``, ``gap``, or ``wick`` for one credit-stop trade."""
    if stop_hit(trade.credit, trade.close_mid, stop_mult):
        return "close"
    if stop_hit(trade.credit, trade.open_mid, stop_mult):
        return "gap"
    return "wick"


def census_stops(trades: Sequence[ReplayTrade], *, stop_mult: float = 1.5) -> StopCensus:
    stops = [t for t in trades if t.exit_reason == "stop_credit"]
    close_confirmed = gap_recovered = wick_only = natural_only = 0
    for trade in stops:
        kind = classify_stop(trade, stop_mult=stop_mult)
        if kind == "close":
            close_confirmed += 1
        elif kind == "gap":
            gap_recovered += 1
        else:
            wick_only += 1
        if not stop_hit(trade.credit, trade.close_mid, stop_mult) and stop_hit(
            trade.credit, trade.close_natural, stop_mult
        ):
            natural_only += 1
    return StopCensus(
        n_stops=len(stops),
        close_confirmed=close_confirmed,
        gap_recovered=gap_recovered,
        wick_only=wick_only,
        close_mid_inside_natural_through=natural_only,
    )


def option_symbol(root: str, expiration: date, right: str, strike: float) -> str:
    """Yahoo/OCC-like symbol without the 6-character root pad."""
    cp = "P" if right.lower().startswith("p") else "C"
    return f"{root.upper()}{expiration.strftime('%y%m%d')}{cp}{int(round(strike * 1000)):08d}"


@dataclass(frozen=True)
class _DayBar:
    day: date
    open: float
    high: float
    low: float
    close: float


def _yahoo_bars(symbol: str, start: datetime, end: datetime, cache: Path) -> list[_DayBar]:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{symbol}.json"
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        url = YAHOO_CHART.format(
            symbol=urllib.parse.quote(symbol),
            start=int(start.timestamp()),
            end=int(end.timestamp()),
        )
        request = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            payload = {"chart": {"error": {"code": exc.code}}}
        path.write_text(json.dumps(payload), encoding="utf-8")
    result = (payload.get("chart") or {}).get("result") or []
    if not result or not result[0] or not result[0].get("timestamp"):
        return []
    block = result[0]
    quote = block["indicators"]["quote"][0]
    bars: list[_DayBar] = []
    for i, ts in enumerate(block["timestamp"]):
        o, h, l, c = quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i]
        if None in (o, h, l, c):
            continue
        # Yahoo stamps the daily bar in UTC. The ET date is the session.
        day = as_et(datetime.fromtimestamp(int(ts), tz=timezone.utc)).date()
        bars.append(_DayBar(day, float(o), float(h), float(l), float(c)))
    return bars


def _on_or_after(bars: Sequence[_DayBar], day: date, slack: int = 4) -> Optional[_DayBar]:
    for bar in bars:
        if day <= bar.day <= day + timedelta(days=slack):
            return bar
    return None


@dataclass(frozen=True)
class MarketCompare:
    source: str
    note: str
    n_sampled: int
    n_entry: int
    n_exit: int
    median_entry_gap: Optional[float]
    median_exit_gap: Optional[float]
    stops_compared: int
    stops_close_confirmed: int
    stops_intraday_only: int
    stops_not_in_range: int
    alpaca_note: str


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def quote_path_verdict(
    credit: float,
    mids: Sequence[float],
    naturals: Sequence[float],
    *,
    stop_mult: float = 1.5,
) -> str:
    """How a real quote path treats a modeled stop.

    ``confirmed`` — the NBBO mid itself traded through the stop.
    ``natural_only`` — the mid stayed inside the stop while short ask − long
    bid went through it (a wide market, not a mid print).
    ``absent`` — neither print reached the stop.
    """
    mid_hit = any(stop_hit(credit, mid, stop_mult) for mid in mids)
    natural_hit = any(stop_hit(credit, nat, stop_mult) for nat in naturals)
    if mid_hit:
        return "confirmed"
    if natural_hit:
        return "natural_only"
    return "absent"


def _alpaca_status() -> str:
    """One line. Never includes the key."""
    try:
        from alpaca_options_credit.credentials import load_credentials
        from alpaca_options_credit.errors import (
            CredentialIsolationError,
            MissingCredentialsError,
            PaperOnlyError,
        )
    except Exception as exc:  # pragma: no cover - import guard
        return f"Alpaca quotes were not pulled ({type(exc).__name__})."
    try:
        load_credentials()
    except MissingCredentialsError:
        return (
            "Alpaca historical option quotes were not pulled: "
            "OPTIONS_APCA_API_KEY_ID / OPTIONS_APCA_API_SECRET_KEY are not set "
            "in this environment, and config/paper-live.yaml is not in the repo."
        )
    except (CredentialIsolationError, PaperOnlyError) as exc:
        return f"Alpaca quotes were not pulled: {exc.__class__.__name__}."
    return "Alpaca keys are present."


def alpaca_quote_sample(
    trades: Sequence[ReplayTrade],
    *,
    stop_mult: float = 1.5,
    limit: int = 12,
) -> tuple[str, dict[str, int]]:
    """NBBO path for a handful of modeled stops. Empty counts when keys are missing."""
    status = _alpaca_status()
    if not status.startswith("Alpaca keys are present"):
        return status, {}
    from alpaca_options_credit.credentials import load_credentials

    creds = load_credentials()
    counts = {"sampled": 0, "confirmed": 0, "natural_only": 0, "absent": 0, "no_print": 0}
    stops = [t for t in trades if t.exit_reason == "stop_credit"][:limit]
    for trade in stops:
        if trade.expiration is None:
            continue
        right = "put" if trade.side == "bullish" else "call"
        start = trade.exit_time - timedelta(hours=1)
        end = trade.exit_time + timedelta(minutes=5)
        short = option_symbol(trade.symbol, trade.expiration, right, trade.short_strike)
        long = option_symbol(trade.symbol, trade.expiration, right, trade.long_strike)
        short_q = _alpaca_quotes(creds.data_url, creds.api_key_id, creds.api_secret_key, short, start, end)
        long_q = _alpaca_quotes(creds.data_url, creds.api_key_id, creds.api_secret_key, long, start, end)
        paired = _pair_quotes(short_q, long_q)
        counts["sampled"] += 1
        if not paired:
            counts["no_print"] += 1
            continue
        mids = [row[0] for row in paired]
        naturals = [row[1] for row in paired]
        counts[quote_path_verdict(trade.credit, mids, naturals, stop_mult=stop_mult)] += 1
    return status, counts


def _alpaca_quotes(data_url, key, secret, occ, start, end) -> list[tuple[datetime, float, float]]:
    params = urllib.parse.urlencode(
        {
            "symbols": occ,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": 1000,
            "sort": "asc",
            "feed": "indicative",
        }
    )
    url = data_url.rstrip("/") + "/v1beta1/options/quotes?" + params
    request = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError:
        return []
    rows = ((payload.get("quotes") or {}).get(occ)) or []
    out: list[tuple[datetime, float, float]] = []
    for row in rows:
        bid = float(row.get("bp") or 0)
        ask = float(row.get("ap") or 0)
        stamp = row.get("t")
        if not stamp or ask <= 0:
            continue
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        out.append((when, bid, ask))
    return out


def _pair_quotes(
    short: Sequence[tuple[datetime, float, float]],
    long: Sequence[tuple[datetime, float, float]],
) -> list[tuple[float, float]]:
    """Align each short quote with the latest long quote at or before it."""
    if not short or not long:
        return []
    paired: list[tuple[float, float]] = []
    j = 0
    for when, s_bid, s_ask in short:
        while j + 1 < len(long) and long[j + 1][0] <= when:
            j += 1
        if long[j][0] > when:
            continue
        _l_when, l_bid, l_ask = long[j]
        mid = (s_bid + s_ask) / 2.0 - (l_bid + l_ask) / 2.0
        natural = s_ask - l_bid
        paired.append((mid, natural))
    return paired


def compare_yahoo(
    trades: Sequence[ReplayTrade],
    cache: Path,
    *,
    stop_mult: float = 1.5,
    limit: int = 80,
) -> MarketCompare:
    """Yahoo daily option bars for a recent sample. Expired symbols 404.

    The adverse print is short high minus long low. Those two extremes are
    not simultaneous, so it is an upper bound on the spread, not a traded price.
    """
    sample = list(trades)[:limit]
    entry_gaps: list[float] = []
    exit_gaps: list[float] = []
    n_entry = n_exit = 0
    stops_compared = close_ok = intraday = missing = 0
    for trade in sample:
        if trade.expiration is None or trade.long_strike <= 0:
            continue
        right = "put" if trade.side == "bullish" else "call"
        start = trade.entry_time - timedelta(days=5)
        end = max(trade.exit_time, datetime.now(timezone.utc)) + timedelta(days=2)
        short_sym = option_symbol(trade.symbol, trade.expiration, right, trade.short_strike)
        long_sym = option_symbol(trade.symbol, trade.expiration, right, trade.long_strike)
        short_bars = _yahoo_bars(short_sym, start, end, cache)
        long_bars = _yahoo_bars(long_sym, start, end, cache)
        entry_day = as_et(trade.entry_time).date()
        exit_day = as_et(trade.exit_time).date()
        entry = _pair(_on_or_after(short_bars, entry_day), _on_or_after(long_bars, entry_day))
        exited = _pair(_on_or_after(short_bars, exit_day), _on_or_after(long_bars, exit_day))
        if entry is not None and trade.entry_mid > 0:
            n_entry += 1
            entry_gaps.append(entry["close"] - trade.entry_mid)
        if exited is not None:
            n_exit += 1
            model_exit = trade.close_mid if trade.exit_reason == "stop_credit" and trade.close_mid else trade.debit
            exit_gaps.append(exited["close"] - model_exit)
        if trade.exit_reason == "stop_credit" and exited is not None:
            stops_compared += 1
            thresh = stop_mult * trade.credit
            if exited["close"] + 1e-9 >= thresh:
                close_ok += 1
            elif exited["adverse"] + 1e-9 >= thresh:
                intraday += 1
            else:
                missing += 1
    note = (
        "Yahoo daily option bars are last trades for contracts that are still "
        "listed. Expired contracts return no bars. The daily close is not an "
        "NBBO mid, and the adverse bound (short high − long low) is not a "
        "simultaneous print."
    )
    return MarketCompare(
        source="yahoo",
        note=note,
        n_sampled=len(sample),
        n_entry=n_entry,
        n_exit=n_exit,
        median_entry_gap=_median(entry_gaps),
        median_exit_gap=_median(exit_gaps),
        stops_compared=stops_compared,
        stops_close_confirmed=close_ok,
        stops_intraday_only=intraday,
        stops_not_in_range=missing,
        alpaca_note=_alpaca_status(),
    )


def _pair(short: Optional[_DayBar], long: Optional[_DayBar]) -> Optional[dict[str, float]]:
    if short is None or long is None or short.day != long.day:
        return None
    return {
        "close": short.close - long.close,
        "adverse": short.high - long.low,
    }
