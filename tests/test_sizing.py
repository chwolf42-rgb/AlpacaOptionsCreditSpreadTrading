from alpaca_options_credit.risk import decide, max_loss_dollars, size_contracts
from alpaca_options_credit.models import (
    ContractQuote,
    OpenSpread,
    SpreadKind,
    SpreadProposal,
    SpreadStatus,
)
from datetime import date


def _proposal(symbol="SPY", width=5.0, credit=1.25):
    dummy = ContractQuote("X", 100.0, date(2026, 4, 17), "put", 1.30, 1.40)
    long = ContractQuote("Y", 95.0, date(2026, 4, 17), "put", 0.10, 0.15)
    return SpreadProposal(
        underlying=symbol,
        kind=SpreadKind.BULL_PUT_CREDIT,
        short=dummy,
        long=long,
        width=width,
        credit=credit,
        qty=0,
        max_loss=0.0,
        invalidation=100.0,
        reason="ok",
    )


def test_max_loss_is_width_minus_credit_times_100():
    assert max_loss_dollars(5.00, 1.25) == 375.0
    assert max_loss_dollars(2.50, 0.50) == 200.0


def test_size_half_percent_of_equity():
    # equity 100k, 0.5% = 500; max loss 375 → 1 contract
    assert size_contracts(100_000, 0.005, 5.00, 1.25) == 1
    # richer account: 500k * 0.5% = 2500 / 375 = 6
    assert size_contracts(500_000, 0.005, 5.00, 1.25) == 6
    assert size_contracts(100, 0.005, 5.00, 1.25) == 0


def test_risk_decision_allows_sized_qty():
    d = decide(
        equity=100_000,
        proposal=_proposal(),
        open_spreads=[],
        risk_pct=0.005,
        max_concurrent=8,
        max_portfolio_risk_pct=0.10,
    )
    assert d.allow is True
    assert d.qty == 1
    assert d.max_loss == 375.0
