"""Load YAML config. Paths stay inside this repo (no shared equity/crypto dirs)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from alpaca_options_credit.errors import ConfigError

DEFAULT_CONFIG_NAME = "config/default.yaml"


def repo_root() -> Path:
    """Walk up from cwd / this file until config/default.yaml exists."""
    candidates = [Path.cwd(), Path(__file__).resolve().parents[2]]
    for start in candidates:
        for p in [start, *start.parents]:
            if (p / DEFAULT_CONFIG_NAME).is_file():
                return p
    return Path.cwd()


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"config must be a mapping: {path}")
    return data


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    root = repo_root()
    cfg_path = Path(path) if path else root / DEFAULT_CONFIG_NAME
    if not cfg_path.is_absolute():
        cand = Path.cwd() / cfg_path
        cfg_path = cand if cand.is_file() else root / cfg_path
    cfg = load_yaml(cfg_path)
    cfg["_config_path"] = str(cfg_path)
    cfg["_repo_root"] = str(root)
    _apply_universe(cfg, root)
    return cfg


def _apply_universe(cfg: dict[str, Any], root: Path) -> None:
    """Resolve universe.symbols from config/universe.yaml tiers (day1 | full_a)."""
    uni = cfg.setdefault("universe", {})
    uni_file = uni.get("file")
    if not uni_file:
        return
    path = Path(uni_file)
    if not path.is_absolute():
        path = root / path
    data = load_yaml(path)
    tiers = data.get("tiers") or {}
    active = str(uni.get("active") or data.get("active") or "day1")
    if active not in tiers:
        raise ConfigError(
            f"universe.active={active!r} is not a tier in {path} "
            f"(have {sorted(tiers)})"
        )
    uni["active"] = active
    uni["symbols"] = list(tiers[active])



def var_dir(cfg: dict[str, Any]) -> Path:
    root = Path(cfg.get("_repo_root") or repo_root())
    rel = cfg.get("bot", {}).get("var_dir", "var/options")
    path = Path(rel)
    if not path.is_absolute():
        path = root / path
    path.mkdir(parents=True, exist_ok=True)
    return path
