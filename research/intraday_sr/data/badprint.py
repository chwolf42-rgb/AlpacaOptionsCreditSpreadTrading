"""Isolated 5m spikes that revert (spec v1.3.1 C1).

A bar is flagged only when its high or low is more than 0.5·ATR_d from the
median close of the ±2 neighbouring bars and the next bar's close is back
within 0.25·ATR_d of that median. The bar is kept. High and low are clamped
to the max/min of the open, the close, and that median. The unclamped
extremes stay in ``high_unclamped`` and ``low_unclamped``.

The flag and the clamp depend on bars t+1 and t+2, so they are visible only
from the close of bar t+2. Before that, ``prices_as_of`` shows the raw bar.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd


def prices_as_of(frame: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
    """High/low as the engine may see them at ``as_of``."""
    if frame.empty or "bad_print" not in frame.columns:
        return frame
    hidden = frame["bad_print"].to_numpy(dtype=bool) & (
        frame["bad_print_visible_at"] > pd.Timestamp(as_of)
    )
    if not bool(np.any(hidden)):
        return frame
    # A shallow frame plus two replaced columns. The hidden clamps are a few
    # bars, so this must not deep-copy the rest of the tape.
    mask = hidden.to_numpy(dtype=bool) if hasattr(hidden, "to_numpy") else np.asarray(hidden, dtype=bool)
    out = frame.copy(deep=False)
    high = np.array(frame["high"].to_numpy(copy=False), copy=True)
    low = np.array(frame["low"].to_numpy(copy=False), copy=True)
    flags = np.array(frame["bad_print"].to_numpy(copy=False), copy=True)
    high[mask] = np.asarray(frame["high_unclamped"].to_numpy(copy=False)[mask], dtype=high.dtype)
    low[mask] = np.asarray(frame["low_unclamped"].to_numpy(copy=False)[mask], dtype=low.dtype)
    flags[mask] = False
    out["high"] = high
    out["low"] = low
    out["bad_print"] = flags
    return out


def flag_summary(frame: pd.DataFrame) -> list[dict]:
    """Per-symbol flag counts. ``review`` is set above 0.1% of bars."""
    if frame.empty or "bad_print" not in frame.columns:
        return []
    rows = []
    for symbol, group in frame.groupby("symbol", sort=True, observed=True):
        total = int(len(group))
        flagged = int(group["bad_print"].sum())
        rate = flagged / total if total else 0.0
        rows.append(
            {
                "symbol": str(symbol),
                "bars": total,
                "flagged": flagged,
                "rate": rate,
                "review": rate > 0.001,
            }
        )
    return rows


def repair_bad_prints(frame: pd.DataFrame, atr_by_session: dict) -> tuple[pd.DataFrame, int]:
    """Clamp isolated spikes. Returns the frame and the number flagged.

    ``atr_by_session`` maps ``(symbol, session date)`` to ATR_d through the
    prior session. A bar without that ATR is left alone. Spikes are not dropped.
    """
    if frame.empty:
        empty = frame.copy()
        empty["high_unclamped"] = empty.get("high", pd.Series(dtype="float64"))
        empty["low_unclamped"] = empty.get("low", pd.Series(dtype="float64"))
        empty["bad_print"] = pd.Series(dtype=bool)
        empty["bad_print_visible_at"] = pd.Series(dtype="datetime64[ns, America/New_York]")
        return empty, 0
    out = frame.sort_values(["symbol", "ts"]).reset_index(drop=True)
    high = out["high"].to_numpy(dtype=np.float64).copy()
    low = out["low"].to_numpy(dtype=np.float64).copy()
    unclamped_high = high.copy()
    unclamped_low = low.copy()
    flagged = np.zeros(len(out), dtype=bool)
    available = list(out["available_at"])
    visible_at = list(available)
    symbols = out["symbol"].astype(str).to_numpy()
    sessions = [_as_date(value) for value in out["session"].tolist()]
    closes = out["close"].to_numpy(dtype=np.float64)
    opens = out["open"].to_numpy(dtype=np.float64)
    stamps = pd.to_datetime(out["ts"])
    count = 0
    unchecked = 0
    start = 0
    while start < len(out):
        stop = start + 1
        while stop < len(out) and symbols[stop] == symbols[start]:
            stop += 1
        flagged_here, missed = _repair_span(
            start,
            stop,
            symbols,
            sessions,
            stamps,
            opens,
            high,
            low,
            closes,
            unclamped_high,
            unclamped_low,
            flagged,
            visible_at,
            available,
            atr_by_session,
        )
        count += flagged_here
        unchecked += missed
        start = stop
    out["high"] = high
    out["low"] = low
    out["high_unclamped"] = unclamped_high
    out["low_unclamped"] = unclamped_low
    out["bad_print"] = flagged
    out["bad_print_visible_at"] = visible_at
    out.attrs["session_end_unchecked"] = unchecked
    return out, count


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _repair_span(
    start: int,
    stop: int,
    symbols,
    sessions,
    stamps: pd.Series,
    opens,
    high,
    low,
    closes,
    unclamped_high,
    unclamped_low,
    flagged,
    visible_at,
    available,
    atr_by_session: dict,
) -> tuple[int, int]:
    """Return flagged spikes and bars with no same-session t+1 and t+2.

    Those session-end bars stay unflagged and unclamped. Nothing is borrowed
    from the next session or from an official close.
    """
    count = 0
    unchecked = 0
    for index in range(start, stop):
        if not _has_forward_pair(index, stop, sessions, stamps):
            unchecked += 1
    for index in range(start + 2, stop - 2):
        if sessions[index] != sessions[index - 1] or sessions[index] != sessions[index + 2]:
            continue
        times = stamps.iloc[index - 2 : index + 3]
        delta = times.diff().iloc[1:]
        if not bool((delta == pd.Timedelta(minutes=5)).all()):
            continue
        atr = atr_by_session.get((symbols[index], sessions[index]))
        if atr is None or not np.isfinite(atr) or atr <= 0:
            continue
        neighbour = np.array(
            [closes[index - 2], closes[index - 1], closes[index + 1], closes[index + 2]],
            dtype=np.float64,
        )
        median = float(np.median(neighbour))
        far = high[index] > median + 0.5 * atr or low[index] < median - 0.5 * atr
        reverted = abs(closes[index + 1] - median) <= 0.25 * atr
        if not (far and reverted):
            continue
        floor = min(opens[index], closes[index], median)
        ceiling = max(opens[index], closes[index], median)
        high[index] = min(max(high[index], floor), ceiling)
        low[index] = min(max(low[index], floor), ceiling)
        flagged[index] = True
        visible_at[index] = available[index + 2]
        count += 1
    return count, unchecked


def _has_forward_pair(index: int, stop: int, sessions, stamps: pd.Series) -> bool:
    """True when t+1 and t+2 are the next two 5m bars of this same session."""
    if index + 2 >= stop:
        return False
    if sessions[index] != sessions[index + 1] or sessions[index] != sessions[index + 2]:
        return False
    opened = stamps.iloc[index]
    return (
        stamps.iloc[index + 1] - opened == pd.Timedelta(minutes=5)
        and stamps.iloc[index + 2] - stamps.iloc[index + 1] == pd.Timedelta(minutes=5)
    )
