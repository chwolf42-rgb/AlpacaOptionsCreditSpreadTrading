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
    CLOSED = "closed"


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
