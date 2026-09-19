"""Load YAML config. Paths stay inside this repo (no shared equity/crypto dirs)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from alpaca_options_credit.errors import ConfigError, ExitPolicyError

DEFAULT_CONFIG_NAME = "config/default.yaml"

# Only allowed options-native exit path. Equity OCO / bracket / attached
# stop-limits are the source of held-leg, pending_cancel, and naked-window bugs.
OPTIONS_NATIVE_EXIT_PATH = "credit_mark_and_structure"
_FORBIDDEN_EQUITY_STOP_KEYS = frozenset(
    {
        "oco",
        "bracket",
        "equity_stop",
        "equity_bracket",
        "attached_stop",
        "stop_order",
        "stop_limit",
        "oco_legs",
        "bracket_legs",
        "use_oco",
        "use_bracket",
        "working_stop",
        "child_stops",
    }
)


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
    validate_exit_policy(cfg)
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


def validate_exit_policy(cfg: dict[str, Any]) -> dict[str, Any]:
    """Refuse equity OCO/bracket stops. Credit-mark + structure-break only.

    Called from load_config and Engine.__init__ so a mutated in-memory cfg
    cannot re-enable the equity stop path before paper fills.
    """
    exits = cfg.setdefault("exits", {})
    if not isinstance(exits, dict):
        raise ExitPolicyError("exits must be a mapping")

    forbidden = _FORBIDDEN_EQUITY_STOP_KEYS.intersection(exits)
    if forbidden:
        raise ExitPolicyError(
            "equity stop keys are forbidden on this options sleeve "
            f"(found {sorted(forbidden)}). Use credit-mark + structure-break only."
        )

    path = str(exits.get("path") or OPTIONS_NATIVE_EXIT_PATH)
    if path != OPTIONS_NATIVE_EXIT_PATH:
        raise ExitPolicyError(
            f"exits.path={path!r} is forbidden. Only "
            f"{OPTIONS_NATIVE_EXIT_PATH!r} is allowed (no equity OCO/bracket)."
        )
    exits["path"] = path

    if exits.get("forbid_equity_oco_bracket") is False:
        raise ExitPolicyError("forbid_equity_oco_bracket cannot be false")
    exits["forbid_equity_oco_bracket"] = True

    if exits.get("never_cancel_working_close") is False:
        raise ExitPolicyError(
            "never_cancel_working_close cannot be false "
            "(cancel-before-replace leaves a naked credit spread)"
        )
    exits["never_cancel_working_close"] = True

    if exits.get("atomic_spread_only") is False:
        raise ExitPolicyError(
            "atomic_spread_only cannot be false "
            "(legging out of a credit spread can leave a naked short)"
        )
    exits["atomic_spread_only"] = True

    broker = cfg.setdefault("broker", {})
    if isinstance(broker, dict):
        order_class = str(broker.get("order_class") or "mleg").lower()
        if order_class in {"oco", "bracket", "oto", "otooco"}:
            raise ExitPolicyError(
                f"broker.order_class={order_class!r} is an equity bracket path. "
                "This bot submits mleg credit-spread opens/closes only."
            )
        broker["order_class"] = "mleg"

    return cfg


def var_dir(cfg: dict[str, Any]) -> Path:
    root = Path(cfg.get("_repo_root") or repo_root())
    rel = cfg.get("bot", {}).get("var_dir", "var/options")
    path = Path(rel)
    if not path.is_absolute():
        path = root / path
    path.mkdir(parents=True, exist_ok=True)
    return path
