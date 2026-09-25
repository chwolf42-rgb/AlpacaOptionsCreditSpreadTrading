"""Durable SQLite journal for arms, open spreads, and exit state.

Isolated under var/options/ — never share with equity or crypto bots.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Union

from alpaca_options_credit.close_prices import (
    CLOSE_DEBIT_BACKFILLED,
    CLOSE_PARTIAL,
    CLOSE_PRICE_MISSING,
    SOURCE_BACKFILL,
    SOURCE_FILL,
    SOURCE_MISSING,
    SOURCE_QUOTE,
    CloseOrderView,
    as_debit,
)
from alpaca_options_credit.rth import ET, RTH_CLOSE, RTH_OPEN, as_et
from alpaca_options_credit.strategy.spreads import (
    STRUCTURE_BREAK_EXIT,
    TAKE_PROFIT_EXIT,
)

from alpaca_options_credit.models import (
    LIVE_SPREAD_STATUSES,
    Arm,
    ArmStatus,
    OpenSpread,
    Side,
    SpreadKind,
    SpreadStatus,
)

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS arms (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    invalidation REAL NOT NULL,
    zone_low REAL NOT NULL,
    zone_high REAL NOT NULL,
    confirmed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    bar_index INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spreads (
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
    close_attempts INTEGER NOT NULL DEFAULT 0,
    exit_order_id TEXT,
    last_close_error TEXT NOT NULL DEFAULT '',
    close_debit REAL,
    closed_at TEXT,
    close_price_source TEXT NOT NULL DEFAULT '',
    close_filled_qty INTEGER NOT NULL DEFAULT 0,
    close_fill_notional REAL,
    close_order_filled_seen INTEGER NOT NULL DEFAULT 0,
    close_order_notional_seen REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    symbol TEXT,
    payload TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.executescript(SCHEMA)
            _migrate_spreads(con)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def log_event(self, kind: str, symbol: Optional[str], payload: dict[str, Any]) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT INTO events (ts, kind, symbol, payload) VALUES (?, ?, ?, ?)",
                (_now(), kind, symbol, json.dumps(payload, default=str)),
            )

    def upsert_arm(self, arm: Arm) -> None:
        with self._conn() as con:
            con.execute(
                """
                INSERT INTO arms (id, symbol, side, invalidation, zone_low, zone_high,
                                  confirmed_at, status, reason, bar_index, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    reason=excluded.reason,
                    invalidation=excluded.invalidation,
                    zone_low=excluded.zone_low,
                    zone_high=excluded.zone_high,
                    bar_index=excluded.bar_index,
                    updated_at=excluded.updated_at
                """,
                (
                    arm.id,
                    arm.symbol,
                    arm.side.value,
                    arm.invalidation,
                    arm.zone_low,
                    arm.zone_high,
                    arm.confirmed_at,
                    arm.status.value,
                    arm.reason,
                    arm.bar_index,
                    _now(),
                ),
            )

    def get_open_arm(self, symbol: str) -> Optional[Arm]:
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM arms WHERE symbol=? AND status=? ORDER BY updated_at DESC LIMIT 1",
                (symbol, ArmStatus.ARMED.value),
            ).fetchone()
        return _arm_from_row(row) if row else None

    def cancel_arm(self, arm_id: str, reason: str) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE arms SET status=?, reason=?, updated_at=? WHERE id=?",
                (ArmStatus.CANCELLED.value, reason, _now(), arm_id),
            )

    def mark_arm_triggered(self, arm_id: str, reason: str) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE arms SET status=?, reason=?, updated_at=? WHERE id=?",
                (ArmStatus.TRIGGERED.value, reason, _now(), arm_id),
            )

    def upsert_spread(self, spread: OpenSpread) -> None:
        # status=closed is not written here. record_close is the only closer.
        closing = spread.status is SpreadStatus.CLOSED
        if closing:
            spread = replace(
                spread,
                status=SpreadStatus.EXITING if spread.exit_reason else SpreadStatus.OPEN,
            )
        with self._conn() as con:
            con.execute(
                """
                INSERT INTO spreads (id, underlying, kind, short_occ, long_occ, width, credit,
                                     qty, max_loss, invalidation, status, opened_at,
                                     broker_order_id, exit_reason, thesis_intact, expiration,
                                     close_attempts, exit_order_id, last_close_error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    exit_reason=excluded.exit_reason,
                    thesis_intact=excluded.thesis_intact,
                    broker_order_id=excluded.broker_order_id,
                    close_attempts=excluded.close_attempts,
                    exit_order_id=excluded.exit_order_id,
                    last_close_error=excluded.last_close_error,
                    updated_at=excluded.updated_at
                """,
                (
                    spread.id,
                    spread.underlying,
                    spread.kind.value,
                    spread.short_occ,
                    spread.long_occ,
                    spread.width,
                    spread.credit,
                    spread.qty,
                    spread.max_loss,
                    spread.invalidation,
                    spread.status.value,
                    spread.opened_at,
                    spread.broker_order_id,
                    spread.exit_reason,
                    1 if spread.thesis_intact else 0,
                    spread.expiration,
                    int(spread.close_attempts),
                    spread.exit_order_id,
                    spread.last_close_error,
                    _now(),
                ),
            )
        if closing:
            self.record_close(
                spread.id,
                spread.exit_reason or "closed",
                close_debit=spread.close_debit,
                closed_at=spread.closed_at or None,
                source=spread.close_price_source,
            )

    def get_spread(self, spread_id: str) -> Optional[OpenSpread]:
        with self._conn() as con:
            row = con.execute("SELECT * FROM spreads WHERE id=?", (spread_id,)).fetchone()
        return _spread_from_row(row) if row else None

    def _live_status_values(self) -> tuple[str, ...]:
        return tuple(s.value for s in LIVE_SPREAD_STATUSES)

    def open_spreads(self) -> list[OpenSpread]:
        statuses = self._live_status_values()
        placeholders = ",".join("?" * len(statuses))
        with self._conn() as con:
            rows = con.execute(
                f"SELECT * FROM spreads WHERE status IN ({placeholders}) ORDER BY opened_at",
                statuses,
            ).fetchall()
        return [_spread_from_row(r) for r in rows]

    def open_for_symbol(self, symbol: str) -> Optional[OpenSpread]:
        statuses = self._live_status_values()
        placeholders = ",".join("?" * len(statuses))
        with self._conn() as con:
            row = con.execute(
                f"SELECT * FROM spreads WHERE underlying=? AND status IN ({placeholders}) LIMIT 1",
                (symbol, *statuses),
            ).fetchone()
        return _spread_from_row(row) if row else None

    def latch_exit(
        self,
        spread_id: str,
        reason: str,
        *,
        thesis_intact: bool = True,
    ) -> None:
        """Keep the spread live with a latched exit plan. First reason wins."""
        with self._conn() as con:
            row = con.execute(
                "SELECT exit_reason, status, thesis_intact FROM spreads WHERE id=?",
                (spread_id,),
            ).fetchone()
            if row is None:
                return
            if row["status"] == SpreadStatus.CLOSED.value:
                return
            kept = row["exit_reason"] or reason
            intact = bool(row["thesis_intact"]) if row["exit_reason"] else thesis_intact
            con.execute(
                """
                UPDATE spreads
                SET status=?, exit_reason=?, thesis_intact=?, updated_at=?
                WHERE id=?
                """,
                (
                    SpreadStatus.EXITING.value,
                    kept,
                    1 if intact else 0,
                    _now(),
                    spread_id,
                ),
            )

    def record_working_close(self, spread_id: str, order_id: str) -> None:
        """Remember the working mleg id. Seen-fill counters reset for this order."""
        with self._conn() as con:
            con.execute(
                """
                UPDATE spreads
                SET exit_order_id=?,
                    last_close_error='',
                    close_order_filled_seen=0,
                    close_order_notional_seen=0,
                    updated_at=?
                WHERE id=? AND status!=?
                """,
                (order_id, _now(), spread_id, SpreadStatus.CLOSED.value),
            )

    def record_close_failure(self, spread_id: str, error: str) -> int:
        """Increment attempts, store the error, keep EXITING. Returns attempt count."""
        with self._conn() as con:
            con.execute(
                """
                UPDATE spreads
                SET status=?,
                    close_attempts=close_attempts + 1,
                    last_close_error=?,
                    updated_at=?
                WHERE id=? AND status!=?
                """,
                (
                    SpreadStatus.EXITING.value,
                    error,
                    _now(),
                    spread_id,
                    SpreadStatus.CLOSED.value,
                ),
            )
            row = con.execute(
                "SELECT close_attempts FROM spreads WHERE id=?", (spread_id,)
            ).fetchone()
        return int(row["close_attempts"]) if row else 0

    def close_spread(
        self,
        spread_id: str,
        reason: str,
        *,
        close_debit: Optional[float] = None,
        closed_at: Optional[str] = None,
        source: str = "",
    ) -> None:
        """Close a spread. Alias of :meth:`record_close` — the only closer."""
        self.record_close(
            spread_id,
            reason,
            close_debit=close_debit,
            closed_at=closed_at,
            source=source,
        )

    def record_close(
        self,
        spread_id: str,
        reason: str,
        *,
        close_debit: Optional[float] = None,
        closed_at: Optional[str] = None,
        source: str = "",
    ) -> None:
        """The only writer of ``status=closed``.

        Always sets ``closed_at`` (first stamp wins). Writes ``close_debit``
        when a finite premium is passed; a later debit replaces an earlier
        one. When no price can be obtained, leaves ``close_debit`` NULL,
        sets ``close_price_source='missing'``, logs ``close_price_missing``,
        and warns. Realized P&L is ``(credit - close_debit) * qty * multiplier``.
        """
        debit = as_debit(close_debit)
        missing_event: Optional[dict[str, Any]] = None
        symbol: Optional[str] = None
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM spreads WHERE id=?",
                (spread_id,),
            ).fetchone()
            if row is None:
                return
            symbol = row["underlying"]
            existing = as_debit(row["close_debit"])
            stored = debit if debit is not None else existing
            prior_source = str(row["close_price_source"] or "")
            if stored is None:
                source_out = SOURCE_MISSING
                if prior_source != SOURCE_MISSING:
                    missing_event = {
                        "spread_id": spread_id,
                        "reason": reason,
                        "source": SOURCE_MISSING,
                    }
            elif debit is not None:
                source_out = source or prior_source or SOURCE_QUOTE
            else:
                source_out = prior_source or SOURCE_QUOTE
            stamp = str(row["closed_at"] or "") or closed_at or _now()
            con.execute(
                """
                UPDATE spreads
                SET status=?,
                    exit_reason=?,
                    last_close_error='',
                    close_debit=?,
                    closed_at=?,
                    close_price_source=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    SpreadStatus.CLOSED.value,
                    reason,
                    stored,
                    stamp,
                    source_out,
                    stamp,
                    spread_id,
                ),
            )
        if missing_event is not None:
            log.warning(
                "close price missing spread=%s underlying=%s reason=%s — "
                "closed-trade stats count this in n_missing_price",
                spread_id,
                symbol,
                reason,
            )
            self.log_event(CLOSE_PRICE_MISSING, symbol, missing_event)

    def apply_close_fill(
        self,
        spread_id: str,
        reason: str,
        order: CloseOrderView,
        *,
        target_qty: int,
    ) -> str:
        """Fold one broker fill snapshot into the journal.

        Returns ``closed``, ``unpriced``, ``working``, ``partial``, or ``dead``.

        ``filled_avg_price`` on an order is the average of that order's fills,
        so each poll replaces that order's contribution instead of adding the
        average again. A terminal short fill increments ``close_attempts`` and
        clears ``exit_order_id`` so the next poll can submit the remainder.
        A still-working partial is left alone (no cancel, no attempt bump).
        """
        outcome = "working"
        debit_out: Optional[float] = None
        filled_at = order.filled_at
        partial_event: Optional[dict[str, Any]] = None
        symbol: Optional[str] = None
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM spreads WHERE id=?",
                (spread_id,),
            ).fetchone()
            if row is None:
                return "dead"
            symbol = row["underlying"]
            if row["status"] == SpreadStatus.CLOSED.value:
                return "closed" if as_debit(row["close_debit"]) is not None else "unpriced"

            seen_qty = int(row["close_order_filled_seen"] or 0)
            seen_notional = float(row["close_order_notional_seen"] or 0.0)
            filled_qty = int(row["close_filled_qty"] or 0)
            notional = row["close_fill_notional"]
            cumulative = float(notional) if notional is not None else 0.0
            same_order = str(row["exit_order_id"] or "") == str(order.order_id or "")
            if not same_order:
                seen_qty = 0
                seen_notional = 0.0

            reported = max(int(order.filled_qty), 0)
            price = as_debit(order.net_debit)
            if reported > seen_qty and price is not None:
                order_notional = price * reported
                cumulative += order_notional - seen_notional
                filled_qty += reported - seen_qty
                seen_qty = reported
                seen_notional = order_notional
            elif reported > seen_qty and price is None and order.state in {"filled", "partial"}:
                # Contracts filled but the broker sent no price. Count qty so
                # we do not resubmit them; the close itself is unpriced.
                filled_qty += reported - seen_qty
                seen_qty = reported

            target = max(int(target_qty), 0)
            terminal = order.state in {"filled", "partial", "dead"}
            reached = target > 0 and filled_qty >= target
            now = _now()
            con.execute(
                """
                UPDATE spreads
                SET close_filled_qty=?,
                    close_fill_notional=?,
                    close_order_filled_seen=?,
                    close_order_notional_seen=?,
                    updated_at=?
                WHERE id=? AND status!=?
                """,
                (
                    filled_qty,
                    cumulative if filled_qty else None,
                    seen_qty,
                    seen_notional if seen_qty else 0.0,
                    now,
                    spread_id,
                    SpreadStatus.CLOSED.value,
                ),
            )
            if reached and filled_qty > 0 and (price is not None or cumulative != 0):
                debit_out = cumulative / filled_qty
                outcome = "closed"
            elif reached and order.state == "filled":
                outcome = "unpriced"
                filled_at = filled_at or now
            elif order.state == "open" or not terminal:
                outcome = "working"
            elif reported > 0 or filled_qty > 0:
                attempts = int(row["close_attempts"] or 0) + 1
                con.execute(
                    """
                    UPDATE spreads
                    SET status=?,
                        close_attempts=?,
                        last_close_error=?,
                        exit_order_id=NULL,
                        close_order_filled_seen=0,
                        close_order_notional_seen=0,
                        updated_at=?
                    WHERE id=? AND status!=?
                    """,
                    (
                        SpreadStatus.EXITING.value,
                        attempts,
                        f"partial fill {filled_qty}/{target}",
                        now,
                        spread_id,
                        SpreadStatus.CLOSED.value,
                    ),
                )
                outcome = "partial"
                partial_event = {
                    "spread_id": spread_id,
                    "reason": reason,
                    "order_id": order.order_id,
                    "filled_qty": filled_qty,
                    "target_qty": target,
                    "net_debit": price,
                    "close_attempts": attempts,
                }
            else:
                attempts = int(row["close_attempts"] or 0) + 1
                con.execute(
                    """
                    UPDATE spreads
                    SET status=?,
                        close_attempts=?,
                        last_close_error=?,
                        exit_order_id=NULL,
                        close_order_filled_seen=0,
                        close_order_notional_seen=0,
                        updated_at=?
                    WHERE id=? AND status!=?
                    """,
                    (
                        SpreadStatus.EXITING.value,
                        attempts,
                        f"close order {order.state} with no fill",
                        now,
                        spread_id,
                        SpreadStatus.CLOSED.value,
                    ),
                )
                outcome = "dead"

        if outcome == "closed" and debit_out is not None:
            self.record_close(
                spread_id,
                reason,
                close_debit=debit_out,
                closed_at=filled_at,
                source=SOURCE_FILL,
            )
        elif outcome == "unpriced":
            self.record_close(
                spread_id,
                reason,
                close_debit=None,
                closed_at=filled_at,
                source=SOURCE_MISSING,
            )
        elif partial_event is not None:
            log.error(
                "PARTIAL CLOSE %s %s filled %s/%s — remainder stays EXITING, "
                "close_attempts=%s, retry next poll",
                symbol,
                spread_id,
                partial_event["filled_qty"],
                partial_event["target_qty"],
                partial_event["close_attempts"],
            )
            self.log_event(CLOSE_PARTIAL, symbol, partial_event)
        return outcome

    def write_backfilled_close(
        self,
        spread_id: str,
        close_debit: float,
        closed_at: str,
        *,
        underlying: str = "",
        exit_reason: str = "",
    ) -> bool:
        """Fill a NULL close_debit on an already-closed row. Never overwrites.

        ``updated_at`` is left as the original close clock. ``closed_at`` is
        set from that clock when it was empty. The row is tagged
        ``close_price_source=backfill`` and an estimated event is logged.
        Returns False when the row already had a debit or was not closed.
        """
        debit = as_debit(close_debit)
        if debit is None:
            return False
        with self._conn() as con:
            cur = con.execute(
                """
                UPDATE spreads
                SET close_debit=?,
                    closed_at=COALESCE(closed_at, ?),
                    close_price_source=?
                WHERE id=? AND status=? AND close_debit IS NULL
                """,
                (
                    debit,
                    closed_at,
                    SOURCE_BACKFILL,
                    spread_id,
                    SpreadStatus.CLOSED.value,
                ),
            )
            wrote = cur.rowcount == 1
        if not wrote:
            return False
        self.log_event(
            CLOSE_DEBIT_BACKFILLED,
            underlying or None,
            {
                "spread_id": spread_id,
                "close_debit": debit,
                "closed_at": closed_at,
                "estimated": True,
                "source": SOURCE_BACKFILL,
                "exit_reason": exit_reason,
            },
        )
        return True

    def closed_spreads(self) -> list[OpenSpread]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM spreads WHERE status=? ORDER BY opened_at",
                (SpreadStatus.CLOSED.value,),
            ).fetchall()
        return [_spread_from_row(r) for r in rows]

    def summarize_managed_outcomes(
        self,
        start: Union[date, datetime],
        end: Union[date, datetime],
        *,
        session: str = "calendar",
        multiplier: float = 100.0,
        take_profit_frac: float = 0.50,
        stop_multiple: float = 1.5,
    ) -> "ManagedOutcomeSummary":
        """Win rate and average win/loss for managed closes in a date window.

        Query path for the EOD digest. ``date`` bounds are inclusive
        America/New_York calendar days. ``datetime`` bounds are half-open
        ``[start, end)``. ``session="rth"`` keeps only weekday 09:30–16:00 ET
        closes; ``session="calendar"`` (default) keeps the whole ET day.

        A managed win is ``take_profit``. A managed loss is a credit stop
        (``stop_credit``, legacy ``stop_2x_credit``, or any ``stop*`` token)
        or ``structure_break``. Other closes, including ``naked_leg``, are
        omitted. Dry-run journals the same row and still sends no broker order.

        ``avg_win`` and ``avg_loss`` are mean realized dollars,
        ``(credit - close_debit) * qty * multiplier``. ``avg_loss`` is signed.
        ``pnl`` is the sum of those dollars. Backfilled (estimated) rows are
        included. When ``close_debit`` was not stored, take-profit and stop
        use the locked policy (capture ``take_profit_frac`` of credit; stop
        at ``stop_multiple`` × credit) and still count in ``n_missing_price``.
        A structure exit without a debit counts in ``n_losses`` and
        ``n_missing_price`` and is left out of ``avg_loss`` and ``pnl``.
        ``win_rate`` is ``n_wins / (n_wins + n_losses)``, or None when the
        window is empty. ``n_estimated`` counts managed closes tagged
        ``backfill``.
        """
        if session not in {"calendar", "rth"}:
            raise ValueError(f"session must be 'calendar' or 'rth', got {session!r}")
        window_start, window_end = _outcome_window(start, end)
        with self._conn() as con:
            rows = con.execute(
                """
                SELECT credit, qty, exit_reason, close_debit, closed_at, updated_at,
                       close_price_source
                FROM spreads
                WHERE status=?
                """,
                (SpreadStatus.CLOSED.value,),
            ).fetchall()

        win_pnls: list[float] = []
        loss_pnls: list[float] = []
        pnl_values: list[float] = []
        n_wins = 0
        n_losses = 0
        n_missing_price = 0
        n_estimated = 0
        for row in rows:
            closed = _parse_ts(row["closed_at"]) or _parse_ts(row["updated_at"])
            if closed is None or not _in_outcome_window(
                closed, window_start, window_end, session
            ):
                continue
            bucket = _managed_bucket(str(row["exit_reason"] or ""))
            if bucket is None:
                continue
            if _optional_float(row["close_debit"]) is None:
                n_missing_price += 1
            if str(_row_get(row, "close_price_source", "") or "") == SOURCE_BACKFILL:
                n_estimated += 1
            pnl = _realized_dollars(
                credit=float(row["credit"]),
                qty=int(row["qty"]),
                reason=str(row["exit_reason"] or ""),
                close_debit=row["close_debit"],
                multiplier=multiplier,
                take_profit_frac=take_profit_frac,
                stop_multiple=stop_multiple,
            )
            if pnl is not None:
                pnl_values.append(pnl)
            if bucket == "win":
                n_wins += 1
                if pnl is not None:
                    win_pnls.append(pnl)
            else:
                n_losses += 1
                if pnl is not None:
                    loss_pnls.append(pnl)

        managed = n_wins + n_losses
        return ManagedOutcomeSummary(
            win_rate=(n_wins / managed) if managed else None,
            avg_win=_mean(win_pnls),
            avg_loss=_mean(loss_pnls),
            n_wins=n_wins,
            n_losses=n_losses,
            n_missing_price=n_missing_price,
            n_estimated=n_estimated,
            pnl=sum(pnl_values) if pnl_values else None,
        )


def _arm_from_row(row: sqlite3.Row) -> Arm:
    return Arm(
        id=row["id"],
        symbol=row["symbol"],
        side=Side(row["side"]),
        invalidation=row["invalidation"],
        zone_low=row["zone_low"],
        zone_high=row["zone_high"],
        confirmed_at=row["confirmed_at"],
        status=ArmStatus(row["status"]),
        reason=row["reason"],
        bar_index=row["bar_index"],
    )


def _spread_from_row(row: sqlite3.Row) -> OpenSpread:
    return OpenSpread(
        id=row["id"],
        underlying=row["underlying"],
        kind=SpreadKind(row["kind"]),
        short_occ=row["short_occ"],
        long_occ=row["long_occ"],
        width=row["width"],
        credit=row["credit"],
        qty=row["qty"],
        max_loss=row["max_loss"],
        invalidation=row["invalidation"],
        status=SpreadStatus(row["status"]),
        opened_at=row["opened_at"],
        broker_order_id=row["broker_order_id"],
        exit_reason=row["exit_reason"],
        thesis_intact=bool(row["thesis_intact"]),
        expiration=row["expiration"] or "",
        close_attempts=int(_row_get(row, "close_attempts", 0) or 0),
        exit_order_id=_row_get(row, "exit_order_id", None),
        last_close_error=str(_row_get(row, "last_close_error", "") or ""),
        close_debit=_optional_float(_row_get(row, "close_debit", None)),
        closed_at=str(_row_get(row, "closed_at", "") or ""),
        close_price_source=str(_row_get(row, "close_price_source", "") or ""),
        close_filled_qty=int(_row_get(row, "close_filled_qty", 0) or 0),
        close_fill_notional=_optional_float(_row_get(row, "close_fill_notional", None)),
        updated_at=str(_row_get(row, "updated_at", "") or ""),
    )


def _row_get(row: sqlite3.Row, key: str, default: Any) -> Any:
    keys = set(row.keys())
    return row[key] if key in keys else default


def _migrate_spreads(con: sqlite3.Connection) -> None:
    """Add fail-closed exit columns to journals created before this schema."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(spreads)").fetchall()}
    if "close_attempts" not in cols:
        con.execute(
            "ALTER TABLE spreads ADD COLUMN close_attempts INTEGER NOT NULL DEFAULT 0"
        )
    if "exit_order_id" not in cols:
        con.execute("ALTER TABLE spreads ADD COLUMN exit_order_id TEXT")
    if "last_close_error" not in cols:
        con.execute(
            "ALTER TABLE spreads ADD COLUMN last_close_error TEXT NOT NULL DEFAULT ''"
        )
    if "close_debit" not in cols:
        con.execute("ALTER TABLE spreads ADD COLUMN close_debit REAL")
    if "closed_at" not in cols:
        con.execute("ALTER TABLE spreads ADD COLUMN closed_at TEXT")
    if "close_price_source" not in cols:
        con.execute(
            "ALTER TABLE spreads ADD COLUMN close_price_source TEXT NOT NULL DEFAULT ''"
        )
    if "close_filled_qty" not in cols:
        con.execute(
            "ALTER TABLE spreads ADD COLUMN close_filled_qty INTEGER NOT NULL DEFAULT 0"
        )
    if "close_fill_notional" not in cols:
        con.execute("ALTER TABLE spreads ADD COLUMN close_fill_notional REAL")
    if "close_order_filled_seen" not in cols:
        con.execute(
            "ALTER TABLE spreads ADD COLUMN close_order_filled_seen INTEGER NOT NULL DEFAULT 0"
        )
    if "close_order_notional_seen" not in cols:
        con.execute("ALTER TABLE spreads ADD COLUMN close_order_notional_seen REAL")


@dataclass(frozen=True)
class ManagedOutcomeSummary:
    """Managed closes for one EOD window.

    ``avg_win`` and ``avg_loss`` are mean realized dollars. ``avg_loss`` is
    signed (negative when those closes lost money). ``win_rate`` is None when
    ``n_wins + n_losses`` is zero.
    """

    win_rate: Optional[float]
    avg_win: Optional[float]
    avg_loss: Optional[float]
    n_wins: int
    n_losses: int
    # Managed closes in the window whose close_debit is still NULL.
    n_missing_price: int = 0
    # Managed closes tagged close_price_source=backfill (estimated).
    n_estimated: int = 0
    # Sum of (credit - close_debit) * qty * multiplier for priced managed closes.
    pnl: Optional[float] = None


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def _mean(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _managed_bucket(reason: str) -> Optional[str]:
    """``win`` for take-profit, ``loss`` for stop / structure, else unmanaged."""
    if reason == TAKE_PROFIT_EXIT:
        return "win"
    if reason == STRUCTURE_BREAK_EXIT or reason.startswith("stop"):
        return "loss"
    return None


def _realized_dollars(
    *,
    credit: float,
    qty: int,
    reason: str,
    close_debit: Any,
    multiplier: float,
    take_profit_frac: float,
    stop_multiple: float,
) -> Optional[float]:
    debit = _optional_float(close_debit)
    if debit is None:
        if reason == TAKE_PROFIT_EXIT:
            debit = credit * (1.0 - take_profit_frac)
        elif reason.startswith("stop"):
            debit = stop_multiple * credit
        else:
            return None
    return (credit - debit) * qty * multiplier


def _parse_ts(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _outcome_window(
    start: Union[date, datetime], end: Union[date, datetime]
) -> tuple[datetime, datetime]:
    if isinstance(start, datetime) and isinstance(end, datetime):
        return _aware(start), _aware(end)
    if isinstance(start, date) and isinstance(end, date) and not isinstance(
        start, datetime
    ) and not isinstance(end, datetime):
        window_start = datetime.combine(start, time.min, tzinfo=ET)
        window_end = datetime.combine(end + timedelta(days=1), time.min, tzinfo=ET)
        return window_start, window_end
    raise TypeError("start and end must both be dates or both be datetimes")


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _in_outcome_window(
    ts: datetime, start: datetime, end: datetime, session: str
) -> bool:
    if not (start <= ts < end):
        return False
    if session == "calendar":
        return True
    local = as_et(ts)
    if local.weekday() >= 5:
        return False
    return RTH_OPEN <= local.time() < RTH_CLOSE
