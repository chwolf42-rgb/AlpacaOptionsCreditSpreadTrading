"""Research code must not read the live bot's settings directory.

The forbidden directory name is assembled from pieces. This file is the
scanner, so the package walk skips it. Snippet checks below prove the
scanner sees the shapes the CP0 review named.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import io
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyarrow.csv as pacsv
import pyarrow.dataset as pads
import pyarrow.parquet as pq
import yaml

from research.intraday_sr.io import ConfigPathRefused, read_bytes, repo_root
from research.intraday_sr.types import ET, BarSet, EngineCfg, SignalCfg

_PART = "con" + "fig"
_PACKAGE = Path(__file__).resolve().parents[1]


def _folded(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _folded(node.left)
        right = _folded(node.right)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                return None
        return "".join(parts)
    return None


def _mentions(text: str) -> bool:
    if text == _PART:
        return True
    parts = [part for part in text.replace("\\", "/").split("/") if part not in ("", ".")]
    return _PART in parts


def _blocked_import(module: str | None) -> bool:
    if not module:
        return False
    parts = module.split(".")
    return parts[:1] == ["alpaca_options_credit"] and _PART in parts


def scan_source(source: str, *, allow_bare_name: bool) -> list[str]:
    tree = ast.parse(source)
    hits: list[str] = []
    bare = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _blocked_import(alias.name):
                    hits.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and _blocked_import(node.module):
            hits.append(node.module or "")
        if not isinstance(node, (ast.Constant, ast.BinOp, ast.JoinedStr)):
            continue
        folded = _folded(node)
        if folded is None or not _mentions(folded):
            continue
        if allow_bare_name and folded == _PART:
            bare += 1
            continue
        hits.append(folded)
    if allow_bare_name and bare != 1:
        hits.append(f"bare-name-count:{bare}")
    return hits


def test_scanner_flags_the_review_shapes():
    samples = [
        "from alpaca_options_credit.config import load\n",
        "import alpaca_options_credit.config as cfg\n",
        "p = Path('pkg') / 'config' / 'universe.yaml'\n",
        "os.path.join('config', 'default.yaml')\n",
        "name = 'con' + 'fig'\n",
        "p = path.parents[3] / 'config'\n",
    ]
    for sample in samples:
        assert scan_source(sample, allow_bare_name=False), sample
    assert scan_source("x = 1\n", allow_bare_name=False) == []


def test_static_scan_has_no_settings_paths():
    hits: list[str] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        if path.name == "test_no_config.py":
            continue
        found = scan_source(path.read_text(encoding="utf-8"), allow_bare_name=path.name == "io.py")
        if found:
            hits.append(f"{path.relative_to(_PACKAGE)}: {found}")
    assert hits == []


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
    if _PART not in parts:
        return None
    return text


def test_chokepoint_refuses_the_settings_directory():
    target = repo_root() / _PART / "universe.yaml"
    try:
        read_bytes(target)
    except ConfigPathRefused:
        return
    raise AssertionError("settings path was readable")


def test_runtime_engine_does_not_touch_settings(monkeypatch, tmp_path):
    touched: list[str] = []
    real_open = builtins.open
    real_io_open = io.open
    real_path_open = Path.open
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes
    real_os_open = os.open
    real_read_table = pq.read_table
    real_dataset = pads.dataset
    real_csv = pacsv.read_csv

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

    def guard_read_text(self, *args, **kwargs):
        hit = _is_forbidden(self)
        if hit:
            touched.append(hit)
        return real_read_text(self, *args, **kwargs)

    def guard_read_bytes(self, *args, **kwargs):
        hit = _is_forbidden(self)
        if hit:
            touched.append(hit)
        return real_read_bytes(self, *args, **kwargs)

    def guard_os_open(path, flags, *args, **kwargs):
        hit = _is_forbidden(path)
        if hit:
            touched.append(hit)
        return real_os_open(path, flags, *args, **kwargs)

    def guard_source(fn):
        def wrapped(source, *args, **kwargs):
            hit = _is_forbidden(source)
            if hit:
                touched.append(hit)
            return fn(source, *args, **kwargs)

        return wrapped

    def guard_yaml(fn):
        def wrapped(stream, *args, **kwargs):
            hit = _is_forbidden(stream)
            if hit:
                touched.append(hit)
            return fn(stream, *args, **kwargs)

        return wrapped

    monkeypatch.setattr(builtins, "open", guard_open)
    monkeypatch.setattr(io, "open", guard_io_open)
    monkeypatch.setattr(Path, "open", guard_path_open)
    monkeypatch.setattr(Path, "read_text", guard_read_text)
    monkeypatch.setattr(Path, "read_bytes", guard_read_bytes)
    monkeypatch.setattr(os, "open", guard_os_open)
    monkeypatch.setattr(pq, "read_table", guard_source(real_read_table))
    monkeypatch.setattr(pads, "dataset", guard_source(real_dataset))
    monkeypatch.setattr(pacsv, "read_csv", guard_source(real_csv))
    for name in ("load", "safe_load", "load_all", "safe_load_all"):
        monkeypatch.setattr(yaml, name, guard_yaml(getattr(yaml, name)))

    modules = [
        "research.intraday_sr.io",
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
    grids = reloaded[2]
    cache = reloaded[9]
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
    sample = tmp_path / "SPY.parquet"
    pd.DataFrame({"symbol": ["SPY"], "close": [1.0]}).to_parquet(sample)
    loaded = cache.load_symbol(sample, "SPY")
    assert list(loaded["symbol"]) == ["SPY"]
    assert touched == []
