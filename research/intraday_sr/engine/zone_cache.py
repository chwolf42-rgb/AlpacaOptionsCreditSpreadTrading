"""Shared level and zone snapshots for Test A variants.

The cache key for zones is ``(tape, symbol, entry_tf, 15m stamp, zone-cfg
hash)``. The hash covers every ``EngineCfg`` field that can change level
geometry or zone scoring, including ``k_zones``. Signal-only fields
(stops, indicators, guardrails, warmup) are left out, and ``SignalCfg``
axes other than the entry timeframe are not part of the key.

``tape`` is a content hash of the bars actually visible at the call.
Without it, two different histories for the same symbol and stamp would
reuse each other's zones. The cache is process-local and bounded.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict

import numpy as np
import pandas as pd

from research.intraday_sr.types import EngineCfg, Level, Zone

# Fields that do not change a level candidate or a zone's geometry or score.
# Anything not listed here is part of the zone-side hash, so a new EngineCfg
# field cannot silently reuse a stale snapshot.
_NOT_ZONE_SIDE = frozenset(
    {
        "warmup_date",
        "dev_start",
        "dev_end",
        "holdout_start",
        "holdout_end",
        "no_new_entries_after_et",
        "forced_exit_bar_open_et",
        "stop_buffer_atr",
        "stop_floor_atr",
        "arm_atr",
        "cancel_bars",
        "entry_offset",
        "risk_fraction",
        "notional_cap_position",
        "notional_cap_total",
        "model_equity",
        "max_entries_per_day",
        "max_concurrent",
        "max_per_symbol",
        "daily_loss_stop",
        "max_losses_day",
        "max_losses_week",
        "rvol_sessions",
        "rsi_length",
        "stoch_k",
        "stoch_d",
        "stoch_smooth",
        "macd_fast",
        "macd_slow",
        "macd_signal",
        "indicator_warmup_bars",
        "bootstrap_seed",
        "bootstrap_resamples",
        "formation_pivot_gap_min",
        "formation_pivot_gap_max",
        "shoulder_atr",
    }
)

# These change zone scores or the top-K cut, not the underlying level list.
_ZONE_ONLY = frozenset(
    {
        "k_zones",
        "k_cluster",
        "zone_pad_atr",
        "touch_sessions",
        "touch_window_bars",
        "recency_half_life_sessions",
        "score_touches",
        "score_rejections",
        "score_recency",
        "score_volume",
        "hvn_rejection_wick",
    }
)

_MAX_ENTRIES = 100_000
_ZONES: OrderedDict[tuple, tuple[Zone, ...]] = OrderedDict()
_LEVELS: OrderedDict[tuple, tuple[Level, ...]] = OrderedDict()


def zone_cfg_token(cfg: EngineCfg) -> str:
    """Hash of every EngineCfg field that can change zones or levels."""
    return _token(cfg, skip=_NOT_ZONE_SIDE)


def level_cfg_token(cfg: EngineCfg) -> str:
    """Hash of the fields that change level candidates. ``k_zones`` is not one of them."""
    return _token(cfg, skip=_NOT_ZONE_SIDE | _ZONE_ONLY)


def _epoch_ns_array(stamps: pd.Series) -> np.ndarray:
    """UTC nanoseconds without walking Python datetime objects."""
    values = stamps
    dtype = values.dtype
    if not (isinstance(dtype, pd.DatetimeTZDtype) or pd.api.types.is_datetime64_dtype(dtype)):
        values = pd.to_datetime(values)
        dtype = values.dtype
    unit = str(getattr(dtype, "unit", "ns"))
    scale = {"ns": 1, "us": 1_000, "ms": 1_000_000, "s": 1_000_000_000}[unit]
    return values.astype("int64").to_numpy(dtype=np.int64) * np.int64(scale)


def tape_token(frame: pd.DataFrame) -> str:
    """Content hash of one symbol's visible bars."""
    digest = hashlib.sha256()
    digest.update(str(len(frame)).encode())
    for column in ("open", "high", "low", "close", "volume"):
        if column not in frame.columns:
            continue
        values = np.ascontiguousarray(frame[column].to_numpy(dtype=np.float64))
        digest.update(values.tobytes())
    if "available_at" in frame.columns and len(frame):
        digest.update(np.ascontiguousarray(_epoch_ns_array(frame["available_at"])).tobytes())
    return digest.hexdigest()


def cached_levels(key: tuple, build):
    found = _LEVELS.get(key)
    if found is not None:
        _LEVELS.move_to_end(key)
        return list(found)
    levels = list(build())
    _remember(_LEVELS, key, tuple(levels))
    return levels


def cached_zones(key: tuple, build):
    found = _ZONES.get(key)
    if found is not None:
        _ZONES.move_to_end(key)
        return list(found)
    zones = list(build())
    _remember(_ZONES, key, tuple(zones))
    return zones


def prefix_hashes(frame: pd.DataFrame) -> np.ndarray:
    """Running FNV-1a of each prefix, so a later as-of can reuse an earlier stamp.

    The mix is the same columns as ``tape_token`` (open, high, low, close,
    volume, available_at). Two prefixes with the same bars share a hash, which
    is what a truncated tape and ``visible(as_of)`` are.
    """
    n = len(frame)
    out = np.empty(n, dtype=np.uint64)
    if n == 0:
        return out
    mixed = np.zeros(n, dtype=np.uint64)
    for name in ("open", "high", "low", "close", "volume"):
        if name not in frame.columns:
            continue
        values = np.ascontiguousarray(frame[name].to_numpy(dtype=np.float64))
        mixed ^= values.view(np.uint64)
    if "available_at" in frame.columns:
        mixed ^= np.ascontiguousarray(_epoch_ns_array(frame["available_at"])).view(np.uint64)
    state = 14695981039346656037
    prime = 1099511628211
    mask = (1 << 64) - 1
    for index in range(n):
        state = ((state ^ int(mixed[index])) * prime) & mask
        out[index] = state
    return out


_SIGNALS: OrderedDict[tuple, tuple] = OrderedDict()


def cached_signals(key: tuple) -> list | None:
    """Return a stored signal list, or None on a miss."""
    found = _SIGNALS.get(key)
    if found is None:
        return None
    _SIGNALS.move_to_end(key)
    return list(found)


def store_signals(key: tuple, signals: list) -> None:
    _remember(_SIGNALS, key, tuple(signals))


_EXTRA_CLEARS: list = []


def register_cache_clear(fn) -> None:
    """Let another module drop its own snapshots when the zone cache is cleared."""
    _EXTRA_CLEARS.append(fn)


def clear_zone_cache() -> None:
    """Drop every cached snapshot. Tests use this so cases do not share tapes."""
    _ZONES.clear()
    _LEVELS.clear()
    _SIGNALS.clear()
    for fn in _EXTRA_CLEARS:
        fn()


def _token(cfg: EngineCfg, *, skip: frozenset[str]) -> str:
    payload = {
        name: getattr(cfg, name)
        for name in sorted(cfg.__dataclass_fields__)
        if name not in skip
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _remember(store: OrderedDict, key: tuple, value: tuple) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > _MAX_ENTRIES:
        store.popitem(last=False)
