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
    PROPOSED = "proposed"
    OPEN = "open"
    EXITING = "exiting"  # exit latched; close not yet accepted
    CLOSED = "closed"


# Still on the book: proposed (observer), live, or close-pending.
LIVE_SPREAD_STATUSES = (
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
