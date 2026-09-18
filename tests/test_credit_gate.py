from datetime import date, timedelta

import pytest

from alpaca_options_credit.models import ContractQuote, Side
from alpaca_options_credit.strategy.spreads import (
    build_proposal,
    credit_meets_width_gate,
    natural_credit,
    occ_symbol,
)


def test_credit_width_gate_20pct():
    assert credit_meets_width_gate(1.00, 5.00, 0.20) is True
    assert credit_meets_width_gate(0.99, 5.00, 0.20) is False
    assert credit_meets_width_gate(0.50, 2.50, 0.20) is True
    assert credit_meets_width_gate(0.40, 2.50, 0.20) is False
    assert credit_meets_width_gate(0.0, 5.0, 0.20) is False


def test_build_proposal_skips_thin_credit():
    today = date(2026, 3, 3)
    exp = today + timedelta(days=37)
    inv = 100.0
    width = 5.0
    # natural credit = short bid 0.70 − long ask 0.30 = 0.40 → 8% of 5.00 < 20%
    chain = []
    for k, bid, ask in [(100.0, 0.70, 0.80), (95.0, 0.20, 0.30)]:
        chain.append(
            ContractQuote(
                occ=occ_symbol("SPY", exp, "put", k),
                strike=k,
                expiration=exp,
                right="put",
                bid=bid,
                ask=ask,
            )
        )
    prop = build_proposal(
        underlying="SPY",
        side=Side.BULLISH,
        invalidation=inv,
        chain=chain,
        width=width,
        min_credit_pct=0.20,
        today=today,
        dte_min=30,
        dte_max=45,
    )
    assert prop.skip is True
    assert prop.skip_reason.startswith("credit")
    assert natural_credit(prop.short, prop.long) == pytest.approx(0.40)
