"""Observer / dry-run broker: records proposed mleg payloads, places zero orders."""

from __future__ import annotations

from typing import Any, Optional

from alpaca_options_credit.models import OpenSpread, SpreadProposal


class DryRunBroker:
    """Same signals as live; submit_* logs proposals and never hits Alpaca orders."""

    dry_run = True

    def __init__(self, equity: float = 100_000.0, account_number: Optional[str] = None):
        self._equity = equity
        self._account_number = account_number
        self.proposed_opens: list[dict[str, Any]] = []
        self.proposed_closes: list[dict[str, Any]] = []
        self.submitted_order_ids: list[str] = []

    def account_equity(self) -> float:
        return self._equity

    def account_number(self) -> Optional[str]:
        return self._account_number

    def submit_open(self, proposal: SpreadProposal, payload: dict[str, Any]) -> Optional[str]:
        self.proposed_opens.append(
            {
                "underlying": proposal.underlying,
                "kind": proposal.kind.value,
                "credit": proposal.credit,
                "qty": proposal.qty,
                "payload": payload,
            }
        )
        return None

    def submit_close(self, spread: OpenSpread, payload: dict[str, Any]) -> Optional[str]:
        self.proposed_closes.append(
            {
                "underlying": spread.underlying,
                "spread_id": spread.id,
                "payload": payload,
                "qty": payload.get("qty"),
            }
        )
        return None

    def open_order_ids(self) -> list[str]:
        return list(self.submitted_order_ids)

    def cancel_order(self, order_id: str) -> None:
        """Forbidden on this sleeve — cancel-before-replace strands a spread."""
        raise RuntimeError(
            "options sleeve must not cancel working exits "
            f"(cancel-before-replace naked window; order_id={order_id})"
        )
