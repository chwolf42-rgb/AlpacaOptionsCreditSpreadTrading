"""Alpaca paper broker + market data. SDK usage is confined to this module."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from alpaca_options_credit.broker.payloads import (
    close_credit_spread_payload,
    open_credit_spread_payload,
)
from alpaca_options_credit.credentials import Credentials, assert_expected_account
from alpaca_options_credit.errors import PaperOnlyError
from alpaca_options_credit.models import Bar, ContractQuote, OpenSpread, SpreadProposal

log = logging.getLogger(__name__)


def _timeframe(bar: str):
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    if bar in ("1Day", "1D", "Day", "daily"):
        return TimeFrame(1, TimeFrameUnit.Day)
    if bar in ("1Hour", "1H", "60Min"):
        return TimeFrame(1, TimeFrameUnit.Hour)
    if bar in ("30Min", "30T"):
        return TimeFrame(30, TimeFrameUnit.Minute)
    raise ValueError(f"unsupported timeframe {bar!r} — use 1Day, 1Hour, or 30Min")


def _bars_start(timeframe: str, limit: int, end: datetime) -> datetime:
    """Lookback window long enough for daily VP (~20–30 sessions) or 1H timing."""
    tf = str(timeframe)
    if tf in ("1Day", "1D", "Day", "daily"):
        return end - timedelta(days=max(limit * 3, 90))
    if tf in ("1Hour", "1H", "60Min"):
        return end - timedelta(days=max(21, (limit // 6) + 3))
    if tf in ("30Min", "30T"):
        return end - timedelta(days=max(14, (limit // 12) + 3))
    return end - timedelta(days=30)


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
        return self._submit(payload)

    def submit_close(self, spread: OpenSpread, payload: dict[str, Any]) -> Optional[str]:
        return self._submit(payload)

    def open_order_ids(self) -> list[str]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True)
        orders = self._trading.get_orders(req)
        return [str(getattr(o, "id", "")) for o in orders if getattr(o, "id", None)]

    def _submit(self, payload: dict[str, Any]) -> Optional[str]:
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


class AlpacaMarketData:
    def __init__(self, creds: Credentials, cfg: dict[str, Any]):
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self.creds = creds
        self.cfg = cfg
        self._stock = StockHistoricalDataClient(creds.api_key_id, creds.api_secret_key)
        self._opt_data = OptionHistoricalDataClient(creds.api_key_id, creds.api_secret_key)
        self._trading = TradingClient(
            creds.api_key_id,
            creds.api_secret_key,
            paper=True,
            url_override=creds.base_url,
        )

    def bars(self, symbol: str, timeframe: str, limit: int) -> list[Bar]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest

        feed_name = (self.cfg.get("market_data") or {}).get("stock_feed", "iex")
        feed = DataFeed.IEX if str(feed_name).lower() == "iex" else DataFeed.SIP
        end = datetime.now(timezone.utc)
        start = _bars_start(timeframe, limit, end)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=_timeframe(timeframe),
            start=start,
            end=end,
            limit=limit,
            feed=feed,
        )
        result = self._stock.get_stock_bars(req)
        raw = []
        if hasattr(result, "data"):
            raw = result.data.get(symbol) or []
        elif hasattr(result, "get"):
            raw = result.get(symbol) or []
        bars: list[Bar] = []
        for b in raw:
            ts = getattr(b, "timestamp", None) or getattr(b, "t", datetime.now(timezone.utc))
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
                )
            )
        return out

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


# Re-export payload helpers so engine never imports alpaca-py.
build_open = open_credit_spread_payload
build_close = close_credit_spread_payload
