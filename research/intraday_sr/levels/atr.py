"""Wilder ATR(n), causal.

The series at index i uses only true ranges through i. The first ``n``
values are NaN except at i == n - 1, where ATR is the simple mean of the
first n true ranges (Wilder's seed). After that,
``ATR_i = (ATR_{i-1} * (n - 1) + TR_i) / n``.

A numba implementation is used when numba imports. The numpy loop is the
fallback and must match numba exactly.
"""

from __future__ import annotations

import pandas as pd


def wilder_atr(bars: pd.DataFrame, length: int = 14) -> pd.Series:
    """ATR aligned to ``bars.index``. NaN until the seed bar."""
    raise NotImplementedError


def wilder_atr_last(bars: pd.DataFrame, length: int = 14) -> float:
    """ATR at the last row. NaN if the frame is shorter than ``length``."""
    raise NotImplementedError
