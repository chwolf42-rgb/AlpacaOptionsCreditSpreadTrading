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

import ctypes
import gc
import hashlib
import json
from collections import OrderedDict

import numpy as np
import pandas as pd

from research.intraday_sr.types import EngineCfg, Level, Zone, detach_live_maps, release_frozen_maps

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

# One full-span symbol is about 40k level stamps and twice that many zone
# stamps (5m and 15m). The budgets keep that working set and evict older
# symbols once a second tape arrives. ``clear_zone_cache`` drops both.
_LEVEL_BYTES = 320_000_000
_ZONE_BYTES = 128_000_000
_SIGNAL_BYTES = 32_000_000


class _Cache:
    """LRU store with a byte budget and an item cap.

    A single entry larger than the budget is kept. Eviction starts once a
    second entry pushes the store over the limit, so the live symbol is not
    dropped to satisfy the cap.
    """

    def __init__(self, max_bytes: int, max_items: int) -> None:
        self.max_bytes = int(max_bytes)
        self.max_items = int(max_items)
        self.data: OrderedDict = OrderedDict()
        self.sizes: dict = {}
        self.nbytes = 0

    def get(self, key):
        found = self.data.get(key)
        if found is None:
            return None
        self.data.move_to_end(key)
        return found

    def put(self, key, value, nbytes: int) -> None:
        if key in self.data:
            self.nbytes -= self.sizes[key]
        self.data[key] = value
        size = int(nbytes)
        self.sizes[key] = size
        self.nbytes += size
        self.data.move_to_end(key)
        self._evict()

    def _evict(self) -> None:
        while len(self.data) > 1 and (self.nbytes > self.max_bytes or len(self.data) > self.max_items):
            old, _value = self.data.popitem(last=False)
            self.nbytes -= self.sizes.pop(old, 0)

    def clear(self) -> None:
        self.data.clear()
        self.sizes.clear()
        self.nbytes = 0

    def __len__(self) -> int:
        return len(self.data)


_ZONES = _Cache(_ZONE_BYTES, 200_000)
_LEVELS = _Cache(_LEVEL_BYTES, 200_000)
_SIGNALS = _Cache(_SIGNAL_BYTES, 64)


# Bumped when zone geometry or stable-identity setup changes.
_ZONE_PIPELINE = "v1.3.3-a-c"


def zone_cfg_token(cfg: EngineCfg) -> str:
    """Hash of every EngineCfg field that can change zones or levels.

    The pipeline tag, ``max_zone_width_atr``, and ``min_clearance_atr`` are
    part of the key so a snapshot from before the v1.3.3 clearance trim
    cannot be served.
    """
    base = _token(cfg, skip=_NOT_ZONE_SIDE)
    width = float(cfg.max_zone_width_atr)
    clearance = float(cfg.min_clearance_atr)
    raw = f"{base}|max_zone_width_atr={width:.6f}|min_clearance_atr={clearance:.6f}|{_ZONE_PIPELINE}"
    return hashlib.sha256(raw.encode()).hexdigest()


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
        return _as_levels(found)
    built = build()
    stored, nbytes, levels = _pack_levels(built)
    _LEVELS.put(key, stored, nbytes)
    return levels


def cached_zones(key: tuple, build):
    found = _ZONES.get(key)
    if found is not None:
        return _as_zones(found)
    zones = list(build())
    if not zones:
        _ZONES.put(key, (), 64)
        return zones
    packed = _PackedZones(zones)
    _ZONES.put(key, packed, packed.nbytes)
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


def cached_signals(key: tuple) -> list | None:
    """Return a stored signal list, or None on a miss."""
    found = _SIGNALS.get(key)
    if found is None:
        return None
    return list(found)


# A Signal plus its Zone is several KB. 3072 left a ~9k full-span list
# under the 32MB budget, and the next variant stacked its own list and
# zone pack past ~700MB. 4096 puts that list over the budget so the next
# miss releases it. A few hundred signals still fit.
_SIGNAL_GRAPH_BYTES = 4096


def store_signals(key: tuple, signals: list) -> None:
    """Remember a signal list. Each signal keeps its zone, so the byte count is the object graph."""
    _SIGNALS.put(key, tuple(signals), 256 + _SIGNAL_GRAPH_BYTES * len(signals))


def release_oversized_signals() -> None:
    """Drop a retained signal list that is already over the byte budget.

    One oversized result stays cached so the same call is warm. A different
    full-span variant would otherwise build its own list while the previous
    one is still resident. Freed pages are returned so the next variant's
    zone packs do not stack on top of that dead heap.
    """
    if _SIGNALS.nbytes <= _SIGNALS.max_bytes:
        return
    _SIGNALS.clear()
    gc.collect()
    # A caller that kept the previous list still reads the right floats.
    detach_live_maps()
    release_frozen_maps()
    libc = ctypes.CDLL(None)
    trim = getattr(libc, "malloc_trim", None)
    if trim is not None:
        trim(0)


_EXTRA_CLEARS: list = []


def register_cache_clear(fn) -> None:
    """Let another module drop its own snapshots when the zone cache is cleared."""
    _EXTRA_CLEARS.append(fn)


def clear_zone_cache() -> None:
    """Drop every cached snapshot, including pivot and resample parents.

    The harness calls this between symbols. Nothing retained here is shared
    with the next symbol.
    """
    _ZONES.clear()
    _LEVELS.clear()
    _SIGNALS.clear()
    release_frozen_maps()
    for fn in _EXTRA_CLEARS:
        fn()


def _as_levels(found):
    materialize = getattr(found, "materialize", None)
    if materialize is not None:
        return materialize()
    return list(found)


def _as_zones(found):
    materialize = getattr(found, "materialize", None)
    if materialize is not None:
        return materialize()
    return list(found)


class _PackedZones:
    """Numeric rows for one stamp. Zone objects are built when a caller reads them."""

    __slots__ = (
        "symbol",
        "tf",
        "engine_cfg",
        "stamp",
        "sides",
        "kinds",
        "numbers",
        "keys",
        "rows",
        "nbytes",
    )

    def __init__(self, zones: list[Zone]) -> None:
        first = zones[0]
        keys = tuple(first.components)
        shared = all(tuple(zone.components) == keys for zone in zones) and all(
            zone.symbol == first.symbol
            and zone.tf == first.tf
            and zone.engine_cfg == first.engine_cfg
            and zone.as_of_ts == zone.available_at == zone.valid_from_ts == first.available_at
            for zone in zones
        )
        self.rows = None
        if shared:
            self.symbol = first.symbol
            self.tf = first.tf
            self.engine_cfg = first.engine_cfg
            self.stamp = first.available_at
            self.keys = keys
            self.sides = tuple(zone.side for zone in zones)
            self.kinds = tuple(tuple(zone.kinds) for zone in zones)
            numbers = np.empty((len(zones), 4 + len(keys)), dtype=np.float64)
            for index, zone in enumerate(zones):
                numbers[index, 0] = zone.low
                numbers[index, 1] = zone.high
                numbers[index, 2] = zone.score
                numbers[index, 3] = zone.atr_d
                for column, key in enumerate(keys):
                    numbers[index, 4 + column] = zone.components[key]
            self.numbers = numbers
            self.nbytes = 160 + int(numbers.nbytes) + 48 * len(zones)
            return
        self.symbol = None
        self.tf = None
        self.engine_cfg = None
        self.stamp = None
        self.sides = None
        self.kinds = None
        self.numbers = None
        self.keys = None
        self.rows = tuple(
            (
                zone.symbol,
                float(zone.low),
                float(zone.high),
                zone.side,
                float(zone.score),
                tuple(zone.components.items()),
                tuple(zone.kinds),
                zone.as_of_ts,
                zone.valid_from_ts,
                zone.available_at,
                zone.engine_cfg,
                zone.tf,
                float(zone.atr_d),
            )
            for zone in zones
        )
        self.nbytes = 64 + 160 * len(self.rows)

    def materialize(self) -> list[Zone]:
        if self.rows is not None:
            return [
                Zone(
                    symbol=symbol,
                    low=low,
                    high=high,
                    side=side,
                    score=score,
                    components=dict(components),
                    kinds=kinds,
                    as_of_ts=as_of_ts,
                    valid_from_ts=valid_from_ts,
                    available_at=available_at,
                    engine_cfg=engine_cfg,
                    tf=tf,
                    atr_d=atr_d,
                )
                for (
                    symbol,
                    low,
                    high,
                    side,
                    score,
                    components,
                    kinds,
                    as_of_ts,
                    valid_from_ts,
                    available_at,
                    engine_cfg,
                    tf,
                    atr_d,
                ) in self.rows
            ]
        numbers = self.numbers
        keys = self.keys
        stamp = self.stamp
        out: list[Zone] = []
        width = len(keys)
        for index, side in enumerate(self.sides):
            components = {keys[column]: float(numbers[index, 4 + column]) for column in range(width)}
            out.append(
                Zone(
                    symbol=self.symbol,
                    low=float(numbers[index, 0]),
                    high=float(numbers[index, 1]),
                    side=side,
                    score=float(numbers[index, 2]),
                    components=components,
                    kinds=self.kinds[index],
                    as_of_ts=stamp,
                    valid_from_ts=stamp,
                    available_at=stamp,
                    engine_cfg=self.engine_cfg,
                    tf=self.tf,
                    atr_d=float(numbers[index, 3]),
                )
            )
        return out


def _pack_levels(built):
    """Store a packed level record when the builder made one, else a tuple."""
    if getattr(built, "materialize", None) is not None and getattr(built, "nbytes", None) is not None:
        return built, int(built.nbytes), built.materialize()
    levels = tuple(built)
    return levels, 64 + 480 * len(levels), list(levels)


def _zone_bytes(zones: list) -> int:
    total = 64
    for zone in zones:
        total += 256 + 24 * len(zone.kinds) + 48 * len(zone.components)
    return total


def _token(cfg: EngineCfg, *, skip: frozenset[str]) -> str:
    payload = {
        name: getattr(cfg, name)
        for name in sorted(cfg.__dataclass_fields__)
        if name not in skip
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()

