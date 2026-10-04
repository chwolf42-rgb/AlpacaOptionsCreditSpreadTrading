"""Pluggable point-in-time daily bars.

Production daily levels come from a panel on another machine. The default
source rebuilds daily OHLC from the 5m RTH bars already in hand. Both
paths must refuse sessions that had not closed by ``asof``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Protocol

import pandas as pd


class DailyBarSource(Protocol):
    """Completed RTH sessions whose close is at or before ``asof``.

    The frame is indexed by tz-aware session close (16:00 America/New_York)
    and has columns ``open, high, low, close, volume``.
    """

    def completed_sessions(self, symbol: str, asof: pd.Timestamp) -> pd.DataFrame:
        """Daily bars knowable at ``asof``. Empty if none qualify."""


class ResampledDailySource:
    """Default source: daily OHLC derived from intraday RTH bars."""

    def __init__(self, bars_by_symbol: Mapping[str, pd.DataFrame]) -> None:
        self._bars_by_symbol = bars_by_symbol

    def completed_sessions(self, symbol: str, asof: pd.Timestamp) -> pd.DataFrame:
        raise NotImplementedError


class NpzDailyBarSource:
    """Simple float32 panel. This is not the #40 research npz layout.

    Expected arrays in the ``.npz``:

    - ``dates``: datetime64[D] session dates, shape ``(n_dates,)``
    - ``symbols``: unicode symbol codes, shape ``(n_symbols,)``
    - ``open``, ``high``, ``low``, ``close``, ``volume``: float32,
      shape ``(n_dates, n_symbols)``

    Each session is stamped 16:00 America/New_York and is returned only
    when that timestamp is at or before ``asof``. The #40 point-in-time
    panel needs a separate adapter that still implements ``DailyBarSource``.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def completed_sessions(self, symbol: str, asof: pd.Timestamp) -> pd.DataFrame:
        raise NotImplementedError
