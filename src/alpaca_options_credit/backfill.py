"""One-off backfill of close_debit for closed spreads that never recorded one.

Reads historical option quotes (then minute bars) at the journal close time.
Rows that already have ``close_debit`` are never updated. A preview run
(``dry_run=True``) writes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from alpaca_options_credit.close_prices import CLOSE_PRICE_MISSING, as_debit
from alpaca_options_credit.journal import Journal, _parse_ts
from alpaca_options_credit.models import OpenSpread

log = logging.getLogger(__name__)

# (short_occ, long_occ, close_time) -> per-spread debit, or None.
PriceAt = Callable[[str, str, datetime], Optional[float]]


@dataclass(frozen=True)
class BackfillRow:
    spread_id: str
    underlying: str
    action: str  # skipped | preview | updated | missing
    close_debit: Optional[float] = None
    closed_at: Optional[str] = None
    exit_reason: str = ""


@dataclass
class BackfillReport:
    dry_run: bool
    results: list[BackfillRow] = field(default_factory=list)

    @property
    def n_updated(self) -> int:
        return sum(1 for row in self.results if row.action == "updated")

    @property
    def n_preview(self) -> int:
        return sum(1 for row in self.results if row.action == "preview")

    @property
    def n_skipped(self) -> int:
        return sum(1 for row in self.results if row.action == "skipped")

    @property
    def n_missing(self) -> int:
        return sum(1 for row in self.results if row.action == "missing")


def backfill_close_debits(
    journal: Journal,
    price_at: PriceAt,
    *,
    dry_run: bool = False,
) -> BackfillReport:
    """Fill NULL ``close_debit`` on closed rows. Idempotent.

    ``dry_run`` previews the debit and does not write the row or an event.
    The as-of clock is ``closed_at`` when set, otherwise ``updated_at``
    (legacy closes stored the close time only there).
    """
    report = BackfillReport(dry_run=dry_run)
    for spread in journal.closed_spreads():
        if spread.close_debit is not None:
            report.results.append(
                BackfillRow(
                    spread_id=spread.id,
                    underlying=spread.underlying,
                    action="skipped",
                    close_debit=spread.close_debit,
                    closed_at=spread.closed_at or spread.updated_at,
                    exit_reason=spread.exit_reason,
                )
            )
            continue
        as_of_text = spread.closed_at or spread.updated_at
        as_of = _parse_ts(as_of_text)
        debit: Optional[float] = None
        if as_of is not None:
            try:
                debit = as_debit(
                    price_at(spread.short_occ, spread.long_occ, as_of)
                )
            except Exception as exc:
                log.warning(
                    "backfill price failed spread=%s underlying=%s: %s",
                    spread.id,
                    spread.underlying,
                    type(exc).__name__,
                )
                debit = None
        if debit is None:
            report.results.append(
                BackfillRow(
                    spread_id=spread.id,
                    underlying=spread.underlying,
                    action="missing",
                    closed_at=as_of_text or None,
                    exit_reason=spread.exit_reason,
                )
            )
            if not dry_run:
                _note_missing(journal, spread, as_of_text)
            continue
        closed_at = as_of_text
        if dry_run:
            report.results.append(
                BackfillRow(
                    spread_id=spread.id,
                    underlying=spread.underlying,
                    action="preview",
                    close_debit=debit,
                    closed_at=closed_at,
                    exit_reason=spread.exit_reason,
                )
            )
            continue
        wrote = journal.write_backfilled_close(
            spread.id,
            debit,
            closed_at,
            underlying=spread.underlying,
            exit_reason=spread.exit_reason,
        )
        report.results.append(
            BackfillRow(
                spread_id=spread.id,
                underlying=spread.underlying,
                action="updated" if wrote else "skipped",
                close_debit=debit if wrote else spread.close_debit,
                closed_at=closed_at,
                exit_reason=spread.exit_reason,
            )
        )
    return report


def format_backfill_report(report: BackfillReport) -> str:
    lines: list[str] = []
    for row in report.results:
        if row.action == "skipped":
            lines.append(
                f"skip {row.underlying} {row.spread_id} close_debit already set "
                f"({row.close_debit})"
            )
        elif row.action == "preview":
            lines.append(
                f"preview {row.underlying} {row.spread_id} "
                f"close_debit={row.close_debit} closed_at={row.closed_at} estimated"
            )
        elif row.action == "updated":
            lines.append(
                f"updated {row.underlying} {row.spread_id} "
                f"close_debit={row.close_debit} closed_at={row.closed_at} estimated"
            )
        else:
            lines.append(
                f"missing {row.underlying} {row.spread_id} "
                f"as_of={row.closed_at} no historical price"
            )
    lines.append(
        "backfill "
        f"updated={report.n_updated} preview={report.n_preview} "
        f"skipped={report.n_skipped} missing={report.n_missing} "
        f"dry_run={report.dry_run}"
    )
    return "\n".join(lines)


def _note_missing(journal: Journal, spread: OpenSpread, as_of_text: str) -> None:
    log.warning(
        "close price missing spread=%s underlying=%s as_of=%s — "
        "backfill left close_debit NULL",
        spread.id,
        spread.underlying,
        as_of_text,
    )
    journal.log_event(
        CLOSE_PRICE_MISSING,
        spread.underlying,
        {
            "spread_id": spread.id,
            "reason": spread.exit_reason,
            "source": "backfill",
            "as_of": as_of_text,
            "estimated": False,
        },
    )
