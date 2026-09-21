"""Credit-spread construction: strikes near invalidation, credit/width gate, TP/stop math.

Exits are options-native (fraction of credit), not an equity R-ladder.

Quote quality is gated before a candidate is treated as a proposal. Junk,
crossed, missing, and non-positive credits get a stable skip token
(`quote_crossed`, `credit_debit`, `credit_below_min_pct`, …) and must not
be logged as proposals.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Iterable, Optional, Sequence

from alpaca_options_credit.models import (
    ContractQuote,
    Side,
    SpreadKind,
    SpreadProposal,
)

# Stable tokens. EOD digests count these; do not put free text in skip_reason.
QUOTE_MISSING = "quote_missing"
QUOTE_CROSSED = "quote_crossed"
QUOTE_ABSURD = "quote_absurd"
QUOTE_WIDE = "quote_wide"
QUOTE_THIN_OI = "quote_thin_oi"
CREDIT_DEBIT = "credit_debit"
CREDIT_BELOW_MIN_PCT = "credit_below_min_pct"

# These fail before the proposal log. Structural skips (empty chain, DTE, …)
# are not in this set.
PRE_PROPOSAL_SKIP_REASONS = frozenset(
    {
        QUOTE_MISSING,
        QUOTE_CROSSED,
        QUOTE_ABSURD,
        QUOTE_WIDE,
        QUOTE_THIN_OI,
        CREDIT_DEBIT,
        CREDIT_BELOW_MIN_PCT,
    }
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


def mid_credit(short: ContractQuote, long: ContractQuote) -> Optional[float]:
    """Short mid − long mid. Diagnostic only; the gate uses natural credit.

    On two-sided uncrossed quotes, mid credit is at least the natural credit,
    so a non-positive natural is already a debit (or worse) at the mid.
    """
    if not (_finite(short.bid) and _finite(short.ask) and _finite(long.bid) and _finite(long.ask)):
        return None
    return short.mid - long.mid


def entry_skip_event_kind(skip_reason: str) -> str:
    """Journal kind for a pre-proposal skip. Credit vs quote so digests can split them."""
    if skip_reason.startswith("credit_"):
        return "credit_skip"
    return "quote_skip"


def _finite(value: float) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _leg_quote_skip(quote: ContractQuote) -> Optional[str]:
    """First failing leg check. Order: missing → absurd → crossed."""
    if not _finite(quote.bid) or not _finite(quote.ask):
        return QUOTE_MISSING
    if quote.bid < 0 or quote.ask < 0:
        return QUOTE_ABSURD
    # No two-sided market. A zero bid or ask is not a fillable credit.
    if quote.bid <= 0 or quote.ask <= 0:
        return QUOTE_MISSING
    if quote.bid > quote.ask + 1e-12:
        return QUOTE_CROSSED
    return None


def _leg_wide(quote: ContractQuote, max_spread_pct_of_mid: float) -> bool:
    if max_spread_pct_of_mid <= 0:
        return False
    mid = quote.mid
    if mid <= 0:
        return True
    return (quote.ask - quote.bid) / mid > max_spread_pct_of_mid + 1e-12


def _open_interest_thin(quote: ContractQuote, min_open_interest: int) -> bool:
    if min_open_interest <= 0:
        return False
    oi = quote.open_interest
    if oi is None:
        return True
    return int(oi) < min_open_interest


def classify_credit_skip(
    short: ContractQuote,
    long: ContractQuote,
    *,
    width: float,
    min_credit_pct: float,
    max_leg_spread_pct_of_mid: float = 0.0,
    max_credit_pct_of_width: float = 1.0,
    min_open_interest: int = 0,
) -> tuple[Optional[str], float]:
    """Quote and credit gates, earliest failure first.

    Returns (skip_reason, natural_credit). skip_reason is None only when the
    natural credit is strictly positive and clears the width floor.

    Order:
      1. missing / non-finite quotes
      2. negative (absurd) prices
      3. crossed bid > ask
      4. optional wide leg markets
      5. optional thin open interest
      6. natural credit at or above the width cap (impossible for a vertical)
      7. non-positive / debit natural credit
      8. positive credit below min_credit_pct of width
    """
    for quote in (short, long):
        leg_reason = _leg_quote_skip(quote)
        if leg_reason:
            credit = 0.0
            if (
                _finite(short.bid)
                and _finite(short.ask)
                and _finite(long.bid)
                and _finite(long.ask)
            ):
                credit = natural_credit(short, long)
            return leg_reason, credit

    if _leg_wide(short, max_leg_spread_pct_of_mid) or _leg_wide(long, max_leg_spread_pct_of_mid):
        return QUOTE_WIDE, natural_credit(short, long)

    if _open_interest_thin(short, min_open_interest) or _open_interest_thin(
        long, min_open_interest
    ):
        return QUOTE_THIN_OI, natural_credit(short, long)

    credit = natural_credit(short, long)
    if not _finite(credit):
        return QUOTE_ABSURD, 0.0

    # A vertical's price lives in [0, width]. Outside that, the quote is junk
    # even if each leg looks two-sided.
    if width > 0 and max_credit_pct_of_width > 0:
        if credit + 1e-12 >= max_credit_pct_of_width * width:
            return QUOTE_ABSURD, credit
    if width > 0 and credit < -width - 1e-12:
        return QUOTE_ABSURD, credit

    if credit <= 0:
        return CREDIT_DEBIT, credit

    if not credit_meets_width_gate(credit, width, min_credit_pct):
        return CREDIT_BELOW_MIN_PCT, credit

    return None, credit


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
    max_leg_spread_pct_of_mid: float = 0.0,
    max_credit_pct_of_width: float = 1.0,
    min_open_interest: int = 0,
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

    skip_reason, credit = classify_credit_skip(
        short,
        long,
        width=width,
        min_credit_pct=min_credit_pct,
        max_leg_spread_pct_of_mid=max_leg_spread_pct_of_mid,
        max_credit_pct_of_width=max_credit_pct_of_width,
        min_open_interest=min_open_interest,
    )
    if skip_reason:
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
            reason=skip_reason,
            skip=True,
            skip_reason=skip_reason,
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
