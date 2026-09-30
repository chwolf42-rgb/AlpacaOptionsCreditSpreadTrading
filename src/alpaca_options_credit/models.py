from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class SpreadKind(str, Enum):
    BULL_PUT_CREDIT = "bull_put_credit"
    BEAR_CALL_CREDIT = "bear_call_credit"


class ArmStatus(str, Enum):
    ARMED = "armed"
    CANCELLED = "cancelled"
    TRIGGERED = "triggered"
    EXPIRED = "expired"


class SpreadStatus(str, Enum):
    PROPOSED = "proposed"  # observer; no broker order
    PENDING_ENTRY = "pending_entry"  # live mleg accepted, not filled
    OPEN = "open"  # entry filled; both legs are held
    EXITING = "exiting"  # exit latched; close not yet accepted
    CLOSED = "closed"
    ENTRY_EXPIRED = "entry_expired"  # day order ended with no fill
    CANCELLED = "cancelled"  # entry canceled or rejected with no fill


# Reserves risk (concurrent, portfolio, one underlying). Terminal entry
# states are absent so an unfilled day order frees the slot.
LIVE_SPREAD_STATUSES = (
    SpreadStatus.PROPOSED,
    SpreadStatus.PENDING_ENTRY,
    SpreadStatus.OPEN,
    SpreadStatus.EXITING,
)

# Actually held, or an observer proposal. A working entry is not held:
# do not mark-to-close it, flatten it, or flag it overnight.
HELD_SPREAD_STATUSES = (
    SpreadStatus.PROPOSED,
    SpreadStatus.OPEN,
    SpreadStatus.EXITING,
)


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Arm:
    id: str
    symbol: str
    side: Side
    invalidation: float
    zone_low: float
    zone_high: float
    confirmed_at: str
    status: ArmStatus
    reason: str = ""
    bar_index: int = 0


@dataclass
class ContractQuote:
    occ: str
    strike: float
    expiration: date
    right: str  # "put" | "call"
    bid: float
    ask: float
    # None when the feed did not send it. Quote filters treat unknown OI as
    # "not thin" unless spreads.min_open_interest is enabled.
    open_interest: Optional[int] = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass
class SpreadProposal:
    underlying: str
    kind: SpreadKind
    short: ContractQuote
    long: ContractQuote
    width: float
    credit: float
    qty: int
    max_loss: float
    invalidation: float
    reason: str
    skip: bool = False
    skip_reason: str = ""


@dataclass
class OpenSpread:
    id: str
    underlying: str
    kind: SpreadKind
    short_occ: str
    long_occ: str
    width: float
    credit: float
    qty: int
    max_loss: float
    invalidation: float
    status: SpreadStatus
    opened_at: str
    broker_order_id: Optional[str] = None
    exit_reason: str = ""
    thesis_intact: bool = True
    expiration: str = ""
    close_attempts: int = 0
    exit_order_id: Optional[str] = None
    last_close_error: str = ""
    # Debit-to-close at the accepted close, and when that close was journaled.
    # EOD managed win/loss uses these; both stay empty until the spread closes.
    close_debit: Optional[float] = None
    closed_at: str = ""
    # quote | fill | backfill | missing | "" (legacy, before this column).
    close_price_source: str = ""
    # Cumulative mleg contracts filled across partial close orders.
    close_filled_qty: int = 0
    # Sum of (net debit × filled qty) so a later fill can finish the average.
    close_fill_notional: Optional[float] = None
    # Journal updated_at. Legacy closes stored the close clock only here.
    updated_at: str = ""
    # Live entry mleg. ``credit`` stays the limit until the fill is known.
    entry_order_id: Optional[str] = None
    entry_filled_qty: int = 0
    entry_limit_credit: Optional[float] = None
