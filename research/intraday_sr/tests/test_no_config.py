"""Research code must not read the live bot configuration directory.

The forbidden path text is assembled from pieces so this file does not itself
contain that path. The static scan covers every Python file in the package,
including this one.
"""

from __future__ import annotations

import builtins
import importlib
import io
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from research.intraday_sr.types import ET, BarSet, EngineCfg, SignalCfg

_CONFIG_DIR = "con" + "fig/"
_UNIVERSE_YAML = _CONFIG_DIR + "universe.yaml"
_DEFAULT_YAML = _CONFIG_DIR + "default.yaml"
_PACKAGE = Path(__file__).resolve().parents[1]


def _is_forbidden(file) -> str | None:
    if isinstance(file, int):
        return None
    if isinstance(file, Path):
        text = file.as_posix()
    elif isinstance(file, str):
        text = file.replace("\\", "/")
    else:
        name = getattr(file, "name", None)
        if not isinstance(name, str):
            return None
        text = name.replace("\\", "/")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if "config" not in parts:
        return None
    index = parts.index("config")
    if index == len(parts) - 1 or index < len(parts) - 1:
        return text
    return None


def test_static_scan_has_no_config_paths():
    needles = (_CONFIG_DIR, _UNIVERSE_YAML, _DEFAULT_YAML)
    hits: list[str] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        found = [needle for needle in needles if needle in text]
        if found:
            hits.append(f"{path.relative_to(_PACKAGE)}: {found}")
    assert hits == []


def test_runtime_engine_does_not_touch_config(monkeypatch):
    touched: list[str] = []
    real_open = builtins.open
    real_io_open = io.open
    real_path_open = Path.open

    def guard_open(file, *args, **kwargs):
        hit = _is_forbidden(file)
        if hit:
            touched.append(hit)
        return real_open(file, *args, **kwargs)

    def guard_io_open(file, *args, **kwargs):
        hit = _is_forbidden(file)
        if hit:
            touched.append(hit)
        return real_io_open(file, *args, **kwargs)

    def guard_path_open(self, *args, **kwargs):
        hit = _is_forbidden(self)
        if hit:
            touched.append(hit)
        return real_path_open(self, *args, **kwargs)

    def guard_yaml(fn):
        def wrapped(stream, *args, **kwargs):
            hit = _is_forbidden(stream)
            if hit:
                touched.append(hit)
            name = getattr(stream, "name", None)
            named = _is_forbidden(name) if isinstance(name, str) else None
            if named:
                touched.append(named)
            return fn(stream, *args, **kwargs)

        return wrapped

    monkeypatch.setattr(builtins, "open", guard_open)
    monkeypatch.setattr(io, "open", guard_io_open)
    monkeypatch.setattr(Path, "open", guard_path_open)
    for name in ("load", "safe_load", "load_all", "safe_load_all"):
        monkeypatch.setattr(yaml, name, guard_yaml(getattr(yaml, name)))

    modules = [
        "research.intraday_sr.types",
        "research.intraday_sr.grids",
        "research.intraday_sr.data.calendar",
        "research.intraday_sr.data.resample",
        "research.intraday_sr.data.adjust",
        "research.intraday_sr.data.vix",
        "research.intraday_sr.data.ratelimit",
        "research.intraday_sr.data.pull",
        "research.intraday_sr.data.cache",
        "research.intraday_sr.engine.levels",
        "research.intraday_sr.engine.zones",
        "research.intraday_sr.engine.formations",
        "research.intraday_sr.engine.signals",
        "research.intraday_sr.engine",
    ]
    reloaded = [importlib.reload(importlib.import_module(name)) for name in modules]
    grids = reloaded[1]
    levels_at = reloaded[-5].levels_at
    zones_at = reloaded[-4].zones_at
    formations_at = reloaded[-3].formations_at
    signals = reloaded[-2].signals

    as_of = datetime(2024, 6, 3, 10, 0, tzinfo=ET)
    bars = BarSet(pd.DataFrame())
    cfg = EngineCfg()
    sig = SignalCfg(
        oscillator="rsi14_30_70",
        rvol_min=1.5,
        entry_tf="5m",
        target="1R",
        k_confirm=0,
        variant_id="runtime-guard",
        test="A",
    )
    levels_at(bars, as_of, cfg)
    zones_at(bars, as_of, cfg)
    formations_at(bars, as_of, cfg)
    assert list(signals(bars, as_of, as_of, cfg, sig)) == []
    assert grids.UNIVERSE[0] == "SPY"
    assert len(grids.universe_sha256()) == 64
    assert touched == []
