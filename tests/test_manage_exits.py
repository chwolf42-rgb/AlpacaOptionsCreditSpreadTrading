"""Engine exit path: options-native mark/structure closes, fail-closed retry."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from alpaca_options_credit.broker.dry_run import DryRunBroker
from alpaca_options_credit.config import load_config
from alpaca_options_credit.engine import Engine
from alpaca_options_credit.journal import Journal
from alpaca_options_credit.models import (
    OpenSpread,
    SpreadKind,
    SpreadStatus,
)
from tests.helpers import FakeMarketData, bar


RTH = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)  # 10:00 ET
OFF_HOURS = datetime(2026, 3, 4, 22, 0, tzinfo=timezone.utc)  # 17:00 ET
NEXT_RTH = datetime(2026, 3, 5, 15, 0, tzinfo=timezone.utc)


def _events(journal: Journal) -> list[tuple[str, Optional[str], dict]]:
    con = sqlite3.connect(journal.path)
    rows = con.execute("SELECT kind, symbol, payload FROM events").fetchall()
    con.close()
    return [(k, s, json.loads(p)) for k, s, p in rows]


def _intact_daily(close: float = 110.0) -> list:
    return [bar(i, close + 1, close - 1, close, 800_000, step="day") for i in range(12)]


def _broken_daily(invalidation: float = 100.0) -> list:
    rows = _intact_daily(110.0)
    last_i = len(rows)
    rows.append(bar(last_i, invalidation + 1, invalidation - 2, invalidation - 1, 1_000_000, step="day"))
    return rows


class PaperBroker:
    """Paper-path broker: dry_run=False so empty/raised closes are failures."""

    dry_run = False

    def __init__(self, *, fails_left: int = 0, empty_once: bool = False):
        self.fails_left = fails_left
        self.empty_once = empty_once
        self.proposed_opens: list[dict[str, Any]] = []
        self.proposed_closes: list[dict[str, Any]] = []
        self.submitted_order_ids: list[str] = []
        self.cancel_calls: list[str] = []
        self.close_seq = 0
        self.positions: dict[str, int] = {}
        self.flattened_residuals: list[dict[str, Any]] = []

    def account_equity(self) -> float:
        return 100_000.0

    def account_number(self) -> Optional[str]:
        return "paper-test"

    def submit_open(self, proposal, payload):
        self.proposed_opens.append({"payload": payload})
        return "open-1"

    def submit_close(self, spread, payload):
        if self.fails_left > 0:
            self.fails_left -= 1
            raise RuntimeError("mleg reject")
        if self.empty_once:
            self.empty_once = False
            return None
        self.close_seq += 1
        oid = f"close-{self.close_seq}"
        self.proposed_closes.append(
            {"spread_id": spread.id, "payload": payload, "qty": payload.get("qty")}
        )
        self.submitted_order_ids.append(oid)
        return oid

    def open_order_ids(self) -> list[str]:
        return list(self.submitted_order_ids)

    def cancel_order(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)

    def option_positions(self) -> dict[str, int]:
        return dict(self.positions)

    def flatten_residual(self, occ: str, payload: dict[str, Any]) -> Optional[str]:
        self.flattened_residuals.append({"occ": occ, "payload": payload})
        return f"flatten-{occ}"


def _engine(
    tmp_path: Path,
    *,
    mark: float,
    daily=None,
    now: datetime = RTH,
    dry_run: bool = True,
    broker=None,
    qty: int = 2,
    credit: float = 1.20,
    invalidation: float = 100.0,
    kind: SpreadKind = SpreadKind.BULL_PUT_CREDIT,
    status: SpreadStatus = SpreadStatus.OPEN,
    exit_reason: str = "",
    exit_order_id: Optional[str] = None,
):
    cfg = load_config()
    cfg["bot"]["dry_run"] = dry_run
    cfg["bot"]["var_dir"] = str(tmp_path / "var")
    cfg["_repo_root"] = str(tmp_path)
    cfg["universe"]["symbols"] = ["SPY"]
    cfg["rth"]["scan_only_rth"] = True
    cfg["calendar"]["skip_fomc"] = False
    cfg["calendar"]["skip_earnings"] = False
    daily = daily if daily is not None else _intact_daily()
    data = FakeMarketData({"SPY": daily}, [], mark=mark, daily_map={"SPY": daily})
    broker = broker if broker is not None else DryRunBroker(equity=100_000)
    journal = Journal(tmp_path / "journal.sqlite")
    journal.upsert_spread(
        OpenSpread(
            id="sp1",
            underlying="SPY",
            kind=kind,
            short_occ="SPY260417P00100000",
            long_occ="SPY260417P00095000",
            width=5.0,
            credit=credit,
            qty=qty,
            max_loss=380.0,
            invalidation=invalidation,
            status=status,
            opened_at="2026-03-03T00:00:00+00:00",
            exit_reason=exit_reason,
            exit_order_id=exit_order_id,
            expiration="2026-04-17",
        )
    )
    now_box = {"t": now}

    class RecEngine(Engine):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.phase: list[str] = []

        def _manage_exits(self, *a, **k):
            self.phase.append("exits")
            return super()._manage_exits(*a, **k)

        def _scan_symbol(self, *a, **k):
            self.phase.append("scan")
            return super()._scan_symbol(*a, **k)

    engine = RecEngine(
        cfg,
        journal,
        broker,
        data,
        dry_run=dry_run,
        now_fn=lambda: now_box["t"],
        calendar={"fomc": [], "earnings": {}},
    )
    return engine, broker, journal, data, now_box


def test_take_profit_closes_spread(tmp_path):
    engine, broker, journal, _, _ = _engine(tmp_path, mark=0.60, credit=1.20)
    result = engine.tick()
    assert any(e.startswith("SPY:take_profit") for e in result.exits)
    assert journal.open_spreads() == []
    closed = journal.get_spread("sp1")
    assert closed is not None
    assert closed.status is SpreadStatus.CLOSED
    assert closed.exit_reason == "take_profit"
    assert broker.proposed_closes
    assert broker.proposed_closes[0]["payload"]["qty"] == "2"
    close_pl = broker.proposed_closes[0]["payload"]
    assert close_pl["order_class"] == "mleg"
    assert len(close_pl["legs"]) == 2
    intents = {leg["position_intent"] for leg in close_pl["legs"]}
    assert intents == {"buy_to_close", "sell_to_close"}
    assert engine.phase[0] == "exits"


def test_stop_2x_credit_closes_spread(tmp_path):
    engine, broker, journal, _, _ = _engine(tmp_path, mark=2.40, credit=1.20)
    result = engine.tick()
    assert any(e.startswith("SPY:stop_2x_credit") for e in result.exits)
    assert journal.open_spreads() == []
    assert journal.get_spread("sp1").exit_reason == "stop_2x_credit"
    assert broker.proposed_closes


def test_structure_break_closes_even_when_mark_quiet(tmp_path):
    engine, broker, journal, _, _ = _engine(
        tmp_path, mark=1.00, credit=1.20, daily=_broken_daily(100.0), invalidation=100.0
    )
    result = engine.tick()
    assert any(e.startswith("SPY:structure_break") for e in result.exits)
    assert journal.open_spreads() == []
    assert journal.get_spread("sp1").exit_reason == "structure_break"
    assert broker.proposed_closes


def test_failed_close_retries_and_alerts(tmp_path):
    broker = PaperBroker(fails_left=1)
    engine, broker, journal, data, _ = _engine(
        tmp_path, mark=2.40, credit=1.20, dry_run=False, broker=broker
    )
    first = engine.tick()
    assert any(e.endswith(":close_failed") for e in first.exits)
    live = journal.open_spreads()
    assert len(live) == 1
    assert live[0].status is SpreadStatus.EXITING
    assert live[0].exit_reason == "stop_2x_credit"
    assert live[0].close_attempts == 1
    assert live[0].last_close_error
    kinds = [k for k, _, _ in _events(journal)]
    assert "close_failed" in kinds
    assert "entry_blocked_unprotected_exit" in kinds
    assert broker.proposed_closes == []

    # Mark recovers — latched exit must still retry (fail-closed).
    data.mark = 0.90
    second = engine.tick()
    assert any(e == "SPY:stop_2x_credit" for e in second.exits)
    assert journal.open_spreads() == []
    assert journal.get_spread("sp1").status is SpreadStatus.CLOSED
    assert broker.proposed_closes
    assert broker.cancel_calls == []


def test_empty_broker_id_is_failed_close(tmp_path):
    broker = PaperBroker(empty_once=True)
    engine, broker, journal, _, _ = _engine(
        tmp_path, mark=0.60, credit=1.20, dry_run=False, broker=broker
    )
    engine.tick()
    live = journal.get_spread("sp1")
    assert live.status is SpreadStatus.EXITING
    assert "close_failed" in [k for k, _, _ in _events(journal)]
    engine.tick()
    assert journal.get_spread("sp1").status is SpreadStatus.CLOSED


def test_working_close_is_not_cancelled_or_replaced(tmp_path):
    broker = PaperBroker()
    broker.submitted_order_ids = ["exit-working"]
    engine, broker, journal, _, _ = _engine(
        tmp_path,
        mark=2.40,
        credit=1.20,
        dry_run=False,
        broker=broker,
        status=SpreadStatus.EXITING,
        exit_reason="stop_2x_credit",
        exit_order_id="exit-working",
    )
    result = engine.tick()
    assert any(e.endswith(":exit_working") for e in result.exits)
    assert broker.proposed_closes == []
    assert broker.cancel_calls == []
    live = journal.get_spread("sp1")
    assert live.status is SpreadStatus.EXITING
    assert live.exit_order_id == "exit-working"


def test_off_hours_flags_and_latches_without_submit(tmp_path):
    engine, broker, journal, _, now_box = _engine(
        tmp_path,
        mark=1.00,
        credit=1.20,
        daily=_broken_daily(100.0),
        now=OFF_HOURS,
        dry_run=False,
        broker=PaperBroker(),
    )
    result = engine.tick()
    assert result.status == "idle_off_hours"
    assert broker.proposed_closes == []
    assert any(e.endswith(":latched_off_hours") for e in result.exits)
    live = journal.get_spread("sp1")
    assert live.status is SpreadStatus.EXITING
    assert live.exit_reason == "structure_break"
    kinds = [k for k, _, _ in _events(journal)]
    assert kinds.count("overnight_open") == 1
    engine.tick()
    assert [k for k, _, _ in _events(journal)].count("overnight_open") == 1

    engine.phase.clear()
    now_box["t"] = NEXT_RTH
    second = engine.tick()
    assert second.status == "rth_scan"
    assert engine.phase[0] == "exits"
    assert broker.proposed_closes
    assert journal.get_spread("sp1").status is SpreadStatus.CLOSED


def test_rth_runs_exits_before_scan(tmp_path):
    engine, _, _, _, _ = _engine(tmp_path, mark=0.80, credit=1.20)
    engine.tick()
    assert engine.phase[0] == "exits"


def test_invalid_qty_does_not_invent_a_tranche(tmp_path):
    engine, broker, journal, _, _ = _engine(
        tmp_path, mark=2.40, credit=1.20, qty=0, dry_run=False, broker=PaperBroker()
    )
    result = engine.tick()
    assert any(e.endswith(":invalid_qty") for e in result.exits)
    assert broker.proposed_closes == []
    live = journal.get_spread("sp1")
    assert live.status is SpreadStatus.EXITING
    assert live.close_attempts >= 1


def test_naked_short_flatten_is_critical(tmp_path):
    broker = PaperBroker()
    broker.positions = {"SPY260417P00100000": -2}  # short only — long never filled
    engine, broker, journal, _, _ = _engine(
        tmp_path, mark=1.00, credit=1.20, dry_run=False, broker=broker
    )
    result = engine.tick()
    assert any(e == "SPY:naked_leg" for e in result.exits)
    assert broker.proposed_closes == []  # no 1-leg "spread close"
    assert broker.flattened_residuals
    flat = broker.flattened_residuals[0]
    assert flat["occ"] == "SPY260417P00100000"
    assert flat["payload"]["emergency_flatten"] is True
    assert flat["payload"]["position_intent"] == "buy_to_close"
    assert journal.get_spread("sp1").status is SpreadStatus.CLOSED
    assert journal.get_spread("sp1").exit_reason == "naked_leg"
    kinds = [k for k, _, _ in _events(journal)]
    assert "naked_leg_critical" in kinds
    assert broker.cancel_calls == []


def test_naked_short_off_hours_flags_without_flatten(tmp_path):
    broker = PaperBroker()
    broker.positions = {"SPY260417P00100000": -1}
    engine, broker, journal, _, _ = _engine(
        tmp_path,
        mark=1.00,
        credit=1.20,
        now=OFF_HOURS,
        dry_run=False,
        broker=broker,
    )
    result = engine.tick()
    assert any(e.endswith(":naked_leg:latched_off_hours") for e in result.exits)
    assert broker.flattened_residuals == []
    assert journal.get_spread("sp1").status is SpreadStatus.EXITING
    assert "naked_leg_critical" in [k for k, _, _ in _events(journal)]


def test_dry_run_cancel_order_is_hard_error():
    broker = DryRunBroker()
    with pytest.raises(RuntimeError, match="must not cancel"):
        broker.cancel_order("any")
