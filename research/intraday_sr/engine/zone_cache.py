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


def prefix_digest(hashes: np.ndarray, index: int) -> bytes:
    """128-bit digest of the prefix ending at ``index``."""
    return hashes[index].tobytes()


def prefix_hashes(frame: pd.DataFrame) -> np.ndarray:
    """Running blake2b-128 of each prefix, one 16-byte digest per bar.

    Every byte of symbol, open, high, low, close, volume, and available_at
    enters the chain. vwap, adj_factor, and session do too when they are
    present, because they move VWAP levels, round numbers, and the prior-day
    pair. A bad-print flag and its visible_at enter as the clamp state of
    that bar. Folding those columns into one integer first is not used: a
    swap of open and close would survive that fold.

    A shortened tape and ``visible(as_of)`` share a digest when they share
    the same leading bars, which is what the prefix cache matches on.
    """
    n = len(frame)
    out = np.empty((n, 16), dtype=np.uint8)
    if n == 0:
        return out
    pieces: list[np.ndarray] = []
    if "symbol" in frame.columns:
        symbols = frame["symbol"].astype(str).to_numpy()
    else:
        symbols = np.array([""] * n, dtype=object)
    sym_bytes = [value.encode("utf-8") for value in symbols]
    same_symbol = all(raw == sym_bytes[0] for raw in sym_bytes)
    for name in ("open", "high", "low", "close", "volume", "vwap", "adj_factor"):
        if name not in frame.columns:
            continue
        values = np.ascontiguousarray(frame[name].to_numpy(dtype=np.float64))
        pieces.append(values.view(np.uint8).reshape(n, 8))
    if "available_at" in frame.columns:
        avail = np.ascontiguousarray(_epoch_ns_array(frame["available_at"]))
        pieces.append(avail.view(np.uint8).reshape(n, 8))
    if "session" in frame.columns:
        session = frame["session"]
        if np.issubdtype(session.dtype, np.integer):
            sess = np.ascontiguousarray(session.to_numpy(dtype=np.int64))
        else:
            parsed = pd.DatetimeIndex(pd.to_datetime(session.to_numpy()))
            sess = (
                parsed.year.astype(np.int64) * 10000
                + parsed.month.astype(np.int64) * 100
                + parsed.day.astype(np.int64)
            ).to_numpy()
        pieces.append(np.ascontiguousarray(sess).view(np.uint8).reshape(n, 8))
    if "bad_print" in frame.columns:
        flag = np.ascontiguousarray(frame["bad_print"].to_numpy(dtype=np.uint8)).reshape(n, 1)
        pieces.append(flag)
        if "bad_print_visible_at" in frame.columns:
            vis = np.ascontiguousarray(_epoch_ns_array(frame["bad_print_visible_at"]))
        else:
            vis = np.zeros(n, dtype=np.int64)
        pieces.append(np.ascontiguousarray(vis.view(np.uint8).reshape(n, 8)))
    block = np.ascontiguousarray(np.concatenate(pieces, axis=1) if pieces else np.zeros((n, 0), dtype=np.uint8))
    width = int(block.shape[1])
    hasher = hashlib.blake2b(digest_size=16)
    flat = memoryview(block.reshape(-1))
    if same_symbol:
        raw = sym_bytes[0]
        prefix = len(raw).to_bytes(4, "little") + raw
        for index in range(n):
            hasher.update(prefix)
            if width:
                hasher.update(flat[index * width : (index + 1) * width])
            out[index] = np.frombuffer(hasher.digest(), dtype=np.uint8)
        return out
    for index in range(n):
        raw = sym_bytes[index]
        hasher.update(len(raw).to_bytes(4, "little"))
        hasher.update(raw)
        if width:
            hasher.update(flat[index * width : (index + 1) * width])
        out[index] = np.frombuffer(hasher.digest(), dtype=np.uint8)
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
