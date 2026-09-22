"""Closed-bar selection and fail-closed freshness for daily / 1Hour series.

Alpaca stock bars are oldest-first: a request ``limit`` keeps the oldest rows
in the window, not the newest. Callers drop that limit, then keep the newest
*closed* bars here. Structure, arm, and entry must not run on a stale tail.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from alpaca_options_credit.models import Bar
from alpaca_options_credit.rth import ET, RTH_CLOSE

# Journal / log reason. Distinct from daily_not_confirmed and arm cancels.
STALE_BARS = "stale_bars"

DAILY_TIMEFRAMES = frozenset({"1Day", "1D", "Day", "daily"})
HOURLY_TIMEFRAMES = frozenset({"1Hour", "1H", "60Min"})
MIN30_TIMEFRAMES = frozenset({"30Min", "30T"})

# Wednesday session → Monday reopen (Thanksgiving) is the long scheduled gap.
# Daily timestamps are the session start, so age at the next open can exceed
# 5 days. Six days still rejects the oldest-limit truncation (~90 days).
MAX_STALE_BAR_AGE = timedelta(days=6)


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def bar_period_end(ts: datetime, timeframe: str) -> datetime:
    """UTC instant when this bar's session or bucket is closed."""
    ts_utc = _as_utc(ts)
    tf = str(timeframe)
    if tf in DAILY_TIMEFRAMES:
        session = ts_utc.astimezone(ET).date()
        close_et = datetime.combine(session, RTH_CLOSE, tzinfo=ET)
        return close_et.astimezone(timezone.utc)
    if tf in HOURLY_TIMEFRAMES:
        return ts_utc + timedelta(hours=1)
    if tf in MIN30_TIMEFRAMES:
        return ts_utc + timedelta(minutes=30)
    return ts_utc


def is_bar_closed(ts: datetime, timeframe: str, now: datetime) -> bool:
    return _as_utc(now) >= bar_period_end(ts, timeframe)


def newest_closed_bars(
    bars: list[Bar],
    timeframe: str,
    limit: int,
    now: datetime,
) -> list[Bar]:
    """Chronological newest `limit` bars whose period has closed."""
    closed = [b for b in bars if is_bar_closed(b.ts, timeframe, now)]
    closed.sort(key=lambda b: _as_utc(b.ts))
    if limit <= 0:
        return []
    if len(closed) <= limit:
        return closed
    return closed[-limit:]


def stale_bars_detail(bars: list[Bar], timeframe: str, now: datetime) -> Optional[str]:
    """None when the newest bar is recent enough to trade. Otherwise a skip detail.

    Empty series fail closed. A timestamp in the future (clock skew) is fresh.
    """
    tf = str(timeframe)
    if not bars:
        return f"{tf}:empty"
    try:
        last = max(_as_utc(b.ts) for b in bars)
    except (TypeError, ValueError):
        return f"{tf}:unreadable"
    age = _as_utc(now) - last
    if age > MAX_STALE_BAR_AGE:
        return f"{tf}:last={last.date().isoformat()}:age_days={age.days}"
    return None
