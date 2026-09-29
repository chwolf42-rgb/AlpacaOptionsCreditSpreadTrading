"""Alpaca paper broker + market data. SDK usage is confined to this module."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from alpaca_options_credit.bar_quality import (
    DAILY_TIMEFRAMES,
    HOURLY_TIMEFRAMES,
    MIN30_TIMEFRAMES,
    is_bar_closed,
    newest_closed_bars,
    next_bar_close,
    stale_bars_detail,
)
from alpaca_options_credit.broker.payloads import (
    assert_atomic_mleg,
    close_credit_spread_payload,
    open_credit_spread_payload,
)
from alpaca_options_credit.errors import AtomicSpreadError
from alpaca_options_credit.credentials import Credentials, assert_expected_account
from alpaca_options_credit.errors import PaperOnlyError
from alpaca_options_credit.market_data_limit import (
    grant_count,
    install_market_data_client,
    pages_for_bar_rows,
)
from alpaca_options_credit.close_prices import (
    BAR_MAX_AGE,
    QUOTE_MAX_AGE,
    CloseOrderView,
    bar_closes_from_prints,
    close_order_view_from_broker_order,
    leg_price_at,
    quote_mids_from_prints,
    spread_debit,
)
from alpaca_options_credit.models import Bar, ContractQuote, OpenSpread, SpreadProposal

log = logging.getLogger(__name__)


def parse_open_interest(raw: Any) -> Optional[int]:
    """Option-contract open interest. Blank / negative / non-numeric → None."""
    if raw is None or raw == "":
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def _timeframe(bar: str):
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    if bar in DAILY_TIMEFRAMES:
        return TimeFrame(1, TimeFrameUnit.Day)
    if bar in HOURLY_TIMEFRAMES:
        return TimeFrame(1, TimeFrameUnit.Hour)
    if bar in MIN30_TIMEFRAMES:
        return TimeFrame(30, TimeFrameUnit.Minute)
    raise ValueError(f"unsupported timeframe {bar!r} — use 1Day, 1Hour, or 30Min")


def _bars_end() -> datetime:
    return datetime.now(timezone.utc)


def _bars_start(timeframe: str, limit: int, end: datetime) -> datetime:
    """Lookback window long enough for daily VP (~20–30 sessions) or 1H timing."""
    tf = str(timeframe)
    if tf in DAILY_TIMEFRAMES:
        return end - timedelta(days=max(limit * 3, 90))
    if tf in HOURLY_TIMEFRAMES:
        return end - timedelta(days=max(21, (limit // 6) + 3))
    if tf in MIN30_TIMEFRAMES:
        return end - timedelta(days=max(14, (limit // 12) + 3))
    return end - timedelta(days=30)


# One HTTP request for the whole 57-name universe. Chunk only past this so a
# larger list stays batched instead of one page per name.
_BARS_SYMBOLS_PER_REQUEST = 100


@dataclass(frozen=True)
class BarBatch:
    """Closed bars for many symbols, and how many data pages the fetch cost.

    ``pages`` is 0 when every symbol was already the latest closed bar.
    """

    by_symbol: dict[str, list[Bar]]
    pages: int


@dataclass
class _ClosedBarCache:
    bars: list[Bar]
    limit: int
    valid_until: datetime


class AlpacaBroker:
    """Paper-only TradingClient wrapper. Observer should use DryRunBroker instead."""

    dry_run = False

    def __init__(self, creds: Credentials):
        if not creds.paper:
            raise PaperOnlyError("AlpacaBroker refuses paper=False")
        from alpaca.trading.client import TradingClient

        self.creds = creds
        self._trading = TradingClient(
            creds.api_key_id,
            creds.api_secret_key,
            paper=True,
            url_override=creds.base_url,
        )
        acct = self._trading.get_account()
        number = getattr(acct, "account_number", None)
        assert_expected_account(number, creds.expected_account_number)
        trading_blocked = getattr(acct, "trading_blocked", False)
        if trading_blocked:
            raise PaperOnlyError("account trading_blocked")
        self._account = acct

    def account_equity(self) -> float:
        raw = getattr(self._account, "equity", None) or getattr(self._account, "portfolio_value", 0)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    def account_number(self) -> Optional[str]:
        return getattr(self._account, "account_number", None)

    def submit_open(self, proposal: SpreadProposal, payload: dict[str, Any]) -> Optional[str]:
        assert_atomic_mleg(payload, intent="open")
        return self._submit_mleg(payload)

    def submit_close(self, spread: OpenSpread, payload: dict[str, Any]) -> Optional[str]:
        assert_atomic_mleg(payload, intent="close")
        return self._submit_mleg(payload)

    def flatten_residual(self, occ: str, payload: dict[str, Any]) -> Optional[str]:
        if not payload.get("emergency_flatten"):
            raise AtomicSpreadError("flatten_residual requires emergency_flatten payload")
        return self._submit_simple(payload)

    def option_positions(self) -> dict[str, int]:
        """OCC → signed qty. Missing/failed query returns {} (engine alerts)."""
        try:
            positions = self._trading.get_all_positions()
        except Exception as exc:  # pragma: no cover - live path
            log.error("option position query failed: %s", type(exc).__name__)
            return {}
        out: dict[str, int] = {}
        for pos in positions or []:
            asset_class = str(getattr(pos, "asset_class", "") or "").lower()
            symbol = str(getattr(pos, "symbol", "") or "")
            if not symbol:
                continue
            if asset_class and "option" not in asset_class:
                continue
            try:
                qty = int(float(getattr(pos, "qty", 0) or 0))
            except (TypeError, ValueError):
                continue
            side = str(getattr(pos, "side", "") or "").lower()
            if side in {"short", "sell"}:
                qty = -abs(qty)
            else:
                qty = abs(qty) if qty > 0 else qty
            if qty:
                out[symbol] = qty
        return out

    def get_close_order(self, order_id: str) -> Optional[CloseOrderView]:
        """Filled net debit and qty for one mleg close. None if the lookup fails."""
        try:
            order = self._trading.get_order_by_id(order_id)
        except Exception as exc:  # pragma: no cover - live path
            log.error("close order lookup failed: %s", type(exc).__name__)
            return None
        if order is None:
            return None
        return close_order_view_from_broker_order(order)

    def open_order_ids(self) -> list[str]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True)
        orders = self._trading.get_orders(req)
        return [str(getattr(o, "id", "")) for o in orders if getattr(o, "id", None)]

    def _submit_mleg(self, payload: dict[str, Any]) -> Optional[str]:
        from alpaca.trading.enums import OrderClass, OrderSide, PositionIntent, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

        intent_map = {
            "buy_to_open": PositionIntent.BUY_TO_OPEN,
            "sell_to_open": PositionIntent.SELL_TO_OPEN,
            "buy_to_close": PositionIntent.BUY_TO_CLOSE,
            "sell_to_close": PositionIntent.SELL_TO_CLOSE,
        }
        side_map = {"buy": OrderSide.BUY, "sell": OrderSide.SELL}
        tif = TimeInForce.DAY if payload.get("time_in_force", "day") == "day" else TimeInForce.GTC
        legs = [
            OptionLegRequest(
                symbol=leg["symbol"],
                ratio_qty=float(leg["ratio_qty"]),
                side=side_map[leg["side"]],
                position_intent=intent_map[leg["position_intent"]],
            )
            for leg in payload["legs"]
        ]
        req = LimitOrderRequest(
            qty=float(payload["qty"]),
            order_class=OrderClass.MLEG,
            time_in_force=tif,
            limit_price=float(payload["limit_price"]),
            legs=legs,
            client_order_id=payload.get("client_order_id"),
        )
        order = self._trading.submit_order(req)
        return str(getattr(order, "id", "") or "") or None

    def _submit_simple(self, payload: dict[str, Any]) -> Optional[str]:
        from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        intent_map = {
            "buy_to_close": PositionIntent.BUY_TO_CLOSE,
            "sell_to_close": PositionIntent.SELL_TO_CLOSE,
        }
        side_map = {"buy": OrderSide.BUY, "sell": OrderSide.SELL}
        tif = TimeInForce.DAY if payload.get("time_in_force", "day") == "day" else TimeInForce.GTC
        req = LimitOrderRequest(
            symbol=payload["symbol"],
            qty=float(payload["qty"]),
            side=side_map[payload["side"]],
            time_in_force=tif,
            limit_price=float(payload["limit_price"]),
            position_intent=intent_map[payload["position_intent"]],
            client_order_id=payload.get("client_order_id"),
        )
        order = self._trading.submit_order(req)
        return str(getattr(order, "id", "") or "") or None


class AlpacaMarketData:
    """Stock bars, option snapshots, and option quotes. Every data-host page is capped."""

    limits_market_data = True

    def __init__(self, creds: Credentials, cfg: dict[str, Any]):
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self.creds = creds
        self.cfg = cfg
        self._stock = StockHistoricalDataClient(creds.api_key_id, creds.api_secret_key)
        self._opt_data = OptionHistoricalDataClient(creds.api_key_id, creds.api_secret_key)
        # One process-wide limiter, shared by both data clients. Paginated
        # pages and SDK retries all go through the session, so each HTTP
        # response counts once. The trading client is a separate host and
        # stays unwrapped.
        install_market_data_client(self._stock)
        install_market_data_client(self._opt_data)
        self._trading = TradingClient(
            creds.api_key_id,
            creds.api_secret_key,
            paper=True,
            url_override=creds.base_url,
        )

    def bars(self, symbol: str, timeframe: str, limit: int, now: Optional[datetime] = None) -> list[Bar]:
        batch = self.bars_for_symbols([symbol], timeframe, limit, now=now)
        return list(batch.by_symbol.get(symbol) or [])

    def bars_for_symbols(
        self,
        symbols: list[str],
        timeframe: str,
        limit: int,
        now: Optional[datetime] = None,
    ) -> BarBatch:
        """Newest closed bars for every symbol, in as few stock-bar pages as possible.

        A closed daily bar is reused until the next session close. A closed
        hourly bar is reused until the next clock-hour close. The series is
        served only while it is still the latest close and still passes the
        freshness check. A miss refetches the whole set in one multi-symbol
        request. An empty or failed fetch is not stored.
        """
        as_of = _as_utc(now) if now is not None else _bars_end()
        ordered = _dedupe_symbols(symbols)
        if not ordered:
            return BarBatch({}, 0)
        cached: dict[str, list[Bar]] = {}
        for symbol in ordered:
            hit = self._cached_closed_bars(symbol, timeframe, limit, as_of)
            if hit is None:
                cached = {}
                break
            cached[symbol] = hit
        if len(cached) == len(ordered):
            log.debug(
                "bars %s cache hit symbols=%d until next close",
                timeframe,
                len(ordered),
            )
            return BarBatch(cached, 0)
        fetched, pages = self._fetch_closed_bars(ordered, timeframe, limit, as_of)
        return BarBatch(fetched, pages)

    def _closed_cache(self) -> dict[tuple[str, str], _ClosedBarCache]:
        # Tests build this object with __new__ and never call __init__.
        cache = getattr(self, "_closed_bars", None)
        if cache is None:
            cache = {}
            self._closed_bars = cache
        return cache

    def _cached_closed_bars(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        now: datetime,
    ) -> Optional[list[Bar]]:
        entry = self._closed_cache().get((symbol, str(timeframe)))
        if entry is None or int(limit) > entry.limit:
            return None
        if now >= entry.valid_until:
            return None
        bars = entry.bars[-int(limit) :] if len(entry.bars) > int(limit) else list(entry.bars)
        if not bars or not is_bar_closed(bars[-1].ts, timeframe, now):
            return None
        if stale_bars_detail(bars, timeframe, now) is not None:
            return None
        return bars

    def _fetch_closed_bars(
        self,
        symbols: list[str],
        timeframe: str,
        limit: int,
        now: datetime,
    ) -> tuple[dict[str, list[Bar]], int]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest

        feed_name = (self.cfg.get("market_data") or {}).get("stock_feed", "iex")
        feed = DataFeed.IEX if str(feed_name).lower() == "iex" else DataFeed.SIP
        start = _bars_start(timeframe, limit, now)
        pages = 0
        raw_by_symbol: dict[str, list[Any]] = {symbol: [] for symbol in symbols}
        for offset in range(0, len(symbols), _BARS_SYMBOLS_PER_REQUEST):
            chunk = symbols[offset : offset + _BARS_SYMBOLS_PER_REQUEST]
            # Do not pass limit. Alpaca returns oldest-first and keeps only the
            # oldest `limit` rows, so limit=60 over a multi-month daily window
            # ends in the past (AMAT 2026-09-21: 60 bars ending 2026-06-21).
            # The SDK pages the whole [start, end] window when limit is unset.
            req = StockBarsRequest(
                symbol_or_symbols=chunk if len(chunk) > 1 else chunk[0],
                timeframe=_timeframe(timeframe),
                start=start,
                end=now,
                feed=feed,
            )
            before = grant_count()
            result = self._stock.get_stock_bars(req)
            after = grant_count()
            total_raw = 0
            for symbol in chunk:
                raw = _raw_bars_for_symbol(result, symbol)
                raw_by_symbol[symbol] = raw
                total_raw += len(raw)
            granted = (after - before) if before is not None and after is not None else 0
            pages += granted if granted > 0 else pages_for_bar_rows(total_raw)

        valid_until = next_bar_close(timeframe, now)
        cache = self._closed_cache()
        out: dict[str, list[Bar]] = {}
        for symbol, raw in raw_by_symbol.items():
            selected = newest_closed_bars(_bars_from_raw(raw, now), timeframe, limit, now)
            out[symbol] = selected
            # Empty, stale, or still-forming tails are not stored. The next
            # loop refetches the whole set so a recovered feed shows up.
            if (
                selected
                and is_bar_closed(selected[-1].ts, timeframe, now)
                and stale_bars_detail(selected, timeframe, now) is None
            ):
                cache[(symbol, str(timeframe))] = _ClosedBarCache(
                    bars=list(selected),
                    limit=int(limit),
                    valid_until=valid_until,
                )
            if selected:
                log.debug(
                    "bars %s %s fetched=%d kept=%d last=%s pages=%d",
                    symbol,
                    timeframe,
                    len(raw),
                    len(selected),
                    selected[-1].ts,
                    pages,
                )
        return out, pages

    def chain(
        self,
        symbol: str,
        right: str,
        dte_min: int,
        dte_max: int,
        strike_lo: float,
        strike_hi: float,
    ) -> list[ContractQuote]:
        from alpaca.trading.enums import AssetStatus, ContractType
        from alpaca.trading.requests import GetOptionContractsRequest

        today = date.today()
        ctype = ContractType.PUT if right.lower().startswith("p") else ContractType.CALL
        req = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            status=AssetStatus.ACTIVE,
            expiration_date_gte=today + timedelta(days=dte_min),
            expiration_date_lte=today + timedelta(days=dte_max),
            type=ctype,
            strike_price_gte=str(strike_lo),
            strike_price_lte=str(strike_hi),
            limit=1000,
        )
        contracts: list[Any] = []
        page_token = None
        while True:
            req.page_token = page_token
            resp = self._trading.get_option_contracts(req)
            batch = getattr(resp, "option_contracts", None) or []
            contracts.extend(batch)
            page_token = getattr(resp, "next_page_token", None)
            if not page_token:
                break

        occs = [c.symbol for c in contracts if getattr(c, "symbol", None)]
        snaps = self._snapshots(occs)
        out: list[ContractQuote] = []
        for c in contracts:
            occ = c.symbol
            bid, ask = snaps.get(occ, (0.0, 0.0))
            exp = c.expiration_date
            if isinstance(exp, str):
                exp = date.fromisoformat(exp)
            out.append(
                ContractQuote(
                    occ=occ,
                    strike=float(c.strike_price),
                    expiration=exp,
                    right="put" if right.lower().startswith("p") else "call",
                    bid=bid,
                    ask=ask,
                    open_interest=parse_open_interest(getattr(c, "open_interest", None)),
                )
            )
        return out

    def historical_spread_debit(
        self,
        short_occ: str,
        long_occ: str,
        at: datetime,
    ) -> Optional[float]:
        """Per-spread debit at ``at`` from historical quotes, then minute bars.

        Quote mid is preferred within 30 minutes. Otherwise the last minute-bar
        close within 18 hours. Returns None when either leg has no print.
        """
        when = at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)
        short_px = self._historical_leg_price(short_occ, when)
        long_px = self._historical_leg_price(long_occ, when)
        return spread_debit(short_px, long_px)

    def _historical_leg_price(self, occ: str, at: datetime) -> Optional[float]:
        quote_start = at - QUOTE_MAX_AGE
        bar_start = at - BAR_MAX_AGE
        end = at + timedelta(minutes=1)
        quotes = self._option_quote_mids(occ, quote_start, end)
        bars = self._option_bar_closes(occ, bar_start, end)
        return leg_price_at(quotes=quotes, bars=bars, at=at)

    def _option_quote_mids(
        self,
        occ: str,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, float]]:
        # alpaca-py wraps latest quotes and bars, not historical quotes.
        # /v1beta1/options/quotes is the historical NBBO. sort=desc so a
        # short page is the tail nearest the close, not the oldest prints.
        feed_name = str(
            (self.cfg.get("market_data") or {}).get("options_feed", "indicative")
        ).lower()
        params: dict[str, Any] = {
            "symbols": occ,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": 1000,
            "sort": "desc",
            "feed": feed_name,
        }
        try:
            response = self._opt_data.get("/options/quotes", data=params)
        except Exception as exc:  # pragma: no cover - live path
            params.pop("feed", None)
            try:
                response = self._opt_data.get("/options/quotes", data=params)
            except Exception as retry_exc:  # pragma: no cover - live path
                log.warning(
                    "option quotes failed for %s: %s",
                    occ,
                    type(retry_exc).__name__ if retry_exc else type(exc).__name__,
                )
                return []
        payload = response if isinstance(response, dict) else {}
        quotes = payload.get("quotes") or {}
        rows = quotes.get(occ) or []
        return quote_mids_from_prints(rows)

    def _option_bar_closes(
        self,
        occ: str,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, float]]:
        from alpaca.data.requests import OptionBarsRequest
        from alpaca.data.timeframe import TimeFrame

        # OptionBarsRequest has no feed field in alpaca-py; the data client
        # uses the account's entitled option feed.
        try:
            req = OptionBarsRequest(
                symbol_or_symbols=occ,
                timeframe=TimeFrame.Minute,
                start=start,
                end=end,
            )
            result = self._opt_data.get_option_bars(req)
        except Exception as exc:  # pragma: no cover - live path
            log.warning("option bars failed for %s: %s", occ, type(exc).__name__)
            return []
        return bar_closes_from_prints(_series_for_symbol(result, occ))

    def spread_mark(self, short_occ: str, long_occ: str) -> Optional[float]:
        snaps = self._snapshots([short_occ, long_occ])
        if short_occ not in snaps or long_occ not in snaps:
            return None
        s_bid, s_ask = snaps[short_occ]
        l_bid, l_ask = snaps[long_occ]
        short_mid = (s_bid + s_ask) / 2.0
        long_mid = (l_bid + l_ask) / 2.0
        return short_mid - long_mid

    def _snapshots(self, symbols: list[str]) -> dict[str, tuple[float, float]]:
        if not symbols:
            return {}
        from alpaca.data.enums import OptionsFeed
        from alpaca.data.requests import OptionSnapshotRequest

        feed_name = (self.cfg.get("market_data") or {}).get("options_feed", "indicative")
        feed = OptionsFeed.INDICATIVE if str(feed_name).lower() == "indicative" else OptionsFeed.OPRA
        out: dict[str, tuple[float, float]] = {}
        # API caps symbols per request (~100).
        for i in range(0, len(symbols), 100):
            chunk = symbols[i : i + 100]
            req = OptionSnapshotRequest(symbol_or_symbols=chunk, feed=feed)
            try:
                snap = self._opt_data.get_option_snapshot(req)
            except Exception as exc:  # pragma: no cover - live path
                log.warning("option snapshot failed: %s", type(exc).__name__)
                continue
            data = getattr(snap, "data", snap) if snap is not None else {}
            if hasattr(data, "items"):
                items = data.items()
            elif isinstance(data, dict):
                items = data.items()
            else:
                items = []
            for occ, payload in items:
                quote = getattr(payload, "latest_quote", None) or payload
                bid = float(getattr(quote, "bid_price", 0) or getattr(quote, "bp", 0) or 0)
                ask = float(getattr(quote, "ask_price", 0) or getattr(quote, "ap", 0) or 0)
                out[str(occ)] = (bid, ask)
        return out


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _dedupe_symbols(symbols: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = str(raw)
        if symbol and symbol not in seen:
            seen.add(symbol)
            ordered.append(symbol)
    return ordered


def _raw_bars_for_symbol(result: Any, symbol: str) -> list[Any]:
    if hasattr(result, "data"):
        data = result.data
        if hasattr(data, "get"):
            return list(data.get(symbol) or [])
    if hasattr(result, "get"):
        return list(result.get(symbol) or [])
    return []


def _bars_from_raw(raw: list[Any], fallback_ts: datetime) -> list[Bar]:
    bars: list[Bar] = []
    for b in raw:
        ts = getattr(b, "timestamp", None) or getattr(b, "t", fallback_ts)
        bars.append(
            Bar(
                ts=ts,
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=float(getattr(b, "volume", 0) or 0),
            )
        )
    return bars


def _series_for_symbol(result: Any, symbol: str) -> list[Any]:
    """Alpaca BarSet / QuoteSet → list of prints for one OCC symbol."""
    data = getattr(result, "data", result)
    raw: Any = None
    if hasattr(data, "get"):
        raw = data.get(symbol)
        if raw is None and hasattr(data, "keys"):
            for key in list(data.keys()):
                if str(key) == symbol:
                    raw = data.get(key)
                    break
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    return list(raw)


# Re-export payload helpers so engine never imports alpaca-py.
build_open = open_credit_spread_payload
build_close = close_credit_spread_payload
