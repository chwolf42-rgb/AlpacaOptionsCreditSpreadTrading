"""Pass-1 signal spill for per-symbol chunking: one pickle per (variant, symbol) under <out>/<tag>/_signals.

S0 Signal/Zone freeze their mappings as MappingProxyType, which pickle refuses. The reducer below pickles a proxy as
a proxy over a plain dict copy (read-only on load, equal by content). It lives in this importable module, not in
harness.run, so a spill file also loads outside the run (harness.run is __main__ under `python -m`).
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
