"""Close-debit math shared by the journal, the live fill path, and backfill.

A close debit is premium points for one spread (short price − long price),
not dollars and not × qty. Realized dollars are
``(credit - close_debit) * qty * multiplier`` with multiplier 100.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

# Event kinds. A close without a price must emit CLOSE_PRICE_MISSING.
CLOSE_PRICE_MISSING = "close_price_missing"
CLOSE_DEBIT_BACKFILLED = "close_debit_backfilled"
CLOSE_PARTIAL = "close_partial"

# spreads.close_price_source
SOURCE_QUOTE = "quote"  # dry-run / observer: mid of the two legs
SOURCE_FILL = "fill"  # live paper: MLEG filled_avg_price
SOURCE_BACKFILL = "backfill"  # historical estimate, tagged estimated
SOURCE_MISSING = "missing"

# Quote mid is preferred when it printed shortly before the close.
# Minute-bar close covers a stamp just after the cash close.
QUOTE_MAX_AGE = timedelta(minutes=30)
BAR_MAX_AGE = timedelta(hours=18)

_OPEN_ORDER_STATES = frozenset(
    {
        "new",
        "accepted",
        "pending_new",
        "accepted_for_bidding",
        "pending_replace",
        "pending_cancel",
        "partially_filled",
        "held",
        "calculated",
    }
)
_FILLED_ORDER_STATES = frozenset({"filled"})


@dataclass(frozen=True)
class CloseOrderView:
    """Broker view of one mleg close. ``net_debit`` is per spread, not × qty."""

    order_id: str
    state: str  # open | filled | partial | dead
    filled_qty: int
    order_qty: int
    net_debit: Optional[float]
    filled_at: Optional[str]


def as_debit(value: Any) -> Optional[float]:
    """Finite premium. None when the feed did not produce a price."""
    if value is None or value is False or value == "":
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price):
        return None
    return price


def _field(obj: Any, *names: str) -> Any:
    if isinstance(obj, dict):
        for name in names:
            if name in obj and obj[name] not in (None, ""):
                return obj[name]
        return None
    for name in names:
        value = getattr(obj, name, None)
        if value not in (None, ""):
            return value
    return None


def parse_print_ts(value: Any) -> Optional[datetime]:
    """Alpaca bar/quote timestamp: datetime or RFC3339 string."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def quote_mids_from_prints(rows: Sequence[Any]) -> list[tuple[datetime, float]]:
    """Historical quote rows (SDK objects or raw ``bp``/``ap`` dicts) → mids."""
    points: list[tuple[datetime, float]] = []
    for row in rows:
        ts = parse_print_ts(_field(row, "timestamp", "t"))
        mid = quote_mid(_field(row, "bid_price", "bp"), _field(row, "ask_price", "ap"))
        if ts is None or mid is None:
            continue
        points.append((ts, mid))
    return points


def bar_closes_from_prints(rows: Sequence[Any]) -> list[tuple[datetime, float]]:
    """Minute bars → positive closes. Zero closes are not a price."""
    points: list[tuple[datetime, float]] = []
    for row in rows:
        ts = parse_print_ts(_field(row, "timestamp", "t"))
        px = as_debit(_field(row, "close", "c"))
        if ts is None or px is None or px <= 0:
            continue
        points.append((ts, px))
    return points


def quote_mid(bid: Any, ask: Any) -> Optional[float]:
    """Two-sided mid. Crossed, one-sided, or non-positive quotes are unusable."""
    b = as_debit(bid)
    a = as_debit(ask)
    if b is None or a is None:
        return None
    if b <= 0 or a <= 0 or a + 1e-12 < b:
        return None
    return (b + a) / 2.0


def spread_debit(short_px: Any, long_px: Any) -> Optional[float]:
    """Debit to close one spread: short leg price − long leg price."""
    short = as_debit(short_px)
    long = as_debit(long_px)
    if short is None or long is None:
        return None
    if short < 0 or long < 0:
        return None
    return short - long


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def select_price_at(
    points: Sequence[tuple[datetime, float]],
    at: datetime,
    *,
    max_age: timedelta,
) -> Optional[float]:
    """Latest print at or before ``at`` that is not older than ``max_age``."""
    when = _aware(at)
    best_ts: Optional[datetime] = None
    best_px: Optional[float] = None
    for ts, px in points:
        price = as_debit(px)
        if price is None or price < 0:
            continue
        stamp = _aware(ts)
        if stamp > when:
            continue
        if when - stamp > max_age:
            continue
        if best_ts is None or stamp > best_ts:
            best_ts = stamp
            best_px = price
    return best_px


def leg_price_at(
    *,
    quotes: Sequence[tuple[datetime, float]],
    bars: Sequence[tuple[datetime, float]],
    at: datetime,
    quote_max_age: timedelta = QUOTE_MAX_AGE,
    bar_max_age: timedelta = BAR_MAX_AGE,
) -> Optional[float]:
    """Prefer a recent quote mid, then a minute-bar close."""
    mid = select_price_at(quotes, at, max_age=quote_max_age)
    if mid is not None:
        return mid
    return select_price_at(bars, at, max_age=bar_max_age)


def _status_text(status: Any) -> str:
    value = getattr(status, "value", None)
    if isinstance(value, str):
        return value.lower()
    return str(status or "").lower()


def _qty(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _iso(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        ts = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return ts.isoformat()
    text = str(value).strip()
    return text or None


def classify_close_order(status: str, filled_qty: int, order_qty: int) -> str:
    """Map a broker status to open / filled / partial / dead.

    ``partially_filled`` stays ``open`` while the order is still working so
    the engine does not cancel it. A terminal order with a short fill is
    ``partial`` and the remainder is retried on a later poll.
    """
    if status in _FILLED_ORDER_STATES:
        return "filled"
    if order_qty > 0 and filled_qty >= order_qty and status not in _OPEN_ORDER_STATES:
        return "filled"
    if status in _OPEN_ORDER_STATES or status == "":
        return "open"
    if filled_qty > 0:
        return "partial"
    return "dead"


def net_debit_from_legs(legs: Sequence[Any]) -> Optional[float]:
    """Buy-to-close price − sell-to-close price when the parent net is absent."""
    buy: Optional[float] = None
    sell: Optional[float] = None
    for leg in legs:
        px = as_debit(_attr(leg, "filled_avg_price"))
        if px is None:
            continue
        intent_text = _enum_text(_attr(leg, "position_intent"))
        side_text = _enum_text(_attr(leg, "side"))
        price = abs(px)
        if "buy" in intent_text or side_text == "buy":
            buy = price
        elif "sell" in intent_text or side_text == "sell":
            sell = price
    if buy is None or sell is None:
        return None
    return buy - sell


def _enum_text(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").lower()


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def close_order_view_from_broker_order(order: Any) -> CloseOrderView:
    """Read an Alpaca (or test double) order into a :class:`CloseOrderView`.

    The parent ``filled_avg_price`` is the net debit of the mleg. Leg prices
    are only a fallback. A positive parent price is a debit; a negative one
    is a credit received to close and is kept signed so PnL stays honest.
    """
    status = _status_text(_attr(order, "status"))
    filled_qty = _qty(_attr(order, "filled_qty"))
    order_qty = _qty(_attr(order, "qty"))
    if status in _FILLED_ORDER_STATES and filled_qty <= 0 and order_qty > 0:
        filled_qty = order_qty
    state = classify_close_order(status, filled_qty, order_qty)
    net = as_debit(_attr(order, "filled_avg_price"))
    if net is None:
        legs = _attr(order, "legs") or []
        net = net_debit_from_legs(legs)
    filled_at = _iso(_attr(order, "filled_at")) or _iso(_attr(order, "updated_at"))
    return CloseOrderView(
        order_id=str(_attr(order, "id") or ""),
        state=state,
        filled_qty=filled_qty,
        order_qty=order_qty,
        net_debit=net,
        filled_at=filled_at,
    )
