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
                                     broker_order_id, exit_reason, thesis_intact, expiration, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    exit_reason=excluded.exit_reason,
                    thesis_intact=excluded.thesis_intact,
                    broker_order_id=excluded.broker_order_id,
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
                    _now(),
                ),
            )

    def open_spreads(self) -> list[OpenSpread]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM spreads WHERE status IN (?, ?) ORDER BY opened_at",
                (SpreadStatus.OPEN.value, SpreadStatus.PROPOSED.value),
            ).fetchall()
        return [_spread_from_row(r) for r in rows]

    def open_for_symbol(self, symbol: str) -> Optional[OpenSpread]:
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM spreads WHERE underlying=? AND status IN (?, ?) LIMIT 1",
                (symbol, SpreadStatus.OPEN.value, SpreadStatus.PROPOSED.value),
            ).fetchone()
        return _spread_from_row(row) if row else None

    def close_spread(self, spread_id: str, reason: str) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE spreads SET status=?, exit_reason=?, updated_at=? WHERE id=?",
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
    )
