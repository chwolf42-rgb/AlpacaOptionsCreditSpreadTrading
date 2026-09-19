"""Durable SQLite journal for arms, open spreads, and exit state.

Isolated under var/options/ — never share with equity or crypto bots.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from alpaca_options_credit.models import (
    LIVE_SPREAD_STATUSES,
    Arm,
    ArmStatus,
    OpenSpread,
    Side,
    SpreadKind,
    SpreadStatus,
)

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
        with self._conn() as con:
            con.execute(
                """
                UPDATE spreads
                SET exit_order_id=?, last_close_error='', updated_at=?
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

    def close_spread(self, spread_id: str, reason: str) -> None:
        with self._conn() as con:
            con.execute(
                """
                UPDATE spreads
                SET status=?, exit_reason=?, last_close_error='', updated_at=?
                WHERE id=?
                """,
                (SpreadStatus.CLOSED.value, reason, _now(), spread_id),
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
