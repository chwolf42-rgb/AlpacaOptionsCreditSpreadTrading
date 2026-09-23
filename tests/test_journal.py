from datetime import datetime, timezone
from pathlib import Path

from alpaca_options_credit.journal import Journal
from alpaca_options_credit.models import Arm, ArmStatus, OpenSpread, Side, SpreadKind, SpreadStatus


def test_journal_persists_arms_and_spreads(tmp_path: Path):
    path = tmp_path / "j.sqlite"
    j = Journal(path)
    arm = Arm(
        id="arm1",
        symbol="SPY",
        side=Side.BULLISH,
        invalidation=100.0,
        zone_low=108.0,
        zone_high=110.0,
        confirmed_at=datetime.now(timezone.utc).isoformat(),
        status=ArmStatus.ARMED,
        reason="test",
    )
    j.upsert_arm(arm)
    j.log_event("arm", "SPY", {"ok": True})

    spread = OpenSpread(
        id="sp1",
        underlying="SPY",
        kind=SpreadKind.BULL_PUT_CREDIT,
        short_occ="A",
        long_occ="B",
        width=5.0,
        credit=1.2,
        qty=1,
        max_loss=380.0,
        invalidation=100.0,
        status=SpreadStatus.OPEN,
        opened_at=datetime.now(timezone.utc).isoformat(),
    )
    j.upsert_spread(spread)

    j2 = Journal(path)
    loaded = j2.get_open_arm("SPY")
    assert loaded is not None
    assert loaded.invalidation == 100.0
    opens = j2.open_spreads()
    assert len(opens) == 1
    assert opens[0].credit == 1.2

    j2.close_spread("sp1", "take_profit")
    assert j2.open_spreads() == []


def test_journal_latch_keeps_spread_live_until_close(tmp_path: Path):
    j = Journal(tmp_path / "j.sqlite")
    j.upsert_spread(
        OpenSpread(
            id="sp1",
            underlying="SPY",
            kind=SpreadKind.BULL_PUT_CREDIT,
            short_occ="A",
            long_occ="B",
            width=5.0,
            credit=1.2,
            qty=2,
            max_loss=380.0,
            invalidation=100.0,
            status=SpreadStatus.OPEN,
            opened_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    j.latch_exit("sp1", "stop_credit")
    live = j.open_spreads()
    assert len(live) == 1
    assert live[0].status is SpreadStatus.EXITING
    assert live[0].exit_reason == "stop_credit"
    j.latch_exit("sp1", "take_profit")  # first reason wins
    assert j.get_spread("sp1").exit_reason == "stop_credit"
    attempts = j.record_close_failure("sp1", "mleg reject")
    assert attempts == 1
    assert j.get_spread("sp1").last_close_error == "mleg reject"
    j.record_working_close("sp1", "close-9")
    assert j.get_spread("sp1").exit_order_id == "close-9"
    j.close_spread("sp1", "stop_credit", close_debit=1.80)
    assert j.open_spreads() == []
