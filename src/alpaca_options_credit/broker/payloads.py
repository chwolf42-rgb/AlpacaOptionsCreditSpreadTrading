"""Encapsulated Alpaca multi-leg payloads.

Alpaca convention (documented deviation from "premium is always positive"):
- POST /v2/orders with order_class=mleg
- limit_price is SIGNED: positive = debit paid, negative = credit received
- Each leg needs side + position_intent (buy_to_open / sell_to_open / *_to_close)
- time_in_force day or gtc; extended_hours must be false/omitted
- qty is the number of spread *units* (not shares)
- ratio_qty must be coprime (we always use 1:1)

We build dicts here so tests do not need alpaca-py; AlpacaBroker maps dict → SDK.
"""

from __future__ import annotations

from typing import Any, Literal

from alpaca_options_credit.models import SpreadKind, SpreadProposal

Intent = Literal["buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"]
SideName = Literal["buy", "sell"]


def credit_limit_price(credit: float) -> float:
    """Negative limit_price = minimum credit we will accept."""
    return -abs(credit)


def debit_limit_price(debit: float) -> float:
    """Positive limit_price = maximum debit we will pay to close."""
    return abs(debit)


def mleg_order(
    *,
    qty: int,
    limit_price: float,
    legs: list[dict[str, Any]],
    time_in_force: str = "day",
    client_order_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "order_class": "mleg",
        "qty": str(int(qty)),
        "type": "limit",
        "limit_price": f"{limit_price:.2f}",
        "time_in_force": time_in_force,
        "legs": legs,
    }
    if client_order_id:
        payload["client_order_id"] = client_order_id
    return payload


def leg(symbol: str, side: SideName, intent: Intent, ratio_qty: int = 1) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "ratio_qty": str(int(ratio_qty)),
        "side": side,
        "position_intent": intent,
    }


def open_credit_spread_payload(
    proposal: SpreadProposal,
    *,
    time_in_force: str = "day",
    client_order_id: str | None = None,
) -> dict[str, Any]:
    """Bull put: sell higher put, buy lower put. Bear call: sell lower call, buy higher call."""
    if proposal.kind not in (SpreadKind.BULL_PUT_CREDIT, SpreadKind.BEAR_CALL_CREDIT):
        raise ValueError(f"unsupported structure {proposal.kind}")
    legs = [
        leg(proposal.short.occ, "sell", "sell_to_open"),
        leg(proposal.long.occ, "buy", "buy_to_open"),
    ]
    return mleg_order(
        qty=proposal.qty,
        limit_price=credit_limit_price(proposal.credit),
        legs=legs,
        time_in_force=time_in_force,
        client_order_id=client_order_id,
    )


def close_credit_spread_payload(
    *,
    short_occ: str,
    long_occ: str,
    qty: int,
    debit: float,
    time_in_force: str = "day",
    client_order_id: str | None = None,
) -> dict[str, Any]:
    legs = [
        leg(short_occ, "buy", "buy_to_close"),
        leg(long_occ, "sell", "sell_to_close"),
    ]
    return mleg_order(
        qty=qty,
        limit_price=debit_limit_price(debit),
        legs=legs,
        time_in_force=time_in_force,
        client_order_id=client_order_id,
    )
