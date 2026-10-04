"""Session adjustment factors.

``Bar.adj_factor`` is raw/adjusted. Trading's parquet stores adj/raw, so
``load_adj_factors`` inverts it. A missing session stays missing. The study
path does not substitute 1.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from research.intraday_sr.io import read_bytes


def load_factors(raw_path: str | Path, adj_path: str | Path) -> pd.Series:
    """Series indexed by ``(symbol, session)`` of ``raw_close / adj_close``."""
    raw = _read_closes(raw_path).rename("close_raw")
    adj = _read_closes(adj_path).rename("close_adj")
    merged = pd.concat([raw, adj], axis=1, join="inner")
    factor = merged["close_raw"] / merged["close_adj"]
    bad = ~np.isfinite(factor.to_numpy()) | (merged["close_adj"].to_numpy() == 0)
    factor = factor.mask(bad)
    factor.name = "adj_factor"
    return factor


def load_adj_factors(path: str | Path) -> pd.Series:
    """Invert Trading's ``adj_factor`` column (adj/raw) into raw/adjusted.

    The file has ``symbol``, ``date``, and ``adj_factor``.
    """
    frame = _read_table(path)
    required = {"symbol", "date", "adj_factor"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{path} is missing {sorted(missing)}")
    trading = frame["adj_factor"].to_numpy(dtype=np.float64)
    factor = np.where((trading == 0) | ~np.isfinite(trading), np.nan, 1.0 / trading)
    dates = pd.to_datetime(frame["date"]).dt.date
    out = pd.Series(
        factor,
        index=pd.MultiIndex.from_arrays(
            [frame["symbol"].astype(str).to_numpy(), dates.to_numpy()],
            names=["symbol", "session"],
        ),
        name="adj_factor",
    )
    return out.groupby(level=[0, 1]).last()


def factor_for(factors: pd.Series, symbol: str, session: date) -> float:
    """Factor for one session. A missing row is NaN, not 1."""
    if factors is None:
        raise ValueError("adjustment factors are required")
    key = (symbol, session)
    try:
        value = factors.loc[key]
    except KeyError:
        return float("nan")
    if isinstance(value, pd.Series):
        value = value.iloc[0]
    return float(value)


def as_traded(adjusted: float, adj_factor: float) -> float:
    """``adjusted * raw/adjusted``."""
    return float(adjusted) * float(adj_factor)


def _read_table(path: str | Path) -> pd.DataFrame:
    import io

    import pyarrow.parquet as pq

    target = Path(path)
    payload = read_bytes(target)
    if target.suffix == ".csv":
        return pd.read_csv(io.BytesIO(payload))
    return pq.read_table(io.BytesIO(payload)).to_pandas()


def _read_closes(path: str | Path) -> pd.Series:
    frame = _read_table(path)
    if "date" not in frame.columns or "symbol" not in frame.columns or "close" not in frame.columns:
        raise ValueError(f"{path} needs date, symbol, and close columns")
    dates = pd.to_datetime(frame["date"]).dt.date
    out = pd.Series(
        frame["close"].to_numpy(dtype=np.float64),
        index=pd.MultiIndex.from_arrays(
            [frame["symbol"].astype(str).to_numpy(), dates.to_numpy()],
            names=["symbol", "session"],
        ),
    )
    return out.groupby(level=[0, 1]).last()
