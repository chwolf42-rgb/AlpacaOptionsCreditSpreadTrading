from datetime import date

from alpaca_options_credit.models import (
    ContractQuote,
    OpenSpread,
    SpreadKind,
    SpreadProposal,
    SpreadStatus,
)
from alpaca_options_credit.risk import decide


def _proposal(symbol):
    dummy = ContractQuote("X", 100.0, date(2026, 4, 17), "put", 1.3, 1.4)
    return SpreadProposal(
        underlying=symbol,
        kind=SpreadKind.BULL_PUT_CREDIT,
        short=dummy,
        long=dummy,
        width=5.0,
        credit=1.25,
        qty=0,
        max_loss=0.0,
        invalidation=100.0,
        reason="ok",
    )


def _open(symbol):
    return OpenSpread(
        id="s1",
        underlying=symbol,
        kind=SpreadKind.BULL_PUT_CREDIT,
        short_occ="A",
        long_occ="B",
        width=5.0,
        credit=1.25,
        qty=1,
        max_loss=375.0,
        invalidation=100.0,
        status=SpreadStatus.OPEN,
        opened_at="2026-03-03T00:00:00+00:00",
    )


def test_one_spread_per_underlying():
    d = decide(
        equity=100_000,
        proposal=_proposal("SPY"),
        open_spreads=[_open("SPY")],
        risk_pct=0.005,
        max_concurrent=8,
        max_portfolio_risk_pct=0.10,
    )
    assert d.allow is False
    assert d.reason == "one_spread_per_underlying"


def test_exiting_spread_still_blocks_same_underlying():
    exiting = _open("SPY")
    exiting.status = SpreadStatus.EXITING
    exiting.exit_reason = "stop_2x_credit"
    d = decide(
        equity=100_000,
        proposal=_proposal("SPY"),
        open_spreads=[exiting],
        risk_pct=0.005,
        max_concurrent=8,
        max_portfolio_risk_pct=0.10,
    )
    assert d.allow is False
    assert d.reason == "one_spread_per_underlying"


def test_allows_different_underlying():
    d = decide(
        equity=100_000,
        proposal=_proposal("QQQ"),
        open_spreads=[_open("SPY")],
        risk_pct=0.005,
        max_concurrent=8,
        max_portfolio_risk_pct=0.10,
    )
    assert d.allow is True
