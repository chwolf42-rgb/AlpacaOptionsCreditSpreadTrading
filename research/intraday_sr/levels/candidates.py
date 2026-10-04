"""Causal level candidates.

Streaming code calls these helpers on bars that have already closed.
None of them may read a row past the bar being closed.
"""

from __future__ import annotations

import pandas as pd

from research.intraday_sr.levels.config import LevelConfig
from research.intraday_sr.levels.types import LevelCandidate


def confirmed_pivots(
    bars: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    left: int,
    right: int,
) -> list[LevelCandidate]:
    """Swing highs and lows.

    A high at index i is a pivot when its high is strictly greater than
    the ``left`` bars before it and the ``right`` bars after it. ``ref_time``
    is index i. ``known_at`` is index i + right, the close of the last
    confirming bar. The same rule, inverted, applies to lows.
    """
    raise NotImplementedError


def round_number_prices(price: float, steps: tuple[float, ...], band: float) -> list[float]:
    """Round grid levels within ``band`` of ``price``.

    ``steps`` is the configured 1 / 5 / 10 grid. The step actually used is
    the coarsest step that is still at most one tenth of ``price``, falling
    back to the finest configured step. See the interface doc.
    """
    raise NotImplementedError


def select_round_step(price: float, steps: tuple[float, ...]) -> float:
    """Price-scaled step from ``steps`` (default 1, 5, 10)."""
    raise NotImplementedError
