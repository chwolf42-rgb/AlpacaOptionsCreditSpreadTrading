"""Backfill NULL close_debit from a historical price function. No network."""

import json
import sqlite3
from datetime import datetime, timezone

from alpaca_options_credit.backfill import backfill_close_debits, format_backfill_report
from alpaca_options_credit.cli import main
from alpaca_options_credit.journal import Journal
from alpaca_options_credit.models import OpenSpread, SpreadKind, SpreadStatus

CLOSE_AT = "2026-09-21T18:04:00+00:00"


def _legacy_closed(
    journal: Journal,
    spread_id: str,
    *,
    updated_at: str = CLOSE_AT,
    close_debit: float | None = None,
    underlying: str = "TSLA",
    reason: str = "structure_break",
    credit: float = 1.20,
    qty: int = 1,
    status: str = "closed",
) -> None:
    con = sqlite3.connect(journal.path)
    con.execute(
        """
        INSERT INTO spreads (
            id, underlying, kind, short_occ, long_occ, width, credit, qty,
            max_loss, invalidation, status, opened_at, exit_reason,
            thesis_intact, expiration, close_attempts, last_close_error,
            close_debit, closed_at, updated_at
        ) VALUES (
            ?, ?, 'bull_put_credit', ?, ?, 5, ?, ?,
            380, 100, ?, '2026-09-01T14:00:00+00:00', ?,
            1, '2026-10-16', 0, '',
            ?, NULL, ?
        )
        """,
        (
            spread_id,
            underlying,
            f"{underlying}261016P00370000",
            f"{underlying}261016P00365000",
            credit,
            qty,
            status,
            reason,
            close_debit,
            updated_at,
        ),
    )
    con.commit()
    con.close()


def _events(journal: Journal) -> list[tuple[str, dict]]:
    con = sqlite3.connect(journal.path)
    rows = con.execute("SELECT kind, payload FROM events").fetchall()
    con.close()
    return [(kind, json.loads(payload)) for kind, payload in rows]


def test_backfill_is_idempotent_and_skips_priced_rows(tmp_path):
    journal = Journal(tmp_path / "j.sqlite")
    _legacy_closed(journal, "priced", close_debit=0.55, underlying="EEM")
    _legacy_closed(journal, "gap", underlying="TSLA")
    _legacy_closed(journal, "open-row", status="open", underlying="MSFT")
    seen: list[datetime] = []

    def price_at(short: str, long: str, at: datetime) -> float:
        seen.append(at)
        assert "TSLA" in short
        return 1.75

    preview = backfill_close_debits(journal, price_at, dry_run=True)
    assert preview.n_preview == 1
    assert preview.n_skipped == 1
    assert preview.n_updated == 0
    assert journal.get_spread("gap").close_debit is None
    assert _events(journal) == []
    assert "preview" in format_backfill_report(preview)
    assert "dry_run=True" in format_backfill_report(preview)

    report = backfill_close_debits(journal, price_at, dry_run=False)
    assert report.n_updated == 1
    assert report.n_skipped == 1
    gap = journal.get_spread("gap")
    assert gap.close_debit == 1.75
    assert gap.closed_at == CLOSE_AT
    assert gap.updated_at == CLOSE_AT
    assert gap.close_price_source == "backfill"
    assert gap.status is SpreadStatus.CLOSED
    priced = journal.get_spread("priced")
    assert priced.close_debit == 0.55
    assert priced.close_price_source == ""
    assert journal.get_spread("open-row").status is SpreadStatus.OPEN
    kinds = _events(journal)
    assert len(kinds) == 1
    assert kinds[0][0] == "close_debit_backfilled"
    assert kinds[0][1]["estimated"] is True
    assert seen[0] == datetime(2026, 9, 21, 18, 4, tzinfo=timezone.utc)

    def blow_up(*_args, **_kwargs):
        raise AssertionError("priced rows must not be fetched again")

    again = backfill_close_debits(journal, blow_up, dry_run=False)
    assert again.n_updated == 0
    assert again.n_skipped == 2
    assert journal.get_spread("gap").close_debit == 1.75
    assert len(_events(journal)) == 1


def test_backfill_missing_price_stays_null_and_can_retry(tmp_path):
    journal = Journal(tmp_path / "j.sqlite")
    _legacy_closed(journal, "gap", underlying="META", reason="stop_2x_credit")

    missing = backfill_close_debits(journal, lambda *_: None, dry_run=False)
    assert missing.n_missing == 1
    assert journal.get_spread("gap").close_debit is None
    assert journal.get_spread("gap").closed_at == ""
    assert _events(journal)[0][0] == "close_price_missing"

    filled = backfill_close_debits(journal, lambda *_: 2.40, dry_run=False)
    assert filled.n_updated == 1
    row = journal.get_spread("gap")
    assert row.close_debit == 2.40
    assert row.close_price_source == "backfill"
    assert row.closed_at == CLOSE_AT
    summary = journal.summarize_managed_outcomes(
        datetime(2026, 9, 21, tzinfo=timezone.utc).date(),
        datetime(2026, 9, 21, tzinfo=timezone.utc).date(),
        session="rth",
    )
    # 18:04 UTC is 14:04 ET, inside RTH. Legacy stop_2x is a managed loss.
    # (1.20 - 2.40) * 1 * 100
    assert summary.n_estimated == 1
    assert summary.n_missing_price == 0
    assert summary.n_losses == 1
    assert summary.avg_loss == -120.0
    assert summary.pnl == -120.0


def test_backfill_cli_priced_journal_does_not_need_credentials(tmp_path, monkeypatch, capsys):
    for key in (
        "OPTIONS_APCA_API_KEY_ID",
        "OPTIONS_APCA_API_SECRET_KEY",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    journal = Journal(tmp_path / "j.sqlite")
    journal.upsert_spread(
        OpenSpread(
            id="sp1",
            underlying="EEM",
            kind=SpreadKind.BULL_PUT_CREDIT,
            short_occ="A",
            long_occ="B",
            width=5.0,
            credit=1.0,
            qty=1,
            max_loss=400.0,
            invalidation=60.0,
            status=SpreadStatus.OPEN,
            opened_at="2026-09-20T14:00:00+00:00",
        )
    )
    journal.close_spread("sp1", "structure_break", close_debit=0.40, closed_at=CLOSE_AT)
    code = main(
        [
            "backfill-close-debits",
            "--journal",
            str(journal.path),
            "--log-level",
            "ERROR",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "skipped=1" in out
    assert journal.get_spread("sp1").close_debit == 0.40


def test_backfill_cli_preview_does_not_write_without_a_price(tmp_path, monkeypatch):
    for key in (
        "OPTIONS_APCA_API_KEY_ID",
        "OPTIONS_APCA_API_SECRET_KEY",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    journal = Journal(tmp_path / "j.sqlite")
    _legacy_closed(journal, "gap")
    code = main(
        [
            "backfill-close-debits",
            "--journal",
            str(journal.path),
            "--dry-run",
            "--log-level",
            "ERROR",
        ]
    )
    assert code == 2
    assert journal.get_spread("gap").close_debit is None
    assert journal.get_spread("gap").closed_at == ""
