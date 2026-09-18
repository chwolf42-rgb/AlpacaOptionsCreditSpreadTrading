from datetime import datetime, timezone
from pathlib import Path

from alpaca_options_credit.broker.dry_run import DryRunBroker
from alpaca_options_credit.config import load_config
from alpaca_options_credit.engine import Engine
from alpaca_options_credit.journal import Journal
from alpaca_options_credit.models import (
    OpenSpread,
    SpreadKind,
    SpreadStatus,
)
from tests.helpers import FakeMarketData, bullish_confirm_pullback_bars, listed_chain
from datetime import date, timedelta


def test_engine_one_per_symbol_blocks_second_open(tmp_path: Path):
    cfg = load_config()
    cfg["bot"]["dry_run"] = True
    cfg["rth"]["scan_only_rth"] = False
    cfg["calendar"]["skip_fomc"] = False
    cfg["calendar"]["skip_earnings"] = False
    cfg["universe"]["symbols"] = ["SPY"]
    cfg["_repo_root"] = str(tmp_path)
    journal = Journal(tmp_path / "j.sqlite")
    journal.upsert_spread(
        OpenSpread(
            id="existing",
            underlying="SPY",
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
    )
    bars = bullish_confirm_pullback_bars()
    exp = date(2026, 3, 3) + timedelta(days=37)
    chain = listed_chain("SPY", "put", exp, [95, 100, 105], bid=1.4, ask=1.45)
    data = FakeMarketData({"SPY": bars}, chain)
    broker = DryRunBroker(100_000)
    engine = Engine(
        cfg,
        journal,
        broker,
        data,
        dry_run=True,
        now_fn=lambda: datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc),
        calendar={"fomc": [], "earnings": {}},
    )
    engine.tick()
    assert broker.proposed_opens == []
    assert broker.submitted_order_ids == []
    assert len(journal.open_spreads()) == 1
