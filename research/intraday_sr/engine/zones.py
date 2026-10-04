"""Zones. S0 stub. D2-3 owns scoring; the signature is the §3 entry point."""

from __future__ import annotations

from datetime import datetime

from research.intraday_sr.types import BarSet, EngineCfg, Zone


def zones_at(bars: BarSet, as_of: datetime, cfg: EngineCfg) -> list[Zone]:
    """Zones recomputed at ``as_of`` from bars already closed. Stub returns none."""
    _ = bars.visible(as_of)
    _ = cfg
    return []
