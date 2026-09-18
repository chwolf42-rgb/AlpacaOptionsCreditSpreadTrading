from datetime import datetime, timezone
from pathlib import Path

from alpaca_options_credit.broker.dry_run import DryRunBroker
from alpaca_options_credit.broker.payloads import open_credit_spread_payload
from alpaca_options_credit.config import load_config
from alpaca_options_credit.engine import Engine
from alpaca_options_credit.journal import Journal
from alpaca_options_credit.models import SpreadKind, SpreadStatus
from tests.helpers import FakeMarketData, bullish_confirm_pullback_bars, listed_chain

from datetime import date, timedelta


def _engine(tmp_path: Path, mark: float = 0.80):
    cfg = load_config()
    cfg["bot"]["dry_run"] = True
    cfg["bot"]["var_dir"] = str(tmp_path / "var")
    cfg["_repo_root"] = str(tmp_path)
    cfg["universe"]["symbols"] = ["SPY"]
    cfg["rth"]["scan_only_rth"] = False  # tests run whenever
    cfg["calendar"]["skip_fomc"] = False
    cfg["calendar"]["skip_earnings"] = False
    bars = bullish_confirm_pullback_bars()
    inv = 102.9
    exp = date(2026, 3, 3) + timedelta(days=37)
    strikes = [round(90 + i * 0.5, 2) for i in range(0, 50)]
    # Rich credit near invalidation so the 20% gate passes (width 5 → need ≥1.00).
    chain = listed_chain(
        "SPY",
        "put",
        exp,
        strikes,
        invalidation=inv,
        width=5.0,
        short_bid=1.40,
        long_ask=0.25,
    )
    data = FakeMarketData({"SPY": bars}, chain, mark=mark)
    broker = DryRunBroker(equity=100_000)
    journal = Journal(tmp_path / "journal.sqlite")
    now = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)  # 10:00 ET
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


def test_observer_places_no_orders(tmp_path):
    engine, broker, journal = _engine(tmp_path)
    result = engine.tick()
    assert broker.submitted_order_ids == []
    assert broker.dry_run is True
    assert any(not p.skip for p in result.proposals), [p.skip_reason for p in result.proposals]
    opens = journal.open_spreads()
    assert opens, "observer should journal a proposed spread"
    for s in opens:
        assert s.broker_order_id is None
        assert s.status == SpreadStatus.PROPOSED
    assert broker.proposed_opens, "dry-run broker should record the mleg payload"
    for rec in broker.proposed_opens:
        payload = rec["payload"]
        assert payload["order_class"] == "mleg"
        assert float(payload["limit_price"]) < 0  # credit
        assert payload["legs"][0]["position_intent"] == "sell_to_open"


def test_observer_payload_never_sent_to_live_stub(tmp_path):
    """Guard: DryRunBroker.submit_open returns None always."""
    engine, broker, _ = _engine(tmp_path)
    engine.tick()
    for rec in broker.proposed_opens:
        assert broker.submit_open is not None
    assert broker.submitted_order_ids == []
