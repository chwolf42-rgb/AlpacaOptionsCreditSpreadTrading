"""Signal stack. S0 stub. D2-3 owns the stack."""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

from research.intraday_sr.types import BarSet, EngineCfg, Signal, SignalCfg


def signals(
    bars: BarSet,
    start: datetime,
    end: datetime,
    cfg: EngineCfg,
    sig: SignalCfg,
) -> Iterator[Signal]:
    """Signals whose ``available_at`` is inside ``[start, end]`` and ``<= end``.

    Stub yields nothing. Only bars closed by ``end`` are visible.
    """
    _ = bars.visible(end)
    _ = (start, cfg, sig)
    return iter(())
