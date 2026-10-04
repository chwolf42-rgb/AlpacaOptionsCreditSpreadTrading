"""S0 cache reader.

The study cache, symbol validation, and the holdout lock land in D2-1.
``load_symbol`` is the only reader, and it goes through the path chokepoint.
"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from research.intraday_sr.io import read_bytes


def load_symbol(path: str | Path, symbol: str | None = None) -> pd.DataFrame:
    """Read one parquet file. Refuses the live settings directory."""
    table = pq.read_table(io.BytesIO(read_bytes(path)))
    frame = table.to_pandas()
    if symbol is not None and "symbol" in frame.columns:
        frame = frame.loc[frame["symbol"] == symbol].reset_index(drop=True)
    return frame


def load_bars_file(*_args, **_kwargs):
    raise NotImplementedError("D2-1")


def normalize_bars(*_args, **_kwargs):
    raise NotImplementedError("D2-1")


def validate_symbol(*_args, **_kwargs):
    raise NotImplementedError("D2-1")


def load_universe_bars(*_args, **_kwargs):
    raise NotImplementedError("D2-1")
