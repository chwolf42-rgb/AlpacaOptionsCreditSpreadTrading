import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from alpaca_options_credit.broker.alpaca import parse_open_interest
from alpaca_options_credit.broker.dry_run import DryRunBroker
from alpaca_options_credit.config import load_config
from alpaca_options_credit.engine import Engine
from alpaca_options_credit.journal import Journal
from alpaca_options_credit.models import ContractQuote, Side, SpreadKind
from alpaca_options_credit.strategy.spreads import (
    CREDIT_BELOW_MIN_PCT,
    CREDIT_DEBIT,
    PRE_PROPOSAL_SKIP_REASONS,
    QUOTE_ABSURD,
    QUOTE_CROSSED,
    QUOTE_MISSING,
    QUOTE_THIN_OI,
    QUOTE_WIDE,
    build_proposal,
    credit_meets_width_gate,
    long_strike_for,
    mid_credit,
    natural_credit,
    occ_symbol,
    pick_short_strike,
)
from alpaca_options_credit.strategy.structure import hybrid_entry
from tests.helpers import FakeMarketData, align_bars_to, hybrid_happy_daily_hourly


def test_default_credit_filters_keep_dry_run_and_width_floor():
    cfg = load_config()
    sp = cfg["spreads"]
    assert cfg["bot"]["dry_run"] is True
    assert sp["min_credit_pct_of_width"] == 0.20
    assert cfg["exits"]["take_profit_frac_of_credit"] == 0.50
    assert cfg["exits"]["stop_multiple_of_credit"] == 1.5
    assert sp["credit_from"] == "natural"
    assert sp["max_credit_pct_of_width"] == 1.0
    assert sp["max_leg_spread_pct_of_mid"] == 0
    assert sp["min_open_interest"] == 0
    assert sp["min_short_inv_gap"] == 1.0


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
    assert prop.skip_reason == CREDIT_BELOW_MIN_PCT
    assert prop.reason == CREDIT_BELOW_MIN_PCT
    assert natural_credit(prop.short, prop.long) == pytest.approx(0.40)


def _put(exp, strike, bid, ask, oi=None) -> ContractQuote:
    return ContractQuote(
        occ=occ_symbol("SPY", exp, "put", strike),
        strike=strike,
        expiration=exp,
        right="put",
        bid=bid,
        ask=ask,
        open_interest=oi,
    )


def _propose(short, long, **kwargs):
    today = date(2026, 3, 3)
    exp = today + timedelta(days=37)
    chain = [
        _put(exp, 100.0, short.bid, short.ask, short.open_interest),
        _put(exp, 95.0, long.bid, long.ask, long.open_interest),
    ]
    params = dict(
        underlying="SPY",
        side=Side.BULLISH,
        invalidation=100.0,
        chain=chain,
        width=5.0,
        min_credit_pct=0.20,
        today=today,
        dte_min=30,
        dte_max=45,
    )
    params.update(kwargs)
    return build_proposal(**params)


def test_debit_natural_is_credit_debit_not_width_gate():
    """AMAT-style: natural credit negative, still inside the $5 width."""
    prop = _propose(
        _put(date(2026, 3, 3), 100, 0.50, 0.60),
        _put(date(2026, 3, 3), 95, 0.20, 1.93),
    )
    assert prop.skip is True
    assert prop.skip_reason == CREDIT_DEBIT
    assert prop.credit == pytest.approx(0.50 - 1.93)
    assert prop.skip_reason in PRE_PROPOSAL_SKIP_REASONS


def test_positive_mid_with_debit_natural_still_skips():
    """Wide markets can print a credit mid while the fillable natural is a debit."""
    short = _put(date(2026, 3, 3), 100, 0.40, 2.00)
    long = _put(date(2026, 3, 3), 95, 0.10, 1.20)
    assert mid_credit(short, long) == pytest.approx((0.40 + 2.00) / 2 - (0.10 + 1.20) / 2)
    assert mid_credit(short, long) > 0
    assert natural_credit(short, long) < 0
    prop = _propose(short, long)
    assert prop.skip_reason == CREDIT_DEBIT


def test_crossed_leg_fails_before_credit_gate():
    """Crossed short bid would otherwise clear the 20% floor. Must not propose."""
    prop = _propose(
        _put(date(2026, 3, 3), 100, 3.00, 0.10),
        _put(date(2026, 3, 3), 95, 0.20, 0.25),
    )
    assert natural_credit(prop.short, prop.long) == pytest.approx(2.75)
    assert credit_meets_width_gate(prop.credit, 5.0, 0.20) is True
    assert prop.skip is True
    assert prop.skip_reason == QUOTE_CROSSED


def test_crossed_beats_debit_when_both_apply():
    prop = _propose(
        _put(date(2026, 3, 3), 100, 2.00, 0.50),
        _put(date(2026, 3, 3), 95, 0.20, 3.00),
    )
    assert prop.credit < 0
    assert prop.skip_reason == QUOTE_CROSSED


def test_missing_quotes_beat_zero_credit():
    prop = _propose(
        _put(date(2026, 3, 3), 100, 0.0, 0.0),
        _put(date(2026, 3, 3), 95, 0.20, 0.30),
    )
    assert prop.skip_reason == QUOTE_MISSING


def test_one_sided_quote_is_missing():
    prop = _propose(
        _put(date(2026, 3, 3), 100, 1.40, 0.0),
        _put(date(2026, 3, 3), 95, 0.20, 0.25),
    )
    assert prop.skip_reason == QUOTE_MISSING


def test_negative_price_is_absurd():
    prop = _propose(
        _put(date(2026, 3, 3), 100, -0.10, 0.40),
        _put(date(2026, 3, 3), 95, 0.20, 0.25),
    )
    assert prop.skip_reason == QUOTE_ABSURD


def test_credit_at_or_above_width_is_absurd():
    prop = _propose(
        _put(date(2026, 3, 3), 100, 6.00, 6.10),
        _put(date(2026, 3, 3), 95, 0.20, 0.30),
    )
    assert prop.credit == pytest.approx(5.70)
    assert prop.skip_reason == QUOTE_ABSURD


def test_debit_beyond_width_is_absurd_not_debit():
    prop = _propose(
        _put(date(2026, 3, 3), 100, 0.10, 0.20),
        _put(date(2026, 3, 3), 95, 0.05, 6.00),
    )
    assert prop.credit == pytest.approx(0.10 - 6.00)
    assert prop.skip_reason == QUOTE_ABSURD


def test_credit_just_inside_width_still_passes_floor():
    prop = _propose(
        _put(date(2026, 3, 3), 100, 4.90, 5.00),
        _put(date(2026, 3, 3), 95, 0.05, 0.10),
    )
    assert prop.skip is False
    assert prop.credit == pytest.approx(4.80)
    assert prop.skip_reason == ""


def test_wide_leg_is_optional_and_config_gated():
    short = _put(date(2026, 3, 3), 100, 1.20, 4.00)
    long = _put(date(2026, 3, 3), 95, 0.10, 0.20)
    open_ = _propose(short, long)
    assert open_.skip is False
    wide = _propose(short, long, max_leg_spread_pct_of_mid=0.50)
    assert wide.skip_reason == QUOTE_WIDE


def test_thin_open_interest_is_optional():
    short = _put(date(2026, 3, 3), 100, 1.40, 1.50, oi=10)
    long = _put(date(2026, 3, 3), 95, 0.20, 0.25, oi=10)
    assert _propose(short, long).skip is False
    thin = _propose(short, long, min_open_interest=50)
    assert thin.skip_reason == QUOTE_THIN_OI
    unknown = _propose(
        _put(date(2026, 3, 3), 100, 1.40, 1.50),
        _put(date(2026, 3, 3), 95, 0.20, 0.25),
        min_open_interest=1,
    )
    assert unknown.skip_reason == QUOTE_THIN_OI


def test_parse_open_interest():
    assert parse_open_interest(None) is None
    assert parse_open_interest("") is None
    assert parse_open_interest("250") == 250
    assert parse_open_interest(12.0) == 12
    assert parse_open_interest(-1) is None
    assert parse_open_interest("n/a") is None


def _events(journal: Journal):
    con = sqlite3.connect(journal.path)
    rows = con.execute("SELECT kind, symbol, payload FROM events").fetchall()
    con.close()
    return [(k, s, json.loads(p)) for k, s, p in rows]


def _ready_engine(tmp_path: Path, chain):
    cfg = load_config()
    cfg["bot"]["dry_run"] = True
    cfg["bot"]["var_dir"] = str(tmp_path / "var")
    cfg["_repo_root"] = str(tmp_path)
    cfg["universe"]["symbols"] = ["SPY"]
    cfg["rth"]["scan_only_rth"] = False
    cfg["calendar"]["skip_fomc"] = False
    cfg["calendar"]["skip_earnings"] = False
    daily, hourly = hybrid_happy_daily_hourly()
    now = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)
    daily, hourly = align_bars_to(daily, hourly, end=now - timedelta(hours=1))
    data = FakeMarketData({"SPY": hourly}, chain, mark=0.80, daily_map={"SPY": daily})
    broker = DryRunBroker(equity=100_000)
    journal = Journal(tmp_path / "journal.sqlite")
    engine = Engine(
        cfg,
        journal,
        broker,
        data,
        dry_run=True,
        now_fn=lambda: now,
        calendar={"fomc": [], "earnings": {}},
    )
    return engine, broker, journal


def _inv_chain(short_ba, long_ba):
    """Price only the short/long the engine will pick for the happy-path arm."""
    daily, hourly = hybrid_happy_daily_hourly()
    view, ready, _why = hybrid_entry(daily, hourly)
    assert ready, view.reason
    inv = float(view.invalidation)
    exp = date(2026, 3, 3) + timedelta(days=37)
    width = 5.0
    strikes = [round(90 + i * 0.5, 2) for i in range(0, 50)]
    gap = float(load_config()["spreads"]["min_short_inv_gap"])
    short_k = pick_short_strike(inv, strikes, adverse="down", min_short_inv_gap=gap)
    assert short_k is not None
    long_k = long_strike_for(SpreadKind.BULL_PUT_CREDIT, short_k, width)
    out = []
    for k in strikes:
        if abs(k - short_k) < 1e-9:
            b, a = short_ba
        elif abs(k - long_k) < 1e-9:
            b, a = long_ba
        else:
            b, a = (0.20, 0.25)
        out.append(
            ContractQuote(
                occ=occ_symbol("SPY", exp, "put", k),
                strike=k,
                expiration=exp,
                right="put",
                bid=b,
                ask=a,
            )
        )
    return out


def test_engine_debit_does_not_reach_proposal_path(tmp_path):
    engine, broker, journal = _ready_engine(
        tmp_path, _inv_chain((0.50, 0.60), (0.20, 1.93))
    )
    result = engine.tick()
    assert engine.dry_run is True
    assert result.proposals == []
    assert broker.submitted_order_ids == []
    assert broker.proposed_opens == []
    events = _events(journal)
    assert not any(k == "proposal" for k, _, _ in events)
    skips = [(k, p) for k, _, p in events if k in {"credit_skip", "quote_skip"}]
    assert skips
    assert skips[0][0] == "credit_skip"
    assert skips[0][1]["skip_reason"] == CREDIT_DEBIT
    assert skips[0][1]["credit"] == pytest.approx(0.50 - 1.93)


def test_engine_crossed_quote_does_not_reach_proposal_path(tmp_path):
    engine, broker, journal = _ready_engine(
        tmp_path, _inv_chain((3.00, 0.10), (0.20, 0.25))
    )
    result = engine.tick()
    assert result.proposals == []
    assert broker.proposed_opens == []
    events = _events(journal)
    assert not any(k == "proposal" for k, _, _ in events)
    skips = [p for k, _, p in events if k == "quote_skip"]
    assert skips and skips[0]["skip_reason"] == QUOTE_CROSSED


def test_engine_thin_credit_is_credit_skip_not_proposal(tmp_path):
    engine, broker, journal = _ready_engine(
        tmp_path, _inv_chain((0.70, 0.80), (0.20, 0.30))
    )
    result = engine.tick()
    assert result.proposals == []
    assert broker.submitted_order_ids == []
    events = _events(journal)
    assert not any(k == "proposal" for k, _, _ in events)
    skips = [p for k, _, p in events if k == "credit_skip"]
    assert skips and skips[0]["skip_reason"] == CREDIT_BELOW_MIN_PCT
    assert skips[0]["credit"] == pytest.approx(0.40)
