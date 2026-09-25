"""EOD managed win/loss summary over closed journal spreads."""

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from alpaca_options_credit.journal import Journal
from alpaca_options_credit.models import OpenSpread, SpreadKind, SpreadStatus


DAY = date(2026, 3, 3)  # Tuesday
RTH = "2026-03-03T15:00:00+00:00"  # 10:00 ET
AFTER_HOURS = "2026-03-03T21:30:00+00:00"  # 16:30 ET
NEXT_RTH = "2026-03-04T15:00:00+00:00"
WEEKEND = "2026-03-07T15:00:00+00:00"  # Saturday 10:00 ET


def _open(journal: Journal, spread_id: str, *, credit: float, qty: int) -> None:
    journal.upsert_spread(
        OpenSpread(
            id=spread_id,
            underlying="SPY",
            kind=SpreadKind.BULL_PUT_CREDIT,
            short_occ="A",
            long_occ="B",
            width=5.0,
            credit=credit,
            qty=qty,
            max_loss=380.0,
            invalidation=100.0,
            status=SpreadStatus.OPEN,
            opened_at="2026-03-02T15:00:00+00:00",
        )
    )


def _close(
    journal: Journal,
    spread_id: str,
    reason: str,
    *,
    credit: float,
    qty: int,
    closed_at: str,
    close_debit: float | None = None,
) -> None:
    _open(journal, spread_id, credit=credit, qty=qty)
    journal.close_spread(
        spread_id, reason, close_debit=close_debit, closed_at=closed_at
    )


def test_calendar_day_and_rth_session(tmp_path: Path):
    journal = Journal(tmp_path / "j.sqlite")
    _close(journal, "tp1", "take_profit", credit=1.20, qty=1, closed_at=RTH, close_debit=0.60)
    _close(journal, "tp2", "take_profit", credit=2.00, qty=2, closed_at=RTH, close_debit=1.00)
    _close(journal, "st", "stop_credit", credit=1.20, qty=1, closed_at=RTH, close_debit=1.80)
    _close(
        journal, "br", "structure_break", credit=1.00, qty=1, closed_at=RTH, close_debit=1.40
    )
    _close(
        journal,
        "ah",
        "structure_break",
        credit=1.00,
        qty=1,
        closed_at=AFTER_HOURS,
        close_debit=2.00,
    )
    _close(
        journal, "next", "take_profit", credit=1.00, qty=1, closed_at=NEXT_RTH, close_debit=0.40
    )
    _close(journal, "nk", "naked_leg", credit=1.00, qty=1, closed_at=RTH, close_debit=0.10)
    _close(
        journal, "wk", "take_profit", credit=1.00, qty=1, closed_at=WEEKEND, close_debit=0.40
    )

    calendar = journal.summarize_managed_outcomes(DAY, DAY)
    assert calendar.n_wins == 2
    assert calendar.n_losses == 3
    assert calendar.win_rate == pytest.approx(2 / 5)
    # (0.60 * 1 + 1.00 * 2) * 100 / 2
    assert calendar.avg_win == pytest.approx(130.0)
    # (-0.60 + -0.40 + -1.00) * 100 / 3
    assert calendar.avg_loss == pytest.approx(-200.0 / 3)
    assert calendar.n_missing_price == 0
    assert calendar.n_estimated == 0
    # 60 + 200 - 60 - 40 - 100
    assert calendar.pnl == pytest.approx(60.0)

    rth = journal.summarize_managed_outcomes(DAY, DAY, session="rth")
    assert rth.n_wins == 2
    assert rth.n_losses == 2
    assert rth.win_rate == pytest.approx(0.5)
    assert rth.avg_win == pytest.approx(130.0)
    assert rth.avg_loss == pytest.approx(-50.0)
    assert rth.pnl == pytest.approx(160.0)

    weekend = journal.summarize_managed_outcomes(date(2026, 3, 7), date(2026, 3, 7))
    assert weekend.n_wins == 1
    assert weekend.avg_win == pytest.approx(60.0)
    weekend_rth = journal.summarize_managed_outcomes(
        date(2026, 3, 7), date(2026, 3, 7), session="rth"
    )
    assert weekend_rth.n_wins == 0
    assert weekend_rth.win_rate is None


def test_legacy_stop_and_unpriced_structure(tmp_path: Path):
    journal = Journal(tmp_path / "j.sqlite")
    _close(journal, "old", "stop_2x_credit", credit=1.20, qty=1, closed_at=RTH)
    _close(journal, "brk", "structure_break", credit=1.00, qty=1, closed_at=RTH)
    _close(journal, "tp", "take_profit", credit=1.00, qty=1, closed_at=RTH)

    summary = journal.summarize_managed_outcomes(DAY, DAY, session="rth")
    assert summary.n_wins == 1
    assert summary.n_losses == 2
    assert summary.win_rate == pytest.approx(1 / 3)
    # Implied TP captures 50% of 1.00 credit.
    assert summary.avg_win == pytest.approx(50.0)
    # Implied 1.5× stop on 1.20; unpriced structure is counted but not averaged.
    assert summary.avg_loss == pytest.approx(-60.0)
    assert summary.n_missing_price == 3
    assert summary.n_estimated == 0
    # Implied stop -60 plus implied take-profit +50. Structure has no dollars.
    assert summary.pnl == pytest.approx(-10.0)


def test_empty_window_and_half_open_datetimes(tmp_path: Path):
    journal = Journal(tmp_path / "j.sqlite")
    empty = journal.summarize_managed_outcomes(DAY, DAY)
    assert empty.win_rate is None
    assert empty.avg_win is None
    assert empty.avg_loss is None
    assert empty.n_wins == 0
    assert empty.n_losses == 0

    _close(journal, "tp", "take_profit", credit=1.20, qty=1, closed_at=RTH, close_debit=0.60)
    start = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    end = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
    at_end = journal.summarize_managed_outcomes(start, end)
    assert at_end.n_wins == 0
    later = datetime(2026, 3, 3, 15, 1, tzinfo=timezone.utc)
    inside = journal.summarize_managed_outcomes(start, later)
    assert inside.n_wins == 1
    assert inside.avg_win == pytest.approx(60.0)

    with pytest.raises(ValueError, match="session"):
        journal.summarize_managed_outcomes(DAY, DAY, session="extended")


def test_old_journal_gains_close_columns(tmp_path: Path):
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
            close_attempts INTEGER NOT NULL DEFAULT 0,
            exit_order_id TEXT,
            last_close_error TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )
    con.commit()
    con.close()

    journal = Journal(path)
    _close(journal, "tp", "take_profit", credit=1.20, qty=1, closed_at=RTH, close_debit=0.60)
    loaded = journal.get_spread("tp")
    assert loaded is not None
    assert loaded.close_debit == pytest.approx(0.60)
    assert loaded.closed_at == RTH
    summary = journal.summarize_managed_outcomes(DAY, DAY, session="rth")
    assert summary.n_wins == 1
    assert summary.avg_win == pytest.approx(60.0)


def test_backfilled_rows_are_in_the_digest(tmp_path: Path):
    journal = Journal(tmp_path / "j.sqlite")
    _close(journal, "est", "structure_break", credit=1.00, qty=2, closed_at=RTH)
    _close(journal, "gap", "stop_credit", credit=1.20, qty=1, closed_at=RTH)
    assert journal.write_backfilled_close(
        "est",
        1.40,
        RTH,
        underlying="SPY",
        exit_reason="structure_break",
    )
    # A second write must not replace the estimated debit.
    assert journal.write_backfilled_close("est", 9.99, RTH) is False

    summary = journal.summarize_managed_outcomes(DAY, DAY, session="rth")
    assert summary.n_estimated == 1
    assert summary.n_missing_price == 1
    assert summary.n_losses == 2
    # Backfill (1.00 - 1.40) * 2 * 100 = -80. Implied stop is not in this average
    # alongside it: the stop row is still missing a stored debit, so the
    # locked 1.5× policy supplies -60, and the mean is (-80 + -60) / 2.
    assert summary.avg_loss == pytest.approx(-70.0)
    assert summary.pnl == pytest.approx(-140.0)
