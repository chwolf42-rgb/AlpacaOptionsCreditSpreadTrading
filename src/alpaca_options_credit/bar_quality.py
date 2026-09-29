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


def next_bar_close(timeframe: str, now: datetime) -> datetime:
    """UTC instant of the next close strictly after ``now``.

    Hourly bars in this bot are clock-hour UTC: a bar timestamped 14:00
    closes at 15:00, matching ``is_bar_closed``. Daily bars close at 16:00
    America/New_York, including across weekends. A series fetched at ``now``
    is still the latest closed bars until this instant.
    """
    now_utc = _as_utc(now)
    tf = str(timeframe)
    if tf in HOURLY_TIMEFRAMES:
        hour = now_utc.replace(minute=0, second=0, microsecond=0)
        return hour + timedelta(hours=1)
    if tf in MIN30_TIMEFRAMES:
        minute = 0 if now_utc.minute < 30 else 30
        slot = now_utc.replace(minute=minute, second=0, microsecond=0)
        return slot + timedelta(minutes=30)
    local = now_utc.astimezone(ET)
    close_today = datetime.combine(local.date(), RTH_CLOSE, tzinfo=ET)
    if local.weekday() < 5 and local < close_today:
        return close_today.astimezone(timezone.utc)
    day = local.date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, RTH_CLOSE, tzinfo=ET).astimezone(timezone.utc)


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


def last_completed_daily_bar(bars: list[Bar], now: datetime) -> Optional[Bar]:
    """Daily close that entry rules may trust.

    A newest bar still inside its session is forming: use the prior completed
    session. A newest bar timestamped after `now` is kept as-is (synthetic
    fixture tails are shifted past the clock on purpose). Live fetches already
    drop the forming session before the engine sees them.
    """
    if not bars:
        return None
    last = max(bars, key=lambda b: _as_utc(b.ts))
    if _as_utc(last.ts) <= _as_utc(now) and not is_bar_closed(last.ts, "1Day", now):
        closed = [b for b in bars if b is not last and is_bar_closed(b.ts, "1Day", now)]
        if not closed:
            return None
        return max(closed, key=lambda b: _as_utc(b.ts))
    return last


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
