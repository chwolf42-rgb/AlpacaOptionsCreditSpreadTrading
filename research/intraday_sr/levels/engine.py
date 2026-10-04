"""Incremental S/R engine.

``update`` is the cheap path: one closed bar, no reread of the prefix.
``levels_asof`` returns the published zones for that symbol at time t
and does not look at bars that close after t. Callers that still have
the engine positioned past t must use a study built with ``history=True``
or a second engine fed only bars through t. ``levels_asof`` on an engine
that has not been shown a bar after t is the supported streaming call.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from research.intraday_sr.levels.config import LevelConfig
from research.intraday_sr.levels.daily import DailyBarSource
from research.intraday_sr.levels.types import (
    Formation,
    FormationEvent,
    LevelCandidate,
    LevelStudy,
    NearestZones,
    Zone,
)


class LevelEngine:
    """Per-symbol incremental state.

    One engine can hold many symbols. Memory stays bounded by processing
    one symbol to completion when the caller uses ``run_symbol`` rather
    than interleaving a long universe in one process.
    """

    def __init__(
        self,
        config: LevelConfig | None = None,
        daily_source: DailyBarSource | None = None,
        *,
        history: bool = False,
    ) -> None:
        """``history=True`` retains every published version for tests.

        The default keeps only the latest published set, which is what
        ``levels_asof`` needs during a forward pass.
        """
        self.config = config if config is not None else LevelConfig()
        self.daily_source = daily_source
        self.history = history

    def update(self, symbol: str, bar: pd.Series) -> None:
        """Ingest one closed bar.

        ``bar.name`` is the tz-aware bar close. Required columns are
        ``open, high, low, close, volume``. Optional: ``vwap``,
        ``trade_count``, ``session``.
        """
        raise NotImplementedError

    def update_settlement(self, symbol: str, bar: pd.Series) -> None:
        """Ingest the 16:00 closing auction, if the cache has one.

        This is not a 5-minute bar and does not update pivots, the opening
        range, session VWAP, or the volume profile. It can set the daily
        close and the daily high/low.
        """
        raise NotImplementedError

    def update_frame(
        self,
        symbol: str,
        bars: pd.DataFrame,
        *,
        settlement: pd.DataFrame | None = None,
    ) -> None:
        """Replay ``bars`` in index order, then any settlement prints.

        Settlement defaults to ``bars.attrs['settlement']`` when present.
        """
        raise NotImplementedError

    def levels_asof(self, symbol: str, t: datetime) -> list[Zone]:
        """Published zones for ``symbol`` knowable at ``t``, best score first.

        Requires that the engine has not ingested a bar that closes after
        ``t``. The batch helper rebuilds from a prefix when the caller
        needs an arbitrary past timestamp after a full replay.
        """
        raise NotImplementedError

    def nearest_zones(self, symbol: str, t: datetime, price: float) -> NearestZones:
        """Nearest published zone strictly below and above ``price`` at ``t``."""
        raise NotImplementedError

    def candidates_asof(self, symbol: str, t: datetime) -> list[LevelCandidate]:
        """Candidates with ``known_at <= t``. Same cursor rule as ``levels_asof``."""
        raise NotImplementedError

    def formations_asof(self, symbol: str, t: datetime) -> list[Formation]:
        """Formations with ``known_at <= t``."""
        raise NotImplementedError

    def events_asof(self, symbol: str, t: datetime) -> list[FormationEvent]:
        """Formation events with ``known_at <= t``."""
        raise NotImplementedError

    def study(self, symbol: str) -> LevelStudy:
        """Full emit log. Zone versions require ``history=True``."""
        raise NotImplementedError


def levels_asof(symbol: str, t: datetime, engine: LevelEngine) -> list[Zone]:
    """Module-level wrapper around ``LevelEngine.levels_asof``."""
    return engine.levels_asof(symbol, t)


def nearest_zones(
    symbol: str,
    t: datetime,
    price: float,
    engine: LevelEngine,
) -> NearestZones:
    """Module-level wrapper around ``LevelEngine.nearest_zones``."""
    return engine.nearest_zones(symbol, t, price)


def run_symbol(
    bars: pd.DataFrame,
    symbol: str,
    config: LevelConfig | None = None,
    *,
    daily_source: DailyBarSource | None = None,
    settlement: pd.DataFrame | None = None,
    history: bool = False,
) -> LevelStudy:
    """Batch helper: replay one symbol and return everything it emitted."""
    raise NotImplementedError


def run_universe(
    bars_by_symbol: dict[str, pd.DataFrame],
    config: LevelConfig | None = None,
    *,
    history: bool = False,
) -> dict[str, LevelStudy]:
    """Replay each symbol on its own engine, in symbol order, then drop it.

    This is the memory-safe path for a 30-name batch. Studies are retained;
    bar frames are not copied into a long-lived engine.
    """
    raise NotImplementedError
