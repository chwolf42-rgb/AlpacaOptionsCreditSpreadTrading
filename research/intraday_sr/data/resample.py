"""Higher-timeframe bars from 5m. S0 stub returns an empty frame.

D2-1 implements §4.4. The stub exists so lookahead test (d) can call it.
"""

from __future__ import annotations

import pandas as pd

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


def resample(bars: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Resample RTH 5m bars to ``15m``, ``1h``, or ``1d``.

    Stub: no higher-timeframe bar is emitted, so none is visible early.
    """
    if tf not in ("15m", "1h", "1d"):
        raise ValueError(f"unsupported timeframe {tf}")
    _ = bars
    return pd.DataFrame(columns=list(HTF_COLUMNS))
