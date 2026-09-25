"""Engine exit path: options-native mark/structure closes, fail-closed retry."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from alpaca_options_credit.broker.dry_run import DryRunBroker
from alpaca_options_credit.close_prices import CloseOrderView
from alpaca_options_credit.config import load_config
from alpaca_options_credit.engine import Engine
from alpaca_options_credit.journal import Journal
from alpaca_options_credit.models import (
    Bar,
    OpenSpread,
    SpreadKind,
    SpreadStatus,
)
from tests.helpers import FakeMarketData


RTH = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)  # 10:00 ET
OFF_HOURS = datetime(2026, 3, 4, 22, 0, tzinfo=timezone.utc)  # 17:00 ET
NEXT_RTH = datetime(2026, 3, 5, 15, 0, tzinfo=timezone.utc)


def _events(journal: Journal) -> list[tuple[str, Optional[str], dict]]:
    con = sqlite3.connect(journal.path)
    rows = con.execute("SELECT kind, symbol, payload FROM events").fetchall()
    con.close()
    return [(k, s, json.loads(p)) for k, s, p in rows]


def _daily_ending(n: int, close: float, end: datetime) -> list[Bar]:
    start = end - timedelta(days=n - 1)
    return [
        Bar(
            ts=start + timedelta(days=i),
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=800_000,
        )
        for i in range(n)
    ]


def _intact_daily(close: float = 110.0) -> list:
    # Session before the engine clock (2026-03-04) so the freshness gate accepts it.
    end = datetime(2026, 3, 2, 21, 0, tzinfo=timezone.utc)
    return _daily_ending(12, close, end)


def _broken_daily(invalidation: float = 100.0) -> list:
    rows = _intact_daily(110.0)
    last = rows[-1]
    rows.append(
        Bar(
            ts=last.ts + timedelta(days=1),
            open=invalidation - 1,
            high=invalidation + 1,
            low=invalidation - 2,
            close=invalidation - 1,
            volume=1_000_000,
        )
    )
    return rows


def _ancient_broken_daily(invalidation: float = 100.0) -> list:
    """Same break geometry, but the tail is months before the engine clock."""
    end = datetime(2026, 1, 16, 21, 0, tzinfo=timezone.utc)
    rows = _daily_ending(12, 110.0, end)
    last = rows[-1]
    rows.append(
        Bar(
            ts=last.ts + timedelta(days=1),
            open=invalidation - 1,
            high=invalidation + 1,
            low=invalidation - 2,
            close=invalidation - 1,
            volume=1_000_000,
        )
    )
    return rows


class PaperBroker:
    """Paper-path broker: dry_run=False so empty/raised closes are failures.

    A successful submit is a fill when ``fill_immediately`` is set. The engine
    must journal ``filled_avg_price`` (``fill_debit`` or the limit) and the
    fill time, not the live quote. ``fill_plan`` yields
    ``(state, filled_qty, net_debit)`` per submit for partial-fill tests.
    """

    dry_run = False

    def __init__(
        self,
        *,
        fails_left: int = 0,
        empty_once: bool = False,
        fill_immediately: bool = True,
        fill_debit: Optional[float] = None,
        filled_at: str = "2026-03-04T15:00:05+00:00",
        fill_plan: Optional[list[tuple[str, int, Optional[float]]]] = None,
    ):
        self.fails_left = fails_left
        self.empty_once = empty_once
        self.fill_immediately = fill_immediately
        self.fill_debit = fill_debit
        self.filled_at = filled_at
        self.fill_plan = list(fill_plan or [])
        self.proposed_opens: list[dict[str, Any]] = []
        self.proposed_closes: list[dict[str, Any]] = []
        self.submitted_order_ids: list[str] = []
        self.cancel_calls: list[str] = []
        self.close_seq = 0
        self.positions: dict[str, int] = {}
        self.flattened_residuals: list[dict[str, Any]] = []
        self.orders: dict[str, CloseOrderView] = {}

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
        qty = int(payload.get("qty") or spread.qty)
        if self.fill_plan:
            state, filled_qty, debit = self.fill_plan.pop(0)
        elif self.fill_immediately:
            state, filled_qty, debit = "filled", qty, self.fill_debit
        else:
            state, filled_qty, debit = "open", 0, None
        if debit is None and state == "filled":
            debit = float(payload["limit_price"])
        self.orders[oid] = CloseOrderView(
            order_id=oid,
            state=state,
            filled_qty=int(filled_qty),
            order_qty=qty,
            net_debit=debit,
            filled_at=self.filled_at if state != "open" else None,
        )
        self.proposed_closes.append(
            {"spread_id": spread.id, "payload": payload, "qty": payload.get("qty")}
        )
        self.submitted_order_ids.append(oid)
        return oid

    def open_order_ids(self) -> list[str]:
        live = [oid for oid, view in self.orders.items() if view.state == "open"]
        for oid in self.submitted_order_ids:
            if oid not in self.orders and oid not in live:
                live.append(oid)
        return live

    def get_close_order(self, order_id: str) -> Optional[CloseOrderView]:
        return self.orders.get(order_id)

    def mark_filled(
        self,
        order_id: str,
        *,
        debit: Optional[float] = None,
        filled_at: Optional[str] = None,
    ) -> None:
        view = self.orders[order_id]
        self.orders[order_id] = CloseOrderView(
            order_id=order_id,
            state="filled",
            filled_qty=view.order_qty,
            order_qty=view.order_qty,
            net_debit=view.net_debit if debit is None else debit,
            filled_at=filled_at or self.filled_at,
        )

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
    assert closed.close_debit == pytest.approx(0.60)
    assert closed.closed_at == RTH.isoformat()
    assert closed.close_price_source == "quote"
    assert broker.proposed_closes
    assert broker.proposed_closes[0]["payload"]["qty"] == "2"
    close_pl = broker.proposed_closes[0]["payload"]
    assert close_pl["order_class"] == "mleg"
    assert len(close_pl["legs"]) == 2
    intents = {leg["position_intent"] for leg in close_pl["legs"]}
    assert intents == {"buy_to_close", "sell_to_close"}
    assert engine.phase[0] == "exits"


def test_stop_1_5x_credit_closes_spread(tmp_path):
    engine, broker, journal, _, _ = _engine(tmp_path, mark=1.80, credit=1.20)
    result = engine.tick()
    assert any(e.startswith("SPY:stop_credit") for e in result.exits)
    assert journal.open_spreads() == []
    closed = journal.get_spread("sp1")
    assert closed.exit_reason == "stop_credit"
    assert closed.close_debit == pytest.approx(1.80)
    assert closed.closed_at == RTH.isoformat()
    assert closed.close_price_source == "quote"
    assert broker.proposed_closes
    assert isinstance(broker, DryRunBroker)
    summary = journal.summarize_managed_outcomes(
        date(2026, 3, 4), date(2026, 3, 4), session="rth"
    )
    assert summary.n_wins == 0
    assert summary.n_losses == 1
    assert summary.win_rate == 0.0
    # (1.20 - 1.80) * qty 2 * 100
    assert summary.avg_loss == pytest.approx(-120.0)
    assert summary.avg_win is None


def test_mark_under_1_5x_does_not_stop(tmp_path):
    engine, broker, journal, _, _ = _engine(tmp_path, mark=1.79, credit=1.20)
    result = engine.tick()
    assert result.exits == []
    assert journal.open_spreads()
    assert journal.get_spread("sp1").status is SpreadStatus.OPEN
    assert broker.proposed_closes == []


def test_structure_break_closes_even_when_mark_quiet(tmp_path):
    engine, broker, journal, _, _ = _engine(
        tmp_path, mark=1.00, credit=1.20, daily=_broken_daily(100.0), invalidation=100.0
    )
    result = engine.tick()
    assert any(e.startswith("SPY:structure_break") for e in result.exits)
    assert journal.open_spreads() == []
    closed = journal.get_spread("sp1")
    assert closed.exit_reason == "structure_break"
    assert closed.close_debit == pytest.approx(1.00)
    assert closed.closed_at == RTH.isoformat()
    assert closed.close_price_source == "quote"
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
    assert live[0].exit_reason == "stop_credit"
    assert live[0].close_attempts == 1
    assert live[0].last_close_error
    kinds = [k for k, _, _ in _events(journal)]
    assert "close_failed" in kinds
    assert "entry_blocked_unprotected_exit" in kinds
    assert broker.proposed_closes == []

    # Mark recovers — latched exit must still retry (fail-closed).
    data.mark = 0.90
    second = engine.tick()
    assert any(e == "SPY:stop_credit" for e in second.exits)
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
        exit_reason="stop_credit",
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
    assert live.close_debit is None
    assert live.closed_at == ""
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
    closed = journal.get_spread("sp1")
    assert closed.status is SpreadStatus.CLOSED
    assert closed.close_debit == pytest.approx(1.00)
    assert closed.close_price_source == "fill"
    assert closed.closed_at


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
    naked = journal.get_spread("sp1")
    assert naked.status is SpreadStatus.CLOSED
    assert naked.exit_reason == "naked_leg"
    assert naked.close_debit == pytest.approx(1.00)
    assert naked.closed_at == RTH.isoformat()
    assert naked.close_price_source == "quote"
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


def test_stale_daily_does_not_structure_exit(tmp_path):
    engine, broker, journal, _, _ = _engine(
        tmp_path,
        mark=1.00,
        credit=1.20,
        daily=_ancient_broken_daily(100.0),
        invalidation=100.0,
    )
    result = engine.tick()
    assert not any("structure_break" in e for e in result.exits)
    assert broker.proposed_closes == []
    live = journal.get_spread("sp1")
    assert live.status is SpreadStatus.OPEN
    assert live.exit_reason == ""
    stale = [p for k, _, p in _events(journal) if p.get("reason") == "stale_bars"]
    assert stale
    assert stale[0]["detail"].startswith("1Day:")


def test_stale_structure_bars_still_take_profit(tmp_path):
    ancient = _ancient_broken_daily(100.0)
    # Quiet the last close so this is a mark exit, not a structure break.
    last = ancient[-1]
    ancient[-1] = last.__class__(
        ts=last.ts,
        open=110.0,
        high=111.0,
        low=109.0,
        close=110.0,
        volume=last.volume,
    )
    engine, broker, journal, _, _ = _engine(
        tmp_path, mark=0.60, credit=1.20, daily=ancient, invalidation=100.0
    )
    result = engine.tick()
    assert any(e.startswith("SPY:take_profit") for e in result.exits)
    assert journal.get_spread("sp1").exit_reason == "take_profit"
    assert broker.proposed_closes


def test_dry_run_cancel_order_is_hard_error():
    broker = DryRunBroker()
    with pytest.raises(RuntimeError, match="must not cancel"):
        broker.cancel_order("any")


def test_structure_break_without_mark_records_missing_price(tmp_path, caplog):
    engine, _, journal, _, _ = _engine(
        tmp_path,
        mark=None,
        credit=1.20,
        daily=_broken_daily(100.0),
        invalidation=100.0,
    )
    with caplog.at_level("WARNING"):
        result = engine.tick()
    assert any(e.startswith("SPY:structure_break") for e in result.exits)
    closed = journal.get_spread("sp1")
    assert closed.status is SpreadStatus.CLOSED
    assert closed.close_debit is None
    assert closed.closed_at == RTH.isoformat()
    assert closed.close_price_source == "missing"
    kinds = [k for k, _, _ in _events(journal)]
    assert "close_price_missing" in kinds
    assert "close price missing" in caplog.text
    summary = journal.summarize_managed_outcomes(
        date(2026, 3, 4), date(2026, 3, 4), session="rth"
    )
    assert summary.n_losses == 1
    assert summary.n_missing_price == 1
    assert summary.avg_loss is None
    assert summary.pnl is None


def test_live_fill_records_broker_debit_not_quote(tmp_path):
    fill_at = "2026-03-04T15:00:05+00:00"
    broker = PaperBroker(fill_immediately=False, fill_debit=2.25, filled_at=fill_at)
    engine, broker, journal, data, _ = _engine(
        tmp_path, mark=1.80, credit=1.20, dry_run=False, broker=broker
    )
    first = engine.tick()
    assert any(e.endswith(":exit_working") for e in first.exits)
    pending = journal.get_spread("sp1")
    assert pending.status is SpreadStatus.EXITING
    assert pending.close_debit is None
    assert pending.exit_order_id
    assert broker.proposed_closes
    data.mark = 0.10
    broker.mark_filled(pending.exit_order_id, debit=2.25, filled_at=fill_at)
    second = engine.tick()
    assert any(e == "SPY:stop_credit" for e in second.exits)
    closed = journal.get_spread("sp1")
    assert closed.status is SpreadStatus.CLOSED
    assert closed.close_debit == pytest.approx(2.25)
    assert closed.closed_at == fill_at
    assert closed.close_price_source == "fill"
    assert len(broker.proposed_closes) == 1


def test_live_partial_fill_retries_remainder_and_averages_debit(tmp_path):
    broker = PaperBroker(
        fill_plan=[
            ("partial", 1, 1.50),
            ("filled", 1, 1.70),
        ]
    )
    engine, broker, journal, _, _ = _engine(
        tmp_path, mark=2.40, credit=1.20, qty=2, dry_run=False, broker=broker
    )
    first = engine.tick()
    assert any(e.endswith(":partial_fill") for e in first.exits)
    mid = journal.get_spread("sp1")
    assert mid.status is SpreadStatus.EXITING
    assert mid.close_attempts == 1
    assert mid.close_filled_qty == 1
    assert mid.close_debit is None
    assert mid.exit_order_id is None
    assert broker.proposed_closes[0]["payload"]["qty"] == "2"
    assert "close_partial" in [k for k, _, _ in _events(journal)]

    second = engine.tick()
    assert any(e == "SPY:stop_credit" for e in second.exits)
    closed = journal.get_spread("sp1")
    assert closed.status is SpreadStatus.CLOSED
    assert closed.close_debit == pytest.approx(1.60)
    assert closed.close_price_source == "fill"
    assert closed.closed_at
    assert broker.proposed_closes[1]["payload"]["qty"] == "1"
    assert closed.close_attempts == 1


def test_working_partial_is_not_replaced(tmp_path):
    broker = PaperBroker(fill_immediately=False, fill_debit=1.60)
    engine, broker, journal, _, _ = _engine(
        tmp_path, mark=2.40, credit=1.20, qty=2, dry_run=False, broker=broker
    )
    engine.tick()
    live = journal.get_spread("sp1")
    oid = live.exit_order_id
    broker.orders[oid] = CloseOrderView(
        order_id=oid,
        state="open",
        filled_qty=1,
        order_qty=2,
        net_debit=1.50,
        filled_at=None,
    )
    again = engine.tick()
    assert any(e.endswith(":exit_working") for e in again.exits)
    held = journal.get_spread("sp1")
    assert held.status is SpreadStatus.EXITING
    assert held.close_attempts == 0
    assert held.close_filled_qty == 1
    assert len(broker.proposed_closes) == 1
    broker.mark_filled(oid, debit=1.60)
    engine.tick()
    closed = journal.get_spread("sp1")
    assert closed.status is SpreadStatus.CLOSED
    assert closed.close_debit == pytest.approx(1.60)
    assert closed.close_price_source == "fill"


def test_dead_close_order_retries_on_the_next_poll(tmp_path):
    broker = PaperBroker(
        fill_plan=[
            ("dead", 0, None),
            ("filled", 2, 1.80),
        ]
    )
    engine, broker, journal, _, _ = _engine(
        tmp_path, mark=2.40, credit=1.20, qty=2, dry_run=False, broker=broker
    )
    first = engine.tick()
    assert any(e.endswith(":close_failed") for e in first.exits)
    live = journal.get_spread("sp1")
    assert live.status is SpreadStatus.EXITING
    assert live.close_attempts == 1
    assert live.close_debit is None
    assert "close_failed" in [k for k, _, _ in _events(journal)]
    second = engine.tick()
    assert any(e == "SPY:stop_credit" for e in second.exits)
    closed = journal.get_spread("sp1")
    assert closed.status is SpreadStatus.CLOSED
    assert closed.close_debit == pytest.approx(1.80)
    assert closed.close_price_source == "fill"
