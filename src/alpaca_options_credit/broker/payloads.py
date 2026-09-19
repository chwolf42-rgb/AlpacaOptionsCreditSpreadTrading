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

from alpaca_options_credit.errors import AtomicSpreadError
from alpaca_options_credit.models import SpreadKind, SpreadProposal

OPEN_INTENTS = frozenset({"sell_to_open", "buy_to_open"})
CLOSE_INTENTS = frozenset({"buy_to_close", "sell_to_close"})
# Names that must never exist as public single-leg close helpers.
FORBIDDEN_SINGLE_LEG_HELPERS = (
    "close_leg",
    "close_short",
    "close_long",
    "single_leg_close",
    "leg_out",
    "close_one_leg",
)

Intent = Literal["buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"]
SideName = Literal["buy", "sell"]


def credit_limit_price(credit: float) -> float:
    """Negative limit_price = minimum credit we will accept."""
    return -abs(credit)


def debit_limit_price(debit: float) -> float:
    """Positive limit_price = maximum debit we will pay to close."""
    return abs(debit)


def assert_atomic_mleg(payload: dict[str, Any], *, intent: Literal["open", "close"]) -> dict[str, Any]:
    """Refuse anything that is not a 2-leg mleg open or close.

    A 1-leg close of a healthy spread is how you get a naked short and a
    margin call. Emergency flatten of an *already* residual leg is a
    different helper and is tagged ``emergency_flatten``.
    """
    if payload.get("emergency_flatten"):
        raise AtomicSpreadError(
            "emergency flatten is not an atomic spread close; "
            "do not pass it through the normal mleg path"
        )
    if str(payload.get("order_class") or "").lower() != "mleg":
        raise AtomicSpreadError(
            f"order_class={payload.get('order_class')!r} is forbidden; "
            "entry/exit must be order_class=mleg"
        )
    legs = payload.get("legs") or []
    if len(legs) != 2:
        raise AtomicSpreadError(
            f"atomic credit spread requires exactly 2 legs, got {len(legs)}"
        )
    intents = {str(leg.get("position_intent") or "") for leg in legs}
    expected = OPEN_INTENTS if intent == "open" else CLOSE_INTENTS
    if intents != set(expected):
        raise AtomicSpreadError(
            f"mleg {intent} intents {sorted(intents)} != {sorted(expected)}"
        )
    symbols = [str(leg.get("symbol") or "") for leg in legs]
    if len(set(symbols)) != 2 or not all(symbols):
        raise AtomicSpreadError("mleg must name two distinct OCC symbols")
    try:
        qty = int(payload.get("qty"))
    except (TypeError, ValueError) as exc:
        raise AtomicSpreadError("mleg qty missing") from exc
    if qty <= 0:
        raise AtomicSpreadError(f"mleg qty must be the live spread size, got {qty!r}")
    return payload


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
    return assert_atomic_mleg(
        mleg_order(
            qty=proposal.qty,
            limit_price=credit_limit_price(proposal.credit),
            legs=legs,
            time_in_force=time_in_force,
            client_order_id=client_order_id,
        ),
        intent="open",
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
    # Close the full journaled spread qty in one mleg. No equity-style
    # tranches (qty=60 vs leftover child-stop fights) and no OCO legs.
    if int(qty) <= 0:
        raise ValueError(f"close qty must be the live spread size, got {qty!r}")
    legs = [
        leg(short_occ, "buy", "buy_to_close"),
        leg(long_occ, "sell", "sell_to_close"),
    ]
    return assert_atomic_mleg(
        mleg_order(
            qty=qty,
            limit_price=debit_limit_price(debit),
            legs=legs,
            time_in_force=time_in_force,
            client_order_id=client_order_id,
        ),
        intent="close",
    )


def emergency_flatten_residual_leg_payload(
    *,
    occ: str,
    qty: int,
    flatten_short: bool,
    limit_price: float,
    time_in_force: str = "day",
) -> dict[str, Any]:
    """CRITICAL residual only — flatten a leftover leg after a broken mleg.

    Not a spread exit. Never used to 'leg out' of a healthy 2-leg book.
    A naked short must be bought in; a leftover long is sold to close.
    """
    if int(qty) <= 0 or not occ:
        raise AtomicSpreadError(f"emergency flatten needs occ + qty, got {occ!r} qty={qty!r}")
    if flatten_short:
        side, intent = "buy", "buy_to_close"
    else:
        side, intent = "sell", "sell_to_close"
    return {
        "order_class": "simple",
        "emergency_flatten": True,
        "qty": str(int(qty)),
        "type": "limit",
        "limit_price": f"{abs(limit_price):.2f}",
        "time_in_force": time_in_force,
        "symbol": occ,
        "side": side,
        "position_intent": intent,
        "legs": [leg(occ, side, intent)],  # one contract remains; not a spread close
    }
