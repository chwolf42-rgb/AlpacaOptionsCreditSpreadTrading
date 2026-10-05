"""Pass-1 signal spill for per-symbol chunking: one pickle per (variant, symbol) under <out>/<tag>/_signals.

S0 Signal/Zone store components/targets in a process-local float blob (``_MapView``), and their
dataclass fields for those maps are InitVars — default pickle therefore drops the map contents
and leaves the slots unset on load. The reducers below rebuild via the public constructors with
plain dict copies, so a spill file also loads outside the run (harness.run is __main__ under
``python -m``). MappingProxyType is still registered for older spill files / stubs.
"""
from __future__ import annotations

import copyreg
import os
import pickle
import types
from pathlib import Path


def mapping_proxy(d: dict) -> types.MappingProxyType:
    return types.MappingProxyType(d)


copyreg.pickle(types.MappingProxyType, lambda m: (mapping_proxy, (dict(m),)))


def _rebuild_zone(kwargs: dict):
    from research.intraday_sr.types import Zone
    return Zone(**kwargs)


def _rebuild_signal(kwargs: dict):
    from research.intraday_sr.types import Signal
    return Signal(**kwargs)


def _reduce_zone(z):
    return (
        _rebuild_zone,
        ({
            "symbol": z.symbol,
            "low": z.low,
            "high": z.high,
            "side": z.side,
            "score": z.score,
            "components": dict(z.components),
            "kinds": tuple(z.kinds),
            "as_of_ts": z.as_of_ts,
            "valid_from_ts": z.valid_from_ts,
            "available_at": z.available_at,
            "engine_cfg": z.engine_cfg,
            "tf": z.tf,
            "atr_d": z.atr_d,
            "zone_id": z.zone_id,
        },),
    )


def _reduce_signal(s):
    return (
        _rebuild_signal,
        ({
            "symbol": s.symbol,
            "tf": s.tf,
            "direction": s.direction,
            "test": s.test,
            "zone": s.zone,
            "formation": s.formation,
            "trigger": s.trigger,
            "stop": s.stop,
            "targets": dict(s.targets),
            "expires_at": s.expires_at,
            "components": dict(s.components),
            "as_of_ts": s.as_of_ts,
            "available_at": s.available_at,
            "variant_id": s.variant_id,
            "confluence": s.confluence,
        },),
    )


def _register_signal_zone_reducers() -> None:
    """Bind reducers lazily so importing spill does not pull the engine on every harness start."""
    from research.intraday_sr.types import Signal, Zone
    copyreg.pickle(Zone, _reduce_zone)
    copyreg.pickle(Signal, _reduce_signal)


_register_signal_zone_reducers()


def write(spill: Path, vid: str, sym: str, sigs: list) -> None:
    d = Path(spill) / vid
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{sym}.{os.getpid()}.tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(sigs, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, d / f"{sym}.pkl")


def read(spill: Path, vid: str, symbols: list) -> list:
    """Signals for one variant in `symbols` order: exactly the list the unchunked path builds."""
    out = []
    for sym in symbols:
        with open(Path(spill) / vid / f"{sym}.pkl", "rb") as fh:
            out.extend(pickle.load(fh))
    return out
