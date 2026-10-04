"""Cluster candidates into scored zones.

Clustering width is ``cluster_atr * ATR(atr_length)`` on the zone
timeframe, computed as of the bar being closed. Single-linkage: walking
candidates sorted by price, a new cluster starts when the gap from the
previous candidate exceeds that width.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from research.intraday_sr.levels.config import LevelConfig
from research.intraday_sr.levels.types import LevelCandidate, Zone


def cluster_zones(
    candidates: list[LevelCandidate],
    *,
    atr: float,
    asof: datetime,
    config: LevelConfig,
    previous: list[Zone] | None = None,
    bars: pd.DataFrame | None = None,
) -> list[Zone]:
    """Published zones knowable at ``asof``.

    ``previous`` carries stable ids and flip state. ``bars`` are the zone
    timeframe bars with close <= ``asof`` and are the only tape used for
    touches, rejections, and volume at the level.
    """
    raise NotImplementedError
