"""Locked entry rules (2026-09-24): no underwater open, short clears inv gap.

Structure-break exits are not part of these tests. honor_structure_break stays
on the exit path in engine._evaluate_exit_reason.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from alpaca_options_credit.bar_quality import last_completed_daily_bar
from alpaca_options_credit.models import Bar, ContractQuote, Side, SpreadKind, SpreadProposal
from alpaca_options_credit.strategy.spreads import (
    DAILY_CLOSE_THROUGH_INV,
    SHORT_TOO_CLOSE_TO_INV,
    UNDERWATER_OPEN_BLOCKED,
    build_proposal,
    occ_symbol,
    pick_short_strike,
)
from alpaca_options_credit.strategy.structure import daily_close_through_invalidation
from tests.helpers import hybrid_happy_daily_hourly
from tests.test_hybrid_engine import _engine, _events

UTC = timezone.utc
# EEM 2026-09-23: armed inv 68.41, short 68 (gap 0.41), daily close through inv.
EEM_INV = 68.41
EEM_SHORT = 68.0
EEM_CLOSE = 67.72


def test_bull_put_blocked_when_daily_close_below_inv():
    assert daily_close_through_invalidation(Side.BULLISH, EEM_INV, EEM_CLOSE) is True
    assert daily_close_through_invalidation(Side.BULLISH, EEM_INV, 67.86) is True
    # A close exactly on the level still holds (same comparison as structure-break).
    assert daily_close_through_invalidation(Side.BULLISH, EEM_INV, EEM_INV) is False
    assert daily_close_through_invalidation(Side.BULLISH, EEM_INV, 69.0) is False


def test_bear_call_blocked_when_daily_close_above_inv():
    assert daily_close_through_invalidation(Side.BEARISH, EEM_INV, 69.10) is True
    assert daily_close_through_invalidation(Side.BEARISH, EEM_INV, EEM_INV) is False
    assert daily_close_through_invalidation(Side.BEARISH, EEM_INV, 67.0) is False


def test_short_within_gap_of_inv_rejected():
    # EEM: short 68 vs inv 68.41 is only 0.41 away. Do not sell 68, and do not
    # tighten up through inv to 69 when nothing clears a 1.0 gap.
    assert pick_short_strike(EEM_INV, [EEM_SHORT, 68.5, 69.0], adverse="down", min_short_inv_gap=1.0) is None
    assert (
        pick_short_strike(EEM_INV, [67.0, 68.0, 69.0], adverse="up", min_short_inv_gap=1.0)
        is None
    )

    today = date(2026, 9, 23)
    exp = today + timedelta(days=37)
    chain = [_quote("EEM", exp, "put", k, 1.40, 1.45) for k in (68.0, 68.5, 69.0)]
    prop = build_proposal(
        underlying="EEM",
        side=Side.BULLISH,
        invalidation=EEM_INV,
        chain=chain,
        width=5.0,
        min_credit_pct=0.20,
        today=today,
        dte_min=30,
        dte_max=45,
        min_short_inv_gap=1.0,
    )
    assert prop.skip is True
    assert prop.skip_reason == SHORT_TOO_CLOSE_TO_INV
    assert prop.short.strike != EEM_SHORT


def test_short_with_enough_gap_allowed_when_daily_close_ok():
    # 67 is 1.41 beyond 68.41. 68 is closer and must not be chosen.
    assert pick_short_strike(
        EEM_INV, [66.0, 67.0, 68.0, 69.0], adverse="down", min_short_inv_gap=1.0
    ) == 67.0
    assert pick_short_strike(
        EEM_INV, [67.0, 68.0, 69.0, 70.0], adverse="up", min_short_inv_gap=1.0
    ) == 70.0
    assert daily_close_through_invalidation(Side.BULLISH, EEM_INV, 70.0) is False

    today = date(2026, 9, 23)
    exp = today + timedelta(days=37)
    chain = [
        _quote("EEM", exp, "put", 67.0, 1.40, 1.45),
        _quote("EEM", exp, "put", 62.0, 0.20, 0.25),
        _quote("EEM", exp, "put", 68.0, 1.80, 1.85),
    ]
    prop = build_proposal(
        underlying="EEM",
        side=Side.BULLISH,
        invalidation=EEM_INV,
        chain=chain,
        width=5.0,
        min_credit_pct=0.20,
        today=today,
        dte_min=30,
        dte_max=45,
        min_short_inv_gap=1.0,
    )
    assert prop.skip is False
    assert prop.short.strike == 67.0
    assert prop.short.strike <= EEM_INV - 1.0 + 1e-9


def test_engine_blocks_bull_put_when_completed_daily_close_is_under_inv(tmp_path):
    """1h can reconfirm while the last *completed* daily close is already through inv.

    The forming session bar still closes above inv, so this is not the
    daily_structure_break arm cancel. The open itself must not happen.
    """
    engine, broker, journal, data = _engine(tmp_path, *hybrid_happy_daily_hourly())
    daily = data.daily_map["SPY"]
    prev = daily[-2]
    daily[-2] = Bar(
        ts=prev.ts,
        open=prev.open,
        high=prev.high,
        low=min(prev.low, 90.0),
        close=90.0,
        volume=prev.volume,
    )
    now = datetime(2026, 3, 4, 15, 0, tzinfo=UTC)
    completed = last_completed_daily_bar(daily, now)
    assert completed is not None
    assert completed.close == 90.0
    assert completed.ts == prev.ts

    result = engine.tick()
    assert result.proposals == []
    assert broker.proposed_opens == []
    assert broker.submitted_order_ids == []
    assert journal.open_spreads() == []
    skips = [p for k, _, p in _events(journal) if k == "entry_skip"]
    assert skips
    assert skips[0]["reason"] == UNDERWATER_OPEN_BLOCKED
    assert skips[0]["detail"] == DAILY_CLOSE_THROUGH_INV
    assert skips[0]["last_daily_close"] == 90.0
    # Arm may still exist: this gate blocks the open, it does not retune exits.
    assert journal.get_open_arm("SPY") is not None


def test_maybe_open_blocks_bear_call_when_daily_close_above_inv(tmp_path):
    engine, broker, journal, data = _engine(tmp_path, *hybrid_happy_daily_hourly())
    now = datetime(2026, 3, 4, 15, 0, tzinfo=UTC)
    data.daily_map["SPY"] = [
        Bar(
            ts=now + timedelta(hours=1),
            open=69.0,
            high=70.0,
            low=68.8,
            close=69.10,
            volume=1_000_000,
        )
    ]
    proposal = _openable_proposal(SpreadKind.BEAR_CALL_CREDIT, invalidation=EEM_INV, short=70.0)
    engine._maybe_open(proposal, [], now)
    assert broker.proposed_opens == []
    assert journal.open_spreads() == []
    skips = [p for k, _, p in _events(journal) if k == "entry_skip"]
    assert skips
    assert skips[0]["reason"] == UNDERWATER_OPEN_BLOCKED
    assert skips[0]["detail"] == DAILY_CLOSE_THROUGH_INV
    assert skips[0]["side"] == Side.BEARISH.value


def test_maybe_open_blocks_eem_bull_put_fill(tmp_path):
    """Fill-time gate: an already-built proposal must not become an observer fill."""
    engine, broker, journal, data = _engine(tmp_path, *hybrid_happy_daily_hourly())
    now = datetime(2026, 3, 4, 15, 0, tzinfo=UTC)
    data.daily_map["EEM"] = [
        Bar(
            ts=now + timedelta(hours=1),
            open=67.9,
            high=68.2,
            low=67.5,
            close=EEM_CLOSE,
            volume=1_000_000,
        )
    ]
    proposal = _openable_proposal(
        SpreadKind.BULL_PUT_CREDIT,
        invalidation=EEM_INV,
        short=EEM_SHORT,
        underlying="EEM",
    )
    engine._maybe_open(proposal, [], now)
    assert broker.proposed_opens == []
    assert journal.open_spreads() == []
    skips = [p for k, _, p in _events(journal) if k == "entry_skip"]
    assert skips[0]["detail"] == DAILY_CLOSE_THROUGH_INV
    assert skips[0]["last_daily_close"] == EEM_CLOSE


def test_engine_opens_when_completed_daily_holds_and_short_clears_gap(tmp_path):
    engine, broker, journal, _ = _engine(tmp_path, *hybrid_happy_daily_hourly())
    result = engine.tick()
    opened = [p for p in result.proposals if not p.skip]
    assert opened, [p.skip_reason for p in result.proposals]
    prop = opened[0]
    gap = 1.0
    assert prop.short.strike <= prop.invalidation - gap + 1e-9
    assert broker.proposed_opens
    assert journal.open_spreads()
    assert not any(k == "entry_skip" for k, _, _ in _events(journal))


def test_engine_skips_short_too_close_instead_of_opening(tmp_path):
    engine, broker, journal, _ = _engine(tmp_path, *hybrid_happy_daily_hourly())
    engine.cfg["spreads"]["min_short_inv_gap"] = 50.0
    result = engine.tick()
    assert broker.proposed_opens == []
    assert journal.open_spreads() == []
    assert result.proposals
    assert all(p.skip and p.skip_reason == SHORT_TOO_CLOSE_TO_INV for p in result.proposals)


def test_forming_daily_bar_does_not_hide_completed_close():
    now = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)  # 11:00 ET
    tuesday = datetime(2026, 9, 22, 13, 30, tzinfo=UTC)
    wednesday = datetime(2026, 9, 23, 13, 30, tzinfo=UTC)
    bars = [
        Bar(ts=tuesday, open=68.0, high=68.5, low=67.4, close=EEM_CLOSE, volume=1),
        Bar(ts=wednesday, open=68.2, high=68.8, low=67.9, close=68.6, volume=1),
    ]
    completed = last_completed_daily_bar(bars, now)
    assert completed is not None
    assert completed.ts == tuesday
    assert completed.close == EEM_CLOSE
    assert daily_close_through_invalidation(Side.BULLISH, EEM_INV, completed.close) is True


def _quote(root: str, exp: date, right: str, strike: float, bid: float, ask: float) -> ContractQuote:
    return ContractQuote(
        occ=occ_symbol(root, exp, right, strike),
        strike=strike,
        expiration=exp,
        right=right,
        bid=bid,
        ask=ask,
    )


def _openable_proposal(
    kind: SpreadKind,
    *,
    invalidation: float,
    short: float,
    underlying: str = "SPY",
) -> SpreadProposal:
    exp = date(2026, 10, 30)
    right = "put" if kind is SpreadKind.BULL_PUT_CREDIT else "call"
    long_k = short - 5.0 if kind is SpreadKind.BULL_PUT_CREDIT else short + 5.0
    return SpreadProposal(
        underlying=underlying,
        kind=kind,
        short=_quote(underlying, exp, right, short, 1.40, 1.45),
        long=_quote(underlying, exp, right, long_k, 0.20, 0.25),
        width=5.0,
        credit=1.15,
        qty=0,
        max_loss=0.0,
        invalidation=invalidation,
        reason="ok",
        skip=False,
    )
