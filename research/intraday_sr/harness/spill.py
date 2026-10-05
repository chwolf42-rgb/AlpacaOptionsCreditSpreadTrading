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


# ---------------------------------------------------------------------------------------------------------------
# Compact columnar spill (pass-2 streaming). One ``<SYM>.npz`` per (variant, symbol) holding ONLY what the
# simulator (harness/portfolio.py) and the ledger path read from a Signal / Zone:
#   Signal: symbol, available_at, expires_at, direction, trigger, stop, targets{1R,2R,zone}, zone, variant_id
#   Zone:   symbol, low, high, score, atr_d, available_at (Guard check on nested reads), zone_id (error text only)
# Timestamps are stored as exact epoch microseconds and rebuilt as America/New_York datetimes; prices and scores
# are float64 copies, so every value the simulator reads is bit-identical to the full object's.
# ---------------------------------------------------------------------------------------------------------------
import heapq
from datetime import datetime, timedelta, timezone

import numpy as np

COMPACT_VERSION = 1
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_US = timedelta(microseconds=1)
_TKEYS = ("1R", "2R", "zone")


def _us(ts) -> int:
    """Exact epoch microseconds of a tz-aware datetime / pd.Timestamp (raises on naive or sub-us stamps)."""
    if ts.tzinfo is None:
        raise ValueError(f"naive timestamp cannot be spilled: {ts!r}")
    if hasattr(ts, "nanosecond"):                        # pd.Timestamp: exact integer nanoseconds
        v = int(ts.value)
        if v % 1000:
            raise ValueError(f"sub-microsecond timestamp cannot be spilled compactly: {ts!r}")
        return v // 1000
    return (ts - _EPOCH) // _US


class _Tab:
    """Per-(variant, symbol) columns kept for the rarely read fields: the targets map (zone-target entries) and
    zone_id (error text only). When the columns came from an .npz file, zone ids are re-read from it on demand."""
    __slots__ = ("targets", "tmask", "zone_idx", "_z_id", "_path")

    def __init__(self, cols, path=None):
        self.targets, self.tmask, self.zone_idx = cols["targets"], cols["tmask"], cols["zone_idx"]
        self._path = path
        self._z_id = None if path is not None else cols["z_id"]

    @property
    def z_id(self):
        if self._z_id is None:
            with np.load(self._path, allow_pickle=False) as z:
                self._z_id = z["z_id"]
        return self._z_id


class LiteZone:
    """View of a LiteSignal's zone: the Zone fields the simulator reads. Has available_at, so Guard still checks
    every nested zone read exactly as for a real Zone."""
    __slots__ = ("_s",)

    def __init__(self, s):
        self._s = s

    symbol = property(lambda self: self._s.symbol)
    low = property(lambda self: self._s.z_low)
    high = property(lambda self: self._s.z_high)
    score = property(lambda self: self._s.z_score)
    atr_d = property(lambda self: self._s.z_atr_d)
    available_at = property(lambda self: self._s.z_available_at)

    @property
    def zone_id(self) -> str:
        s = self._s
        return s._tab.z_id[int(s._tab.zone_idx[s._row])].decode()

    def __repr__(self) -> str:
        return f"LiteZone({self.symbol} {self.low}-{self.high} score={self.score} at={self.available_at})"


class LiteSignal:
    """The Signal fields the simulator and ledger path read (see the module note), as plain slots; the zone is a
    view over the z_* slots and ``targets`` / ``zone_id`` are rebuilt on access from the per-file columns with
    exactly the keys and values the original had."""
    __slots__ = ("symbol", "available_at", "expires_at", "direction", "trigger", "stop", "variant_id",
                 "z_low", "z_high", "z_score", "z_atr_d", "z_available_at", "_tab", "_row")

    @property
    def zone(self) -> LiteZone:
        return LiteZone(self)

    @property
    def targets(self) -> dict:
        tab, i = self._tab, self._row
        m = int(tab.tmask[i])
        return {k: float(tab.targets[i, q]) for q, k in enumerate(_TKEYS) if m >> q & 1}

    def __repr__(self) -> str:
        return (f"LiteSignal({self.variant_id} {self.symbol} {self.available_at} dir={self.direction} "
                f"trig={self.trigger} stop={self.stop})")


def to_columns(sigs: list) -> dict:
    """Signals for ONE (variant, symbol) -> dict of numpy columns (zones de-duplicated by identity)."""
    n = len(sigs)
    syms = {s.symbol for s in sigs}
    vids = {s.variant_id for s in sigs}
    if len(syms) > 1 or len(vids) > 1:
        raise ValueError(f"compact spill holds one (variant, symbol); got symbols {syms} variants {vids}")
    zrow: dict = {}
    zl, zh, zs, za, zav, zid, zsym = [], [], [], [], [], [], set()
    av = np.empty(n, np.int64)
    ex = np.empty(n, np.int64)
    dr = np.empty(n, np.int8)
    tr = np.empty(n, np.float64)
    sp = np.empty(n, np.float64)
    tg = np.full((n, 3), np.nan, np.float64)
    tm = np.zeros(n, np.uint8)
    zi = np.empty(n, np.int32)
    for i, s in enumerate(sigs):
        z = s.zone
        j = zrow.get(id(z))
        if j is None:
            j = zrow[id(z)] = len(zl)
            zl.append(float(z.low)); zh.append(float(z.high)); zs.append(float(z.score)); za.append(float(z.atr_d))
            zav.append(_us(z.available_at)); zid.append(str(z.zone_id)); zsym.add(z.symbol)
        av[i] = _us(s.available_at)
        ex[i] = _us(s.expires_at)
        d = int(s.direction)
        if d not in (1, -1):
            raise ValueError(f"direction {d!r}")
        dr[i] = d
        tr[i] = float(s.trigger)
        sp[i] = float(s.stop)
        t = s.targets
        for k in t:
            if k not in _TKEYS:
                raise ValueError(f"unknown target key {k!r}")
            q = _TKEYS.index(k)
            v = t[k]
            if v is None:
                raise ValueError("None target value cannot be spilled compactly")
            tg[i, q] = float(v)
            tm[i] |= 1 << q
        zi[i] = j
    if zsym - syms and syms:
        raise ValueError(f"zone symbol {zsym} differs from signal symbol {syms}")
    zid_arr = np.array(zid, dtype="S64") if zid else np.empty(0, "S64")
    if any(len(x) > 64 for x in zid):
        raise ValueError("zone_id longer than 64 bytes")
    return {"version": np.int64(COMPACT_VERSION), "symbol": np.array(next(iter(syms), "")),
            "variant_id": np.array(next(iter(vids), "")), "avail_us": av, "expires_us": ex, "direction": dr,
            "trigger": tr, "stop": sp, "targets": tg, "tmask": tm, "zone_idx": zi,
            "z_low": np.array(zl, np.float64), "z_high": np.array(zh, np.float64),
            "z_score": np.array(zs, np.float64), "z_atr_d": np.array(za, np.float64),
            "z_avail_us": np.array(zav, np.int64), "z_id": zid_arr}


def write_compact(spill: Path, vid: str, sym: str, sigs: list) -> None:
    d = Path(spill) / vid
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{sym}.{os.getpid()}.tmp.npz"
    np.savez(tmp, **to_columns(sigs))
    os.replace(tmp, d / f"{sym}.npz")


def _dt(us: int, cache: dict) -> datetime:
    v = cache.get(us)
    if v is None:
        from research.intraday_sr.types import ET
        v = cache[us] = datetime.fromtimestamp(us // 1_000_000, ET).replace(microsecond=us % 1_000_000)
    return v


def from_columns(cols, cache: dict | None = None, symbol: str | None = None, path=None) -> list:
    """Columns -> LiteSignal list for one (variant, symbol), stable-sorted by available_at (ties keep the engine's
    emit order). ``symbol`` overrides the stored name (scale tests only). Timestamps are shared via ``cache``."""
    cache = {} if cache is None else cache
    if int(cols["version"]) != COMPACT_VERSION:
        raise ValueError(f"compact spill version {int(cols['version'])} != {COMPACT_VERSION}")
    sym = symbol or str(cols["symbol"])
    vid = str(cols["variant_id"])
    tab = _Tab(cols, path)
    zi = cols["zone_idx"]
    zlo, zhi, zsc = cols["z_low"][zi].tolist(), cols["z_high"][zi].tolist(), cols["z_score"][zi].tolist()
    zat, zav = cols["z_atr_d"][zi].tolist(), cols["z_avail_us"][zi].tolist()
    av = cols["avail_us"]
    order = np.argsort(av, kind="stable")
    avl, exl, drl = av.tolist(), cols["expires_us"].tolist(), cols["direction"].tolist()
    trl, spl = cols["trigger"].tolist(), cols["stop"].tolist()
    out = []
    for i in order.tolist():
        s = LiteSignal()
        s.symbol, s.variant_id = sym, vid
        s.available_at, s.expires_at = _dt(avl[i], cache), _dt(exl[i], cache)
        s.direction, s.trigger, s.stop = drl[i], trl[i], spl[i]
        s.z_low, s.z_high, s.z_score, s.z_atr_d = zlo[i], zhi[i], zsc[i], zat[i]
        s.z_available_at = _dt(zav[i], cache)
        s._tab, s._row = tab, i
        out.append(s)
    return out


def merge_streams(streams: list) -> list:
    """THE pass-2 merge order (deterministic, documented, tested):
        1. available_at   2. symbol (string order)   3. the signal's order within its symbol's stream
    Each stream is one symbol's signals already stable-sorted by available_at (``from_columns``); heapq.merge is
    stable across equal keys, and two streams never share a symbol, so ties fall back to within-symbol order.
    This equals the order `simulate` arrives at from the legacy list (symbols concatenated, then a stable sort by
    (available_at, symbol) per session), so simulation output is identical."""
    return list(heapq.merge(*streams, key=lambda s: (s.available_at, s.symbol)))


def read_compact(spill: Path, vid: str, symbols: list, rename: dict | None = None) -> list:
    """Pass-2 loader: LiteSignals for one variant across `symbols`, in `merge_streams` order. Reads ``<SYM>.npz``
    when present, else converts the legacy ``<SYM>.pkl`` one symbol at a time (full objects never accumulate).
    ``rename`` maps a symbol to the file it is read from (scale tests: 33 names over one real spill)."""
    cache: dict = {}
    streams = []
    for sym in symbols:
        src = (rename or {}).get(sym, sym)
        p = Path(spill) / vid / f"{src}.npz"
        if p.is_file():
            with np.load(p, allow_pickle=False) as z:
                cols = {k: z[k] for k in z.files if k != "z_id"}
            cols["z_id"] = None
        else:
            p = None
            with open(Path(spill) / vid / f"{src}.pkl", "rb") as fh:
                cols = to_columns(pickle.load(fh))
        streams.append(from_columns(cols, cache, symbol=sym if src != sym else None, path=p))
    return merge_streams(streams)
