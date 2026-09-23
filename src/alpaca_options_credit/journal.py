"""Durable SQLite journal for arms, open spreads, and exit state.

Isolated under var/options/ — never share with equity or crypto bots.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Union

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

    def close_spread(
        self,
        spread_id: str,
        reason: str,
        *,
        close_debit: Optional[float] = None,
        closed_at: Optional[str] = None,
    ) -> None:
        """Mark the spread closed. First close time and a later debit are kept.

        ``close_debit`` is the debit-to-close mark (premium points). Realized
        P&L for the EOD digest is ``(credit - close_debit) * qty * multiplier``.
        """
        stamp = closed_at or _now()
        with self._conn() as con:
            con.execute(
                """
                UPDATE spreads
                SET status=?,
                    exit_reason=?,
                    last_close_error='',
                    close_debit=COALESCE(?, close_debit),
                    closed_at=COALESCE(closed_at, ?),
                    updated_at=?
                WHERE id=?
                """,
                (
                    SpreadStatus.CLOSED.value,
                    reason,
                    close_debit,
                    stamp,
                    stamp,
                    spread_id,
                ),
            )

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
        When ``close_debit`` was not stored, take-profit and stop use the
        locked policy (capture ``take_profit_frac`` of credit; stop at
        ``stop_multiple`` × credit). A structure exit without a debit counts
        in ``n_losses`` and is left out of ``avg_loss``. ``win_rate`` is
        ``n_wins / (n_wins + n_losses)``, or None when the window is empty.
        """
        if session not in {"calendar", "rth"}:
            raise ValueError(f"session must be 'calendar' or 'rth', got {session!r}")
        window_start, window_end = _outcome_window(start, end)
        with self._conn() as con:
            rows = con.execute(
                """
                SELECT credit, qty, exit_reason, close_debit, closed_at, updated_at
                FROM spreads
                WHERE status=?
                """,
                (SpreadStatus.CLOSED.value,),
            ).fetchall()

        win_pnls: list[float] = []
        loss_pnls: list[float] = []
        n_wins = 0
        n_losses = 0
        for row in rows:
            closed = _parse_ts(row["closed_at"]) or _parse_ts(row["updated_at"])
            if closed is None or not _in_outcome_window(
                closed, window_start, window_end, session
            ):
                continue
            bucket = _managed_bucket(str(row["exit_reason"] or ""))
            if bucket is None:
                continue
            pnl = _realized_dollars(
                credit=float(row["credit"]),
                qty=int(row["qty"]),
                reason=str(row["exit_reason"] or ""),
                close_debit=row["close_debit"],
                multiplier=multiplier,
                take_profit_frac=take_profit_frac,
                stop_multiple=stop_multiple,
            )
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
