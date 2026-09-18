from datetime import date

from alpaca_options_credit.broker.payloads import (
    close_credit_spread_payload,
    open_credit_spread_payload,
)
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
