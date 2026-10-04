"""Cboe daily VIX / VIX9D CSV loader.

A decision on day ``d`` uses the close of the last session strictly before
``d``. The same-day close is never returned. Reads go through the path chokepoint.
"""

from __future__ import annotations

import io
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

from research.intraday_sr.io import read_bytes
from research.intraday_sr.types import ET


def load_vix_csv(path: str | Path) -> pd.DataFrame:
    """Load a Cboe history CSV (DATE, OPEN, HIGH, LOW, CLOSE)."""
    frame = pd.read_csv(io.BytesIO(read_bytes(path)))
    columns = {str(col).strip().upper(): col for col in frame.columns}
    required = ("DATE", "OPEN", "HIGH", "LOW", "CLOSE")
    missing = [name for name in required if name not in columns]
    if missing:
        raise ValueError(f"{path} is missing {missing}")
    out = pd.DataFrame(
        {
            "open": pd.to_numeric(frame[columns["OPEN"]], errors="coerce"),
            "high": pd.to_numeric(frame[columns["HIGH"]], errors="coerce"),
            "low": pd.to_numeric(frame[columns["LOW"]], errors="coerce"),
            "close": pd.to_numeric(frame[columns["CLOSE"]], errors="coerce"),
        }
    )
    parsed = pd.to_datetime(frame[columns["DATE"]], format="mixed").dt.date
    out.index = pd.Index(parsed, name="date")
    out = out.dropna(subset=["close"]).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


def prior_close(history: pd.DataFrame, day: date) -> float:
    """Close of the last row strictly before ``day``."""
    earlier = history.loc[history.index < day, "close"]
    if earlier.empty:
        raise KeyError(f"no VIX close before {day.isoformat()}")
    return float(earlier.iloc[-1])


def asof_timestamp(day: date) -> datetime:
    """09:30 ET on ``day``, when the prior close may be used."""
    return datetime.combine(day, time(9, 30), tzinfo=ET)
