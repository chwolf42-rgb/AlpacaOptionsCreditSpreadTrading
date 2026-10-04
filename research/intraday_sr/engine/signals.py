"""Causal signal gate.

Emits one long when ``k_confirm`` is 0 and a support zone is already
published. The full stack (hold, oscillator, MACD, RVOL, arm) is D2-3.
``confluence`` stays 0 here, matching the three optional flags.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterator

from research.intraday_sr.engine.zones import zones_at
from research.intraday_sr.types import BarSet, EngineCfg, Signal, SignalCfg


def signals(
    bars: BarSet,
    start: datetime,
    end: datetime,
    cfg: EngineCfg,
    sig: SignalCfg,
) -> Iterator[Signal]:
    """Signals whose ``available_at`` is inside ``[start, end]``."""
    if sig.k_confirm != 0:
        return iter(())
    published = [
        zone
        for zone in zones_at(bars, end, cfg)
        if zone.side == "support" and start <= zone.available_at <= end
    ]
    if not published:
        return iter(())
    zone = published[0]
    risk = max(float(cfg.stop_floor_atr), float(cfg.stop_buffer_atr)) * float(zone.atr_d)
    entry = float(zone.high) + float(cfg.entry_offset)
    stop = entry - risk
    one_r = entry - stop
    yield Signal(
        symbol=zone.symbol,
        tf=sig.entry_tf,
        direction=1,
        test=sig.test,
        zone=zone,
        formation=None,
        trigger=entry,
        stop=stop,
        targets={"1R": entry + one_r, "2R": entry + 2.0 * one_r, "zone": entry + one_r},
        expires_at=zone.available_at + timedelta(minutes=5 * int(cfg.cancel_bars)),
        components={"oscillator": 0.0, "macd": 0.0, "rvol": 0.0},
        as_of_ts=zone.available_at,
        available_at=zone.available_at,
        variant_id=sig.variant_id,
        confluence=0,
    )
