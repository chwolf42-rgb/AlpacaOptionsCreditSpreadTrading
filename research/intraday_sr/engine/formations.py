"""Formations. S0 stub. D2-4 owns detection."""

from __future__ import annotations

from datetime import datetime

from research.intraday_sr.types import BarSet, EngineCfg, Formation


def formations_at(bars: BarSet, as_of: datetime, cfg: EngineCfg) -> list[Formation]:
    """Formations knowable at ``as_of``. Stub returns none."""
    _ = bars.visible(as_of)
    _ = cfg
    return []
