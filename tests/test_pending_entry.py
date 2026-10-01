"""Live entries stay pending until the day mleg fills.

A journaled OPEN spread is a position. A working, expired, or rejected
entry is not: it must not be closed, flattened, or flagged overnight, and
it must free the risk slot when the order dies with no fill.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from alpaca_options_credit.close_prices import (
    EntryOrderView,
    entry_order_view_from_broker_order,
)
from alpaca_options_credit.config import load_config
from alpaca_options_credit.engine import Engine
from alpaca_options_credit.journal import Journal
from alpaca_options_credit.models import (
    Arm,
    ArmStatus,
    Bar,
    ContractQuote,
    OpenSpread,
    Side,
    SpreadKind,
    SpreadProposal,
    SpreadStatus,
)
from alpaca_options_credit.risk import decide
from tests.helpers import FakeMarketData


RTH = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)  # 10:00 ET
OFF_HOURS = datetime(2026, 3, 4, 22, 0, tzinfo=timezone.utc)  # 17:00 ET
SHORT = "MSFT261106P00490000"
LONG = "MSFT261106P00485000"
LIMIT = 1.12


def _events(journal: Journal) -> list[tuple[str, Optional[str], dict]]:
    con = sqlite3.connect(journal.path)
    rows = con.execute(
        "SELECT kind, symbol, payload FROM events ORDER BY id"
    ).fetchall()
    con.close()
    return [(k, s, json.loads(p)) for k, s, p in rows]


def _kinds(journal: Journal) -> list[str]:
    return [k for k, _s, _p in _events(journal)]


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


def _intact_daily(close: float = 110.0) -> list[Bar]:
    end = datetime(2026, 3, 2, 21, 0, tzinfo=timezone.utc)
    return _daily_ending(12, close, end)


def _broken_daily(invalidation: float = 100.0) -> list[Bar]:
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


def _ancient_broken_daily(invalidation: float = 100.0) -> list[Bar]:
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


def _view(
    order_id: str,
    *,
    state: str,
    raw_status: str,
    filled_qty: int = 0,
    order_qty: int = 2,
    credit: Optional[float] = None,
) -> EntryOrderView:
    return EntryOrderView(
        order_id=order_id,
        state=state,
        raw_status=raw_status,
        filled_qty=filled_qty,
        order_qty=order_qty,
        filled_avg_credit=credit,
        filled_at="2026-03-04T15:01:00+00:00" if filled_qty else None,
    )


class EntryBroker:
    """Paper broker double. Working closes are not cancelled here."""

    dry_run = False

    def __init__(self, *, positions_unknown: bool = False):
        self.orders: dict[str, EntryOrderView] = {}
        self.positions: dict[str, int] = {}
        self.positions_unknown = positions_unknown
        self.opens: list[dict[str, Any]] = []
        self.closes: list[dict[str, Any]] = []
        self.cancels: list[str] = []
        self.flattened: list[str] = []
        self.cancel_error: Optional[Exception] = None
        self.submit_error: Optional[Exception] = None
        self.submit_empty = False
        self._seq = 0

    def account_equity(self) -> float:
        return 100_000.0

    def account_number(self) -> Optional[str]:
        return "paper-test"

    def submit_open(self, proposal, payload):
        if self.submit_error is not None:
            raise self.submit_error
        if self.submit_empty:
            return None
        self._seq += 1
        oid = f"entry-{self._seq}"
        self.opens.append(
            {
                "id": oid,
                "payload": payload,
                "credit": proposal.credit,
                "qty": proposal.qty,
            }
        )
        self.orders[oid] = _view(
            oid,
            state="open",
            raw_status="new",
            order_qty=int(proposal.qty),
        )
        return oid

    def submit_close(self, spread, payload):
        self.closes.append(
            {
                "spread_id": spread.id,
                "qty": payload.get("qty"),
                "payload": payload,
            }
        )
        return f"close-{spread.id}"

    def open_order_ids(self) -> list[str]:
        return [oid for oid, view in self.orders.items() if view.state == "open"]

    def get_entry_order(self, order_id: str) -> Optional[EntryOrderView]:
        return self.orders.get(order_id)

    def cancel_entry_order(self, order_id: str) -> None:
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancels.append(order_id)
        view = self.orders.get(order_id)
        if view is None:
            return
        self.orders[order_id] = _view(
            order_id,
            state="partial" if view.filled_qty > 0 else "dead",
            raw_status="canceled",
            filled_qty=view.filled_qty,
            order_qty=view.order_qty,
            credit=view.filled_avg_credit,
        )

    def cancel_order(self, order_id: str) -> None:
        raise RuntimeError(f"working exits must not be cancelled ({order_id})")

    def option_positions(self) -> Optional[dict[str, int]]:
        if self.positions_unknown:
            return None
        return dict(self.positions)

    def flatten_residual(self, occ: str, payload: dict[str, Any]) -> Optional[str]:
        self.flattened.append(occ)
        return f"flat-{occ}"


def _engine(
    tmp_path: Path,
    *,
    broker: Optional[EntryBroker] = None,
    daily: Optional[dict[str, list[Bar]]] = None,
    now: datetime = RTH,
    mark: float = 0.90,
):
    cfg = load_config()
    cfg["bot"]["dry_run"] = False
    cfg["bot"]["var_dir"] = str(tmp_path / "var")
    cfg["_repo_root"] = str(tmp_path)
    cfg["universe"]["symbols"] = []
    cfg["rth"]["scan_only_rth"] = True
    cfg["calendar"]["skip_fomc"] = False
    cfg["calendar"]["skip_earnings"] = False
    daily = daily if daily is not None else {"MSFT": _intact_daily(), "SPY": _intact_daily()}
    data = FakeMarketData(daily, [], mark=mark, daily_map=daily)
    broker = broker if broker is not None else EntryBroker()
    journal = Journal(tmp_path / "journal.sqlite")
    engine = Engine(
        cfg,
        journal,
        broker,
        data,
        dry_run=False,
        now_fn=lambda: now,
        calendar={"fomc": [], "earnings": {}},
    )
    return engine, broker, journal


def _spread(
    *,
    spread_id: str = "msft",
    underlying: str = "MSFT",
    status: SpreadStatus = SpreadStatus.PENDING_ENTRY,
    qty: int = 2,
    credit: float = LIMIT,
    max_loss: float = 776.0,
    order_id: Optional[str] = "ord-msft",
    entry_filled_qty: int = 0,
    short_occ: str = SHORT,
    long_occ: str = LONG,
) -> OpenSpread:
    return OpenSpread(
        id=spread_id,
        underlying=underlying,
        kind=SpreadKind.BULL_PUT_CREDIT,
        short_occ=short_occ,
        long_occ=long_occ,
        width=5.0,
        credit=credit,
        qty=qty,
        max_loss=max_loss,
        invalidation=100.0,
        status=status,
        opened_at="2026-03-04T14:40:00+00:00",
        broker_order_id=order_id,
        expiration="2026-11-06",
        entry_order_id=order_id if status is SpreadStatus.PENDING_ENTRY else None,
        entry_filled_qty=entry_filled_qty,
        entry_limit_credit=credit if status is not SpreadStatus.PROPOSED else None,
    )


def _proposal(symbol: str = "MSFT", credit: float = LIMIT) -> SpreadProposal:
    exp = date(2026, 11, 6)
    short = ContractQuote(SHORT, 490.0, exp, "put", 1.40, 1.45)
    long = ContractQuote(LONG, 485.0, exp, "put", 0.22, 0.28)
    return SpreadProposal(
        underlying=symbol,
        kind=SpreadKind.BULL_PUT_CREDIT,
        short=short,
        long=long,
        width=5.0,
        credit=credit,
        qty=0,
        max_loss=0.0,
        invalidation=100.0,
        reason="test",
    )


def test_fill_promotes_to_open_at_actual_credit(tmp_path: Path):
    engine, broker, journal = _engine(tmp_path, mark=0.90)
    journal.upsert_spread(_spread())
    broker.orders["ord-msft"] = _view(
        "ord-msft",
        state="filled",
        raw_status="filled",
        filled_qty=2,
        order_qty=2,
        credit=0.97,
    )
    broker.positions = {SHORT: -2, LONG: 2}

    engine.tick()

    live = journal.get_spread("msft")
    assert live is not None
    assert live.status is SpreadStatus.OPEN
    assert live.qty == 2
    assert live.credit == pytest.approx(0.97)
    assert live.entry_limit_credit == pytest.approx(LIMIT)
    assert live.entry_filled_qty == 2
    assert live.max_loss == pytest.approx((5.0 - 0.97) * 100 * 2)
    assert broker.closes == []
    assert broker.flattened == []
    assert "entry_filled" in _kinds(journal)
    filled = next(p for k, _s, p in _events(journal) if k == "entry_filled")
    assert filled["filled_credit"] == pytest.approx(0.97)
    assert filled["limit_credit"] == pytest.approx(LIMIT)


def test_terminal_partial_opens_only_the_filled_qty(tmp_path: Path):
    engine, broker, journal = _engine(tmp_path, mark=0.90)
    journal.upsert_spread(_spread(qty=2, max_loss=776.0))
    broker.orders["ord-msft"] = _view(
        "ord-msft",
        state="partial",
        raw_status="canceled",
        filled_qty=1,
        order_qty=2,
        credit=1.01,
    )

    engine.tick()

    live = journal.get_spread("msft")
    assert live is not None
    assert live.status is SpreadStatus.OPEN
    assert live.qty == 1
    assert live.credit == pytest.approx(1.01)
    assert live.entry_filled_qty == 1
    assert live.max_loss == pytest.approx((5.0 - 1.01) * 100)
    assert broker.closes == []
    assert "entry_partial" in _kinds(journal)


def test_working_partial_stays_pending_and_is_not_flattened(tmp_path: Path):
    engine, broker, journal = _engine(tmp_path, mark=0.20)
    journal.upsert_spread(_spread())
    broker.orders["ord-msft"] = _view(
        "ord-msft",
        state="open",
        raw_status="partially_filled",
        filled_qty=1,
        order_qty=2,
        credit=1.05,
    )
    # Both legs of the filled unit are on. A pending entry is not flattened.
    broker.positions = {SHORT: -1, LONG: 1}

    engine.tick()

    live = journal.get_spread("msft")
    assert live is not None
    assert live.status is SpreadStatus.PENDING_ENTRY
    assert live.qty == 2
    assert live.credit == pytest.approx(LIMIT)
    assert live.max_loss == pytest.approx(776.0)
    assert live.entry_filled_qty == 1
    assert broker.closes == []
    assert broker.flattened == []
    assert broker.cancels == []
    assert "entry_partial_working" in _kinds(journal)


@pytest.mark.parametrize(
    ("raw_status", "status", "event"),
    [
        ("expired", SpreadStatus.ENTRY_EXPIRED, "entry_expired"),
        ("done_for_day", SpreadStatus.ENTRY_EXPIRED, "entry_expired"),
        ("canceled", SpreadStatus.CANCELLED, "entry_cancelled"),
        ("rejected", SpreadStatus.CANCELLED, "entry_cancelled"),
    ],
)
def test_unfilled_terminal_frees_the_slot(tmp_path: Path, raw_status, status, event):
    engine, broker, journal = _engine(tmp_path)
    journal.upsert_spread(_spread())
    broker.orders["ord-msft"] = _view(
        "ord-msft", state="dead", raw_status=raw_status, filled_qty=0, order_qty=2
    )
    live = journal.open_spreads()
    same = decide(
        equity=100_000,
        proposal=_proposal("MSFT"),
        open_spreads=live,
        risk_pct=0.005,
        max_concurrent=5,
        max_portfolio_risk_pct=0.10,
    )
    assert same.allow is False
    assert same.reason == "one_spread_per_underlying"
    capped = decide(
        equity=100_000,
        proposal=_proposal("QQQ"),
        open_spreads=live,
        risk_pct=0.005,
        max_concurrent=1,
        max_portfolio_risk_pct=0.10,
    )
    assert capped.allow is False
    assert capped.reason == "max_concurrent"

    engine.tick()

    row = journal.get_spread("msft")
    assert row is not None
    assert row.status is status
    assert row.exit_reason == event
    assert journal.open_spreads() == []
    assert broker.closes == []
    assert event in _kinds(journal)
    freed_book = journal.open_spreads()
    freed = decide(
        equity=100_000,
        proposal=_proposal("MSFT"),
        open_spreads=freed_book,
        risk_pct=0.005,
        max_concurrent=5,
        max_portfolio_risk_pct=0.10,
    )
    assert freed.allow is True
    other = decide(
        equity=100_000,
        proposal=_proposal("QQQ"),
        open_spreads=freed_book,
        risk_pct=0.005,
        max_concurrent=1,
        max_portfolio_risk_pct=0.10,
    )
    assert other.allow is True


def test_pending_is_not_closed_by_exits(tmp_path: Path):
    """A take-profit mark closes a held spread and leaves the working entry alone."""
    engine, broker, journal = _engine(tmp_path, mark=0.20)
    journal.upsert_spread(_spread())
    broker.orders["ord-msft"] = _view(
        "ord-msft", state="open", raw_status="new", order_qty=2
    )
    journal.upsert_spread(
        _spread(
            spread_id="spy",
            underlying="SPY",
            status=SpreadStatus.OPEN,
            qty=1,
            credit=1.20,
            max_loss=380.0,
            order_id=None,
            short_occ="SPY260417P00100000",
            long_occ="SPY260417P00095000",
        )
    )

    engine.tick()

    pending = journal.get_spread("msft")
    held = journal.get_spread("spy")
    assert pending is not None and pending.status is SpreadStatus.PENDING_ENTRY
    assert held is not None and held.status is SpreadStatus.EXITING
    assert held.exit_reason == "take_profit"
    assert [c["spread_id"] for c in broker.closes] == ["spy"]
    assert broker.cancels == []
    assert broker.flattened == []


def test_structure_break_cancels_working_entry_without_a_close(tmp_path: Path):
    engine, broker, journal = _engine(
        tmp_path,
        daily={"MSFT": _broken_daily(), "SPY": _intact_daily()},
        mark=0.90,
    )
    journal.upsert_spread(_spread())
    broker.orders["ord-msft"] = _view(
        "ord-msft", state="open", raw_status="new", order_qty=2
    )

    engine.tick()

    row = journal.get_spread("msft")
    assert row is not None
    assert row.status is SpreadStatus.CANCELLED
    assert broker.cancels == ["ord-msft"]
    assert broker.closes == []
    assert broker.flattened == []
    assert "entry_cancel_structure_break" in _kinds(journal)
    assert "entry_cancelled" in _kinds(journal)
    assert row.status is not SpreadStatus.EXITING


def test_structure_break_off_hours_cancels_entry_and_does_not_submit_a_close(
    tmp_path: Path,
):
    engine, broker, journal = _engine(
        tmp_path,
        daily={"MSFT": _broken_daily()},
        now=OFF_HOURS,
        mark=0.20,
    )
    journal.upsert_spread(_spread())
    broker.orders["ord-msft"] = _view(
        "ord-msft", state="open", raw_status="accepted", order_qty=2
    )

    engine.tick()

    row = journal.get_spread("msft")
    assert row is not None
    assert row.status is SpreadStatus.CANCELLED
    assert broker.cancels == ["ord-msft"]
    assert broker.closes == []
    assert "overnight_open" not in _kinds(journal)


def test_structure_break_on_a_partial_cancels_then_closes_only_the_fill(
    tmp_path: Path,
):
    engine, broker, journal = _engine(
        tmp_path,
        daily={"MSFT": _broken_daily()},
        mark=0.90,
    )
    journal.upsert_spread(_spread(qty=3, max_loss=1164.0))
    broker.orders["ord-msft"] = _view(
        "ord-msft",
        state="open",
        raw_status="partially_filled",
        filled_qty=1,
        order_qty=3,
        credit=1.10,
    )

    engine.tick()

    assert broker.cancels == ["ord-msft"]
    assert len(broker.closes) == 1
    assert int(broker.closes[0]["qty"]) == 1
    row = journal.get_spread("msft")
    assert row is not None
    assert row.status is SpreadStatus.EXITING
    assert row.qty == 1
    assert row.exit_reason == "structure_break"


def test_failed_entry_cancel_does_not_submit_a_close(tmp_path: Path):
    engine, broker, journal = _engine(
        tmp_path, daily={"MSFT": _broken_daily()}, mark=0.90
    )
    journal.upsert_spread(_spread())
    broker.orders["ord-msft"] = _view(
        "ord-msft", state="open", raw_status="new", order_qty=2
    )
    broker.cancel_error = RuntimeError("reject cancel")

    engine.tick()

    row = journal.get_spread("msft")
    assert row is not None
    assert row.status is SpreadStatus.PENDING_ENTRY
    assert broker.closes == []
    assert "entry_cancel_failed" in _kinds(journal)


def test_stale_structure_does_not_cancel_a_working_entry(tmp_path: Path):
    engine, broker, journal = _engine(
        tmp_path,
        daily={"MSFT": _ancient_broken_daily()},
        mark=0.20,
    )
    journal.upsert_spread(_spread())
    broker.orders["ord-msft"] = _view(
        "ord-msft", state="open", raw_status="new", order_qty=2
    )

    engine.tick()

    row = journal.get_spread("msft")
    assert row is not None
    assert row.status is SpreadStatus.PENDING_ENTRY
    assert broker.cancels == []
    assert broker.closes == []


def test_pending_does_not_flag_overnight_open(tmp_path: Path):
    engine, broker, journal = _engine(tmp_path, now=OFF_HOURS, mark=0.90)
    journal.upsert_spread(_spread())
    broker.orders["ord-msft"] = _view(
        "ord-msft", state="open", raw_status="new", order_qty=2
    )
    journal.upsert_spread(
        _spread(
            spread_id="spy",
            underlying="SPY",
            status=SpreadStatus.OPEN,
            qty=1,
            credit=1.20,
            max_loss=380.0,
            order_id=None,
            short_occ="SPY260417P00100000",
            long_occ="SPY260417P00095000",
        )
    )

    engine.tick()

    overnight = [p for k, _s, p in _events(journal) if k == "overnight_open"]
    assert len(overnight) == 1
    ids = [s["id"] for s in overnight[0]["spreads"]]
    assert ids == ["spy"]
    assert journal.get_spread("msft").status is SpreadStatus.PENDING_ENTRY


def test_pending_counts_toward_risk_limits():
    pending = _spread(max_loss=9800.0)
    same = decide(
        equity=100_000,
        proposal=_proposal("MSFT"),
        open_spreads=[pending],
        risk_pct=0.005,
        max_concurrent=5,
        max_portfolio_risk_pct=0.10,
    )
    assert same.allow is False
    assert same.reason == "one_spread_per_underlying"

    other = _proposal("QQQ")
    capped = decide(
        equity=100_000,
        proposal=other,
        open_spreads=[pending],
        risk_pct=0.005,
        max_concurrent=1,
        max_portfolio_risk_pct=0.10,
    )
    assert capped.allow is False
    assert capped.reason == "max_concurrent"

    budget = decide(
        equity=100_000,
        proposal=other,
        open_spreads=[pending],
        risk_pct=0.005,
        max_concurrent=5,
        max_portfolio_risk_pct=0.10,
    )
    assert budget.allow is False
    assert budget.reason == "max_risk_budget"


def test_live_submit_journals_pending_not_open(tmp_path: Path):
    engine, broker, journal = _engine(tmp_path)
    journal.upsert_arm(
        Arm(
            id="arm-msft",
            symbol="MSFT",
            side=Side.BULLISH,
            invalidation=100.0,
            zone_low=108.0,
            zone_high=110.0,
            confirmed_at="2026-03-02T21:00:00+00:00",
            status=ArmStatus.ARMED,
            reason="test",
        )
    )

    engine._maybe_open(_proposal(), [], RTH)

    assert len(broker.opens) == 1
    payload = broker.opens[0]["payload"]
    assert payload["order_class"] == "mleg"
    assert payload["time_in_force"] == "day"
    assert len(payload["legs"]) == 2
    assert float(payload["limit_price"]) < 0
    row = journal.open_spreads()[0]
    assert row.status is SpreadStatus.PENDING_ENTRY
    assert row.credit == pytest.approx(LIMIT)
    assert row.entry_limit_credit == pytest.approx(LIMIT)
    assert row.entry_order_id == broker.opens[0]["id"]
    assert row.broker_order_id == row.entry_order_id
    assert journal.get_open_arm("MSFT") is None
    arm_row = journal.get_spread(row.id)
    assert arm_row is not None

    # The working order still reserves the underlying.
    engine._maybe_open(_proposal(), journal.open_spreads(), RTH)
    assert len(broker.opens) == 1
    assert "risk_skip" in _kinds(journal)


def test_empty_or_rejected_submit_is_not_journaled(tmp_path: Path):
    engine, broker, journal = _engine(tmp_path)
    broker.submit_empty = True
    engine._maybe_open(_proposal(), [], RTH)
    assert journal.open_spreads() == []
    assert "entry_submit_failed" in _kinds(journal)

    broker.submit_empty = False
    broker.submit_error = RuntimeError("rejected")
    engine._maybe_open(_proposal(), [], RTH)
    assert journal.open_spreads() == []
    assert _kinds(journal).count("entry_submit_failed") == 2


def test_legacy_open_unfilled_is_terminal_and_working_becomes_pending(tmp_path: Path):
    engine, broker, journal = _engine(tmp_path, mark=0.20)
    # Today's bug: journaled OPEN at the limit while the day order never filled.
    phantom = _spread(status=SpreadStatus.OPEN, order_id="ord-msft")
    phantom.entry_order_id = None
    phantom.entry_limit_credit = None
    journal.upsert_spread(phantom)
    broker.orders["ord-msft"] = _view(
        "ord-msft", state="dead", raw_status="expired", filled_qty=0, order_qty=2
    )

    engine.tick()

    row = journal.get_spread("msft")
    assert row is not None
    assert row.status is SpreadStatus.ENTRY_EXPIRED
    assert journal.open_spreads() == []
    assert broker.closes == []
    assert "legacy_entry_unfilled" in _kinds(journal)
    assert "entry_expired" in _kinds(journal)
    freed = decide(
        equity=100_000,
        proposal=_proposal("MSFT"),
        open_spreads=journal.open_spreads(),
        risk_pct=0.005,
        max_concurrent=5,
        max_portfolio_risk_pct=0.10,
    )
    assert freed.allow is True


def test_legacy_open_still_working_is_pending_and_not_closed(tmp_path: Path):
    engine, broker, journal = _engine(tmp_path, mark=0.20)
    working = _spread(status=SpreadStatus.OPEN, order_id="ord-msft")
    working.entry_order_id = None
    journal.upsert_spread(working)
    broker.orders["ord-msft"] = _view(
        "ord-msft",
        state="open",
        raw_status="new",
        filled_qty=1,
        order_qty=2,
        credit=1.08,
    )
    broker.positions = {SHORT: -1, LONG: 1}

    engine.tick()

    row = journal.get_spread("msft")
    assert row is not None
    assert row.status is SpreadStatus.PENDING_ENTRY
    assert row.entry_order_id == "ord-msft"
    assert row.entry_filled_qty == 1
    assert row.qty == 2
    assert row.credit == pytest.approx(LIMIT)
    assert broker.closes == []
    assert broker.cancels == []
    assert broker.flattened == []
    assert "legacy_entry_still_working" in _kinds(journal)


def test_legacy_filled_open_keeps_the_position_and_stores_the_fill(tmp_path: Path):
    engine, broker, journal = _engine(tmp_path, mark=0.90)
    held = _spread(status=SpreadStatus.OPEN, qty=2, credit=LIMIT, max_loss=776.0, order_id="ord-msft")
    held.entry_order_id = None
    held.entry_limit_credit = None
    journal.upsert_spread(held)
    broker.orders["ord-msft"] = _view(
        "ord-msft",
        state="filled",
        raw_status="filled",
        filled_qty=2,
        order_qty=2,
        credit=0.97,
    )
    broker.positions = {SHORT: -2, LONG: 2}

    engine.tick()

    row = journal.get_spread("msft")
    assert row is not None
    assert row.status is SpreadStatus.OPEN
    assert row.qty == 2
    assert row.credit == pytest.approx(0.97)
    assert row.entry_limit_credit == pytest.approx(LIMIT)
    assert row.max_loss == pytest.approx((5.0 - 0.97) * 100 * 2)
    assert broker.closes == []
    assert "legacy_entry_filled" in _kinds(journal)


def test_legacy_unfilled_with_unknown_positions_is_not_freed(tmp_path: Path):
    broker = EntryBroker(positions_unknown=True)
    engine, broker, journal = _engine(tmp_path, broker=broker)
    phantom = _spread(status=SpreadStatus.OPEN, order_id="ord-msft")
    phantom.entry_order_id = None
    journal.upsert_spread(phantom)
    broker.orders["ord-msft"] = _view(
        "ord-msft", state="dead", raw_status="expired", filled_qty=0
    )

    engine.tick()

    assert journal.get_spread("msft").status is SpreadStatus.OPEN
    assert "legacy_entry_positions_unknown" in _kinds(journal)

    broker.positions_unknown = False
    engine.tick()

    assert journal.get_spread("msft").status is SpreadStatus.ENTRY_EXPIRED
    assert journal.open_spreads() == []


def test_legacy_unfilled_with_a_held_leg_stays_open(tmp_path: Path):
    engine, broker, journal = _engine(tmp_path, mark=0.90)
    phantom = _spread(status=SpreadStatus.OPEN, order_id="ord-msft")
    phantom.entry_order_id = None
    journal.upsert_spread(phantom)
    broker.orders["ord-msft"] = _view(
        "ord-msft", state="dead", raw_status="expired", filled_qty=0
    )
    broker.positions = {SHORT: -1, LONG: 0}

    engine.tick()

    row = journal.get_spread("msft")
    assert row is not None
    assert row.status is not SpreadStatus.ENTRY_EXPIRED
    assert row.status is not SpreadStatus.CANCELLED
    assert "legacy_entry_positions_held" in _kinds(journal)


def test_old_journal_gains_entry_columns(tmp_path: Path):
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE spreads (
            id TEXT PRIMARY KEY,
            underlying TEXT NOT NULL,
            kind TEXT NOT NULL,
            short_occ TEXT NOT NULL,
            long_occ TEXT NOT NULL,
            width REAL NOT NULL,
            credit REAL NOT NULL,
            qty INTEGER NOT NULL,
            max_loss REAL NOT NULL,
            invalidation REAL NOT NULL,
            status TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            broker_order_id TEXT,
            exit_reason TEXT NOT NULL DEFAULT '',
            thesis_intact INTEGER NOT NULL DEFAULT 1,
            expiration TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        INSERT INTO spreads (
            id, underlying, kind, short_occ, long_occ, width, credit, qty, max_loss,
            invalidation, status, opened_at, broker_order_id, exit_reason,
            thesis_intact, expiration, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "msft",
            "MSFT",
            "bull_put_credit",
            SHORT,
            LONG,
            5.0,
            LIMIT,
            2,
            776.0,
            100.0,
            "open",
            "2026-03-04T14:40:00+00:00",
            "ord-msft",
            "",
            1,
            "2026-11-06",
            "2026-03-04T14:40:00+00:00",
        ),
    )
    con.commit()
    con.close()

    journal = Journal(path)
    loaded = journal.get_spread("msft")
    assert loaded is not None
    assert loaded.status is SpreadStatus.OPEN
    assert loaded.entry_order_id == "ord-msft"
    assert loaded.entry_filled_qty == 0
    assert loaded.entry_limit_credit == pytest.approx(LIMIT)
    assert loaded.broker_order_id == "ord-msft"


def test_entry_order_view_reads_signed_credit_and_working_partial():
    filled = entry_order_view_from_broker_order(
        {
            "id": "ord-1",
            "status": "filled",
            "filled_qty": "2",
            "qty": "2",
            "filled_avg_price": "-0.97",
            "filled_at": "2026-03-04T15:01:00+00:00",
        }
    )
    assert filled.state == "filled"
    assert filled.filled_qty == 2
    assert filled.filled_avg_credit == pytest.approx(0.97)

    partial = entry_order_view_from_broker_order(
        {
            "id": "ord-2",
            "status": "partially_filled",
            "filled_qty": "1",
            "qty": "3",
            "filled_avg_price": "-1.05",
        }
    )
    assert partial.state == "open"
    assert partial.filled_qty == 1
    assert partial.raw_status == "partially_filled"

    expired = entry_order_view_from_broker_order(
        {"id": "ord-3", "status": "expired", "filled_qty": "0", "qty": "2"}
    )
    assert expired.state == "dead"
    assert expired.filled_qty == 0
    assert expired.raw_status == "expired"

    from_legs = entry_order_view_from_broker_order(
        {
            "id": "ord-4",
            "status": "filled",
            "filled_qty": "1",
            "qty": "1",
            "legs": [
                {"side": "sell", "position_intent": "sell_to_open", "filled_avg_price": "1.40"},
                {"side": "buy", "position_intent": "buy_to_open", "filled_avg_price": "0.28"},
            ],
        }
    )
    assert from_legs.filled_avg_credit == pytest.approx(1.12)
