"""Max-loss sizing, one-spread-per-underlying, concurrent and budget caps."""

from __future__ import annotations

from dataclasses import dataclass

from alpaca_options_credit.models import (
    LIVE_SPREAD_STATUSES,
    OpenSpread,
    SpreadProposal,
)


def max_loss_dollars(width: float, credit: float, multiplier: int = 100) -> float:
    """width×100 − credit×100. Credit is the option premium (per share)."""
    return (width - credit) * multiplier


def size_contracts(
    equity: float,
    risk_pct: float,
    width: float,
    credit: float,
    multiplier: int = 100,
) -> int:
    """Floor of (equity * risk_pct) / max_loss. Zero if un-sizeable."""
    ml = max_loss_dollars(width, credit, multiplier)
    if ml <= 0 or equity <= 0 or risk_pct <= 0:
        return 0
    budget = equity * risk_pct
    return int(budget // ml)


@dataclass
class RiskDecision:
    allow: bool
    qty: int
    reason: str
    max_loss: float


def decide(
    *,
    equity: float,
    proposal: SpreadProposal,
    open_spreads: list[OpenSpread],
    risk_pct: float,
    max_concurrent: int,
    max_portfolio_risk_pct: float,
    one_per_underlying: bool = True,
    multiplier: int = 100,
) -> RiskDecision:
    live = [s for s in open_spreads if s.status in LIVE_SPREAD_STATUSES]
    if one_per_underlying and any(s.underlying == proposal.underlying for s in live):
        return RiskDecision(False, 0, "one_spread_per_underlying", 0.0)
    if len(live) >= max_concurrent:
        return RiskDecision(False, 0, "max_concurrent", 0.0)

    qty = size_contracts(equity, risk_pct, proposal.width, proposal.credit, multiplier)
    if qty < 1:
        return RiskDecision(False, 0, "size_zero", 0.0)

    this_loss = max_loss_dollars(proposal.width, proposal.credit, multiplier) * qty
    open_loss = sum(s.max_loss for s in live)
    cap = equity * max_portfolio_risk_pct
    if open_loss + this_loss > cap + 1e-9:
        return RiskDecision(False, 0, "max_risk_budget", this_loss)

    return RiskDecision(True, qty, "ok", this_loss)
