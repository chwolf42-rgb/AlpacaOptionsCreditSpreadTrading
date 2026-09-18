"""Credit-spread construction: strikes near invalidation, credit/width gate, TP/stop math.

Exits are options-native (fraction of credit), not an equity R-ladder.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Optional, Sequence

from alpaca_options_credit.models import (
    ContractQuote,
    Side,
    SpreadKind,
    SpreadProposal,
)

OCC_ROOT_WIDTH = 6


def occ_symbol(root: str, expiration: date, right: str, strike: float) -> str:
    """OCC OSI: ROOT(6) + YYMMDD + C/P + strike*1000 zero-padded to 8."""
    r = (root.upper() + "      ")[:OCC_ROOT_WIDTH]
    cp = "P" if right.lower().startswith("p") else "C"
    strike_i = int(round(strike * 1000))
    return f"{r}{expiration.strftime('%y%m%d')}{cp}{strike_i:08d}"


def pick_short_strike(
    invalidation: float,
    listed: Sequence[float],
    *,
    adverse: str,
) -> Optional[float]:
    """Listed strike at or just beyond the structure stop (not far-OTM lottery).

    adverse='down' (bull put): prefer nearest strike, stepping to <= invalidation
    if the nearest prints inside the structure.
    adverse='up' (bear call): prefer nearest, stepping to >= invalidation if inside.
    """
    if not listed:
        return None
    unique = sorted(set(float(s) for s in listed))
    nearest = min(unique, key=lambda s: (abs(s - invalidation), s))
    if adverse == "down":
        if nearest > invalidation:
            beyond = [s for s in unique if s <= invalidation]
            return max(beyond) if beyond else nearest
        return nearest
    if nearest < invalidation:
        beyond = [s for s in unique if s >= invalidation]
        return min(beyond) if beyond else nearest
    return nearest


def long_strike_for(kind: SpreadKind, short_strike: float, width: float) -> float:
    if kind is SpreadKind.BULL_PUT_CREDIT:
        return short_strike - width
    return short_strike + width


def credit_meets_width_gate(credit: float, width: float, min_pct: float = 0.20) -> bool:
    if width <= 0 or credit <= 0:
        return False
    return (credit / width) + 1e-12 >= min_pct


def natural_credit(short: ContractQuote, long: ContractQuote) -> float:
    """Fillable credit: short bid − long ask. Mid is not used for the gate."""
    return short.bid - long.ask


def debit_to_close(short_mark: float, long_mark: float) -> float:
    """Debit to buy back the credit spread (short mid − long mid)."""
    return short_mark - long_mark


def take_profit_hit(credit: float, mark_to_close: float, tp_frac: float = 0.50) -> bool:
    """TP when remaining debit-to-close <= (1 - tp_frac) * credit, i.e. captured >= tp_frac."""
    captured = credit - mark_to_close
    return captured + 1e-12 >= tp_frac * credit


def stop_hit(credit: float, mark_to_close: float, stop_mult: float = 2.0) -> bool:
    """Stop when debit-to-close >= stop_mult × credit."""
    return mark_to_close + 1e-12 >= stop_mult * credit


def captured_frac(credit: float, mark_to_close: float) -> float:
    if credit <= 0:
        return 0.0
    return (credit - mark_to_close) / credit


def choose_expiration(today: date, expirations: Iterable[date], dte_min: int, dte_max: int) -> Optional[date]:
    eligible = [
        d
        for d in expirations
        if dte_min <= (d - today).days <= dte_max
    ]
    if not eligible:
        return None
    target = today + timedelta(days=(dte_min + dte_max) // 2)
    return min(eligible, key=lambda d: (abs((d - target).days), d))


def build_proposal(
    *,
    underlying: str,
    side: Side,
    invalidation: float,
    chain: Sequence[ContractQuote],
    width: float,
    min_credit_pct: float,
    today: date,
    dte_min: int,
    dte_max: int,
) -> SpreadProposal:
    kind = (
        SpreadKind.BULL_PUT_CREDIT if side is Side.BULLISH else SpreadKind.BEAR_CALL_CREDIT
    )
    right = "put" if kind is SpreadKind.BULL_PUT_CREDIT else "call"
    matching = [c for c in chain if c.right == right]
    skip = _skip_proposal(underlying, kind, width, invalidation, "empty_chain")
    if not matching:
        return skip

    expiration = choose_expiration(today, {c.expiration for c in matching}, dte_min, dte_max)
    if expiration is None:
        return _skip_proposal(underlying, kind, width, invalidation, "no_dte_in_window")

    slice_ = [c for c in matching if c.expiration == expiration]
    listed = [c.strike for c in slice_]
    adverse = "down" if kind is SpreadKind.BULL_PUT_CREDIT else "up"
    short_k = pick_short_strike(invalidation, listed, adverse=adverse)
    if short_k is None:
        return _skip_proposal(underlying, kind, width, invalidation, "no_listed_short")

    long_k = long_strike_for(kind, short_k, width)
    short = _by_strike(slice_, short_k)
    long = _by_strike(slice_, long_k)
    if short is None or long is None:
        return _skip_proposal(underlying, kind, width, invalidation, "width_not_listed")

    credit = natural_credit(short, long)
    if not credit_meets_width_gate(credit, width, min_credit_pct):
        return SpreadProposal(
            underlying=underlying,
            kind=kind,
            short=short,
            long=long,
            width=width,
            credit=credit,
            qty=0,
            max_loss=0.0,
            invalidation=invalidation,
            reason="credit_below_width_gate",
            skip=True,
            skip_reason=f"credit {credit:.2f} < {min_credit_pct:.0%} of width {width}",
        )

    return SpreadProposal(
        underlying=underlying,
        kind=kind,
        short=short,
        long=long,
        width=width,
        credit=credit,
        qty=0,
        max_loss=0.0,
        invalidation=invalidation,
        reason="ok",
        skip=False,
    )


def _by_strike(quotes: Sequence[ContractQuote], strike: float) -> Optional[ContractQuote]:
    for q in quotes:
        if abs(q.strike - strike) < 1e-9:
            return q
    return None


def _skip_proposal(
    underlying: str,
    kind: SpreadKind,
    width: float,
    invalidation: float,
    reason: str,
) -> SpreadProposal:
    dummy = ContractQuote(
        occ="",
        strike=0.0,
        expiration=date(2099, 1, 1),
        right="put",
        bid=0.0,
        ask=0.0,
    )
    return SpreadProposal(
        underlying=underlying,
        kind=kind,
        short=dummy,
        long=dummy,
        width=width,
        credit=0.0,
        qty=0,
        max_loss=0.0,
        invalidation=invalidation,
        reason=reason,
        skip=True,
        skip_reason=reason,
    )


def should_roll(
    *,
    roll_cfg: dict,
    thesis_intact: bool,
    dte: int,
) -> bool:
    """Stub: close unless knobs say roll. Scaffold still needs execute=true."""
    if not roll_cfg.get("enabled"):
        return False
    if roll_cfg.get("require_thesis_intact", True) and not thesis_intact:
        return False
    if dte < int(roll_cfg.get("min_dte_to_consider", 21)):
        return False
    return bool(roll_cfg.get("execute"))
