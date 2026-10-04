"""Level candidates. S0 stub: D2-2 replaces the body.

The stub still filters through ``BarSet.visible`` so a future bar is not
readable from this function. It returns no levels.
"""

from __future__ import annotations

from datetime import datetime

from research.intraday_sr.types import BarSet, EngineCfg, Level


def levels_at(bars: BarSet, as_of: datetime, cfg: EngineCfg) -> list[Level]:
    """Candidates with ``available_at <= as_of``. Stub returns an empty list."""
    _ = bars.visible(as_of)
    _ = cfg
    return []
