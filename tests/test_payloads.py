from datetime import date

import pytest

from alpaca_options_credit.broker.payloads import (
    FORBIDDEN_SINGLE_LEG_HELPERS,
    assert_atomic_mleg,
    close_credit_spread_payload,
    emergency_flatten_residual_leg_payload,
    mleg_order,
    open_credit_spread_payload,
)
from alpaca_options_credit.errors import AtomicSpreadError
import alpaca_options_credit.broker.payloads as payloads_mod
from alpaca_options_credit.models import ContractQuote, SpreadKind, SpreadProposal


def test_open_payload_is_signed_credit_mleg():
    short = ContractQuote("SPY260417P00100000", 100.0, date(2026, 4, 17), "put", 1.30, 1.40)
    long = ContractQuote("SPY260417P00095000", 95.0, date(2026, 4, 17), "put", 0.20, 0.25)
    prop = SpreadProposal(
        underlying="SPY",
        kind=SpreadKind.BULL_PUT_CREDIT,
        short=short,
        long=long,
        width=5.0,
        credit=1.05,
        qty=2,
        max_loss=790.0,
        invalidation=100.0,
        reason="ok",
    )
    payload = open_credit_spread_payload(prop)
    assert payload["order_class"] == "mleg"
    assert payload["type"] == "limit"
    assert payload["qty"] == "2"
    assert payload["limit_price"] == "-1.05"
    intents = {leg["position_intent"] for leg in payload["legs"]}
    assert intents == {"sell_to_open", "buy_to_open"}
    assert all("extended_hours" not in payload for _ in [0])


def test_close_payload_rejects_zero_qty():
    with pytest.raises(ValueError, match="live spread size"):
        close_credit_spread_payload(
            short_occ="SPY260417P00100000",
            long_occ="SPY260417P00095000",
            qty=0,
            debit=0.60,
        )


def test_close_payload_is_signed_debit():
    payload = close_credit_spread_payload(
        short_occ="SPY260417P00100000",
        long_occ="SPY260417P00095000",
        qty=1,
        debit=0.60,
    )
    assert payload["limit_price"] == "0.60"
    intents = {leg["position_intent"] for leg in payload["legs"]}
    assert intents == {"buy_to_close", "sell_to_close"}
    assert_atomic_mleg(payload, intent="close")
    assert payload["order_class"] == "mleg"
    assert len(payload["legs"]) == 2


def test_assert_atomic_mleg_rejects_single_leg():
    one = mleg_order(
        qty=1,
        limit_price=0.60,
        legs=[{"symbol": "A", "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_close"}],
    )
    with pytest.raises(AtomicSpreadError, match="exactly 2 legs"):
        assert_atomic_mleg(one, intent="close")


def test_assert_atomic_mleg_rejects_non_mleg():
    with pytest.raises(AtomicSpreadError, match="order_class"):
        assert_atomic_mleg({"order_class": "simple", "qty": 1, "legs": []}, intent="close")


def test_no_single_leg_close_helpers():
    for name in FORBIDDEN_SINGLE_LEG_HELPERS:
        assert not hasattr(payloads_mod, name)


def test_emergency_flatten_is_tagged_not_atomic():
    payload = emergency_flatten_residual_leg_payload(
        occ="SPY260417P00100000",
        qty=1,
        flatten_short=True,
        limit_price=1.20,
    )
    assert payload["emergency_flatten"] is True
    assert payload["order_class"] == "simple"
    assert payload["position_intent"] == "buy_to_close"
    with pytest.raises(AtomicSpreadError, match="emergency flatten"):
        assert_atomic_mleg(payload, intent="close")
