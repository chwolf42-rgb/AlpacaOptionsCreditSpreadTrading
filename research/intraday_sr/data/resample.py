"""Resample RTH 5m bars to 15m, 1h, and 1d (spec §4.4).

Buckets are anchored at 09:30 ET. A higher-timeframe bar is emitted only
when the 5m bar that closes on the bucket end is present. Nothing is
forward-filled. The last 1h bucket of a session is the 30-minute remainder
(15:30–16:00, or 12:30–13:00 on an early close) and is flagged ``partial``.
A daily bar is the RTH aggregate and is not flagged partial.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from research.intraday_sr.data.calendar import session_close, session_open
from research.intraday_sr.types import ET

HTF_COLUMNS = (
    "symbol",
    "tf",
    "ts",
    "available_at",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vwap",
    "trades",
    "session",
    "adj_factor",
    "partial",
)

_WIDTH = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1)}


def resample(bars: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Build completed ``tf`` bars from 5m rows. ``tf`` is 15m, 1h, or 1d."""
    if tf not in ("15m", "1h", "1d"):
        raise ValueError(f"unsupported timeframe {tf}")
    empty = pd.DataFrame(columns=list(HTF_COLUMNS))
    if bars is None or len(bars) == 0:
        return empty
    frame = bars.sort_values(["symbol", "ts"])
    rows: list[dict] = []
    for (symbol, session), group in frame.groupby(["symbol", "session"], sort=True):
        if isinstance(session, datetime):
            session = session.date()
        elif not isinstance(session, date):
            session = pd.Timestamp(session).date()
        rows.extend(_session_buckets(group, symbol, session, tf))
    if not rows:
        return empty
    out = pd.DataFrame(rows)
    return out.loc[:, list(HTF_COLUMNS)].reset_index(drop=True)


def _session_buckets(group: pd.DataFrame, symbol: str, session, tf: str) -> list[dict]:
    close_at = session_close(session)
    open_at = session_open(session)
    if close_at is None or open_at is None:
        return []
    group = group.sort_values("ts")
    if tf == "1d":
        buckets = [(open_at, close_at, False)]
    else:
        width = _WIDTH[tf]
        buckets = []
        cursor = open_at
        while cursor < close_at:
            end = cursor + width
            if end > close_at:
                end = close_at
            partial = tf == "1h" and (end - cursor) < width
            buckets.append((cursor, end, partial))
            cursor = end
    built: list[dict] = []
    for start, end, partial in buckets:
        # 5m bars that open in [start, end) close by `end` on this grid.
        # The bucket stays hidden until the bar that closes exactly at `end` exists.
        const = group[(group["ts"] >= start) & (group["available_at"] <= end)]
        if const.empty or not (const["available_at"] == end).any():
            continue
        built.append(_aggregate(const, symbol, session, tf, start, end, partial))
    return built


def _aggregate(
    const: pd.DataFrame,
    symbol: str,
    session,
    tf: str,
    start: datetime,
    end: datetime,
    partial: bool,
) -> dict:
    const = const.sort_values("ts")
    volume = const["volume"].to_numpy(dtype=np.float64)
    vwap = const["vwap"].to_numpy(dtype=np.float64)
    vol_sum = float(volume.sum())
    if vol_sum > 0:
        vwap_out = float((vwap * volume).sum() / vol_sum)
    else:
        vwap_out = float(const["close"].iloc[-1])
    factor = float(const["adj_factor"].iloc[0]) if "adj_factor" in const.columns else 1.0
    start_et = start.astimezone(ET) if start.tzinfo else start
    end_et = end.astimezone(ET) if end.tzinfo else end
    return {
        "symbol": symbol,
        "tf": tf,
        "ts": start_et,
        "available_at": end_et,
        "open": np.float32(const["open"].iloc[0]),
        "high": np.float32(const["high"].max()),
        "low": np.float32(const["low"].min()),
        "close": np.float32(const["close"].iloc[-1]),
        "volume": vol_sum,
        "vwap": np.float32(vwap_out),
        "trades": int(const["trades"].sum()) if "trades" in const.columns else 0,
        "session": session,
        "adj_factor": np.float32(factor),
        "partial": bool(partial),
    }
