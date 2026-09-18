"""Broker protocol. Live Alpaca vs observer/dry-run that never places orders."""

from __future__ import annotations

from typing import Any, Optional, Protocol

from alpaca_options_credit.models import ContractQuote, OpenSpread, SpreadProposal


class Broker(Protocol):
    dry_run: bool

    def account_equity(self) -> float: ...

    def account_number(self) -> Optional[str]: ...

    def submit_open(self, proposal: SpreadProposal, payload: dict[str, Any]) -> Optional[str]:
        """Return broker order id, or None if nothing was sent."""

    def submit_close(self, spread: OpenSpread, payload: dict[str, Any]) -> Optional[str]:
        ...

    def open_order_ids(self) -> list[str]:
        ...


class MarketData(Protocol):
    def bars(self, symbol: str, timeframe: str, limit: int) -> list: ...

    def chain(
        self,
        symbol: str,
        right: str,
        dte_min: int,
        dte_max: int,
        strike_lo: float,
        strike_hi: float,
    ) -> list[ContractQuote]: ...

    def spread_mark(self, short_occ: str, long_occ: str) -> Optional[float]:
        """Debit to close (short mid − long mid), or None."""
