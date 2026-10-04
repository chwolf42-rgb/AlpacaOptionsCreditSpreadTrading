"""Session adjustment factors from the daily raw and adjusted tapes.

``f_d = raw_close / adj_close``. It is constant inside the session. Round
numbers use the as-traded price ``adjusted * f_d``. Paths are parameters;
this module does not look for a tape on disk by itself.

A missing symbol-session is not filled from another day.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd


def load_factors(raw_path: str | Path, adj_path: str | Path) -> pd.Series:
    """Return a Series indexed by ``(symbol, session date)`` of ``raw/adj``.

    Both files use the daily schema ``date`` (str or timestamp), ``symbol``,
    and ``close``. Other columns are ignored.
    """
    raw = _read_closes(raw_path).rename("close_raw")
    adj = _read_closes(adj_path).rename("close_adj")
    merged = pd.concat([raw, adj], axis=1, join="inner")
    factor = merged["close_raw"] / merged["close_adj"]
    bad = ~np.isfinite(factor.to_numpy()) | (merged["close_adj"].to_numpy() == 0)
    if bad.any():
        factor = factor.mask(bad)
    factor.name = "adj_factor"
    return factor


def factor_for(factors: pd.Series | None, symbol: str, session: date) -> float:
    """Factor for one session. ``None`` factors mean the tape was not configured.

    An unconfigured tape returns 1. A configured tape with no row for this
    symbol-session returns NaN. It does not borrow a neighboring session.
    """
    if factors is None:
        return 1.0
    key = (symbol, session)
    try:
        value = factors.loc[key]
    except KeyError:
        return float("nan")
    if isinstance(value, pd.Series):
        value = value.iloc[0]
    return float(value)


def _read_closes(path: str | Path) -> pd.Series:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(target)
    if target.suffix == ".csv":
        frame = pd.read_csv(target)
    else:
        frame = pd.read_parquet(target)
    if "date" not in frame.columns or "symbol" not in frame.columns or "close" not in frame.columns:
        raise ValueError(f"{target} needs date, symbol, and close columns")
    dates = pd.to_datetime(frame["date"]).dt.date
    out = pd.Series(frame["close"].to_numpy(dtype=np.float64), index=pd.MultiIndex.from_arrays(
        [frame["symbol"].astype(str).to_numpy(), dates.to_numpy()],
        names=["symbol", "session"],
    ))
    return out.groupby(level=[0, 1]).last()
