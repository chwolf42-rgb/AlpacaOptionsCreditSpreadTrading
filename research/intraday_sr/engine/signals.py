"""Test A, Test B, and formation-only signal stacks.

The hold and the re-confirmation entry are mandatory for Test A.
``k_confirm`` is how many of the three optional conditions (oscillator,
MACD, RVOL) must also hold. Test B keeps conditions 1–5 with that same
``k_confirm`` rule and replaces step 6 with the F8 neckline retest.
``F_W``, ``F_IHS``, ``F_M``, and ``F_HS`` are the formation trigger alone.

spec_doc v1.3.5 (8504fd7c19136141a32746234b63dd084106369d).
engine_spec v1.3.5.

Stops are derived from the clamped zone and the pullback. Whether a later
bar trades through that stop is the harness's job, and it uses the
unclamped high and low.

SPEC v1.3.3 lock (c): within a session, a new zone inherits a previous
zone's setup when the two are on the same side and the overlap of their
padded ranges is at least half the narrower width. The inherited id keeps
the touch, the rejection close, the armed bar, and the emitted-order
block. Identity resets at the session boundary. ``Signal.zone`` stays the
snapshot at ``as_of``; the stable id is internal.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from typing import Iterator

import numpy as np
import pandas as pd

from research.intraday_sr.data.badprint import prices_as_of
from research.intraday_sr.engine.indicators import macd, rsi_wilder, rvol, stochastic
from research.intraday_sr.engine.levels import (
    _LevelTape,
    _atr_from_segments,
    _segments,
    timeframe_frame,
)
from research.intraday_sr.engine.tape import floor_15m, minute_of_day, session_day
from research.intraday_sr.engine.zone_cache import (
    cached_levels,
    cached_signals,
    cached_zones,
    level_cfg_token,
    prefix_digest,
    register_cache_clear,
    release_oversized_signals,
    store_signals,
    tape_token,
    zone_cfg_token,
)
from research.intraday_sr.engine.formations import FormationStats, formations_at
from research.intraday_sr.engine.zones import _cfg_id, fast_zones
from research.intraday_sr.types import ET, BarSet, EngineCfg, Formation, Signal, SignalCfg, Zone, as_et


class _ZoneSetup:
    """Per stable zone, for one session.

    ``arm_for`` freezes the arm bar of a (touch, rc) pair the first time it
    is known, so a later 15m snapshot cannot retarget the same setup.
    ``consumed`` is a pair that already emitted, was cancelled, or expired.
    ``block_until`` is the last bar index of an emitted order.
    """

    __slots__ = ("arm_for", "consumed", "block_until")

    def __init__(self) -> None:
        self.arm_for: dict[tuple[int, int], int | None] = {}
        self.consumed: set[tuple[int, int]] = set()
        self.block_until: int = -1


class _StableZone:
    """A snapshot zone plus the id its setup state is keyed by."""

    __slots__ = ("zone", "stable_id")

    def __init__(self, zone: Zone, stable_id: str) -> None:
        self.zone = zone
        self.stable_id = stable_id


def _overlap_width(left: Zone, right: Zone) -> float:
    return max(0.0, min(left.high, right.high) - max(left.low, right.low))


def _ranges_qualify(left: Zone, right: Zone) -> bool:
    """Same side and overlap at least half the narrower padded width.

    Exactly half qualifies. A narrower fraction does not, so a zone that
    drifts a little at every recompute cannot keep one identity forever.
    """
    if left.side != right.side:
        return False
    overlap = _overlap_width(left, right)
    smaller = min(left.high - left.low, right.high - right.low)
    if smaller <= 0.0:
        return left.low == left.high == right.low == right.high
    return overlap >= 0.5 * smaller


def _fresh_stable_id(zone: Zone, used: set[str]) -> str:
    base = zone.zone_id
    if base not in used:
        return base
    suffix = 1
    while f"{base}#{suffix}" in used:
        suffix += 1
    return f"{base}#{suffix}"


def _match_stable_ids(previous: list[_StableZone], current: list[Zone]) -> list[_StableZone]:
    """One-to-one match of ``current`` onto ``previous``.

    Qualifying pairs are assigned greedily by widest overlap, then the
    previous zone's higher score, then its lower low. A current zone with
    no qualifying previous zone gets a fresh id.
    """
    pairs: list[tuple[float, float, float, float, float, int, int]] = []
    for new_index, zone in enumerate(current):
        for prev_index, prior in enumerate(previous):
            if not _ranges_qualify(prior.zone, zone):
                continue
            overlap = _overlap_width(prior.zone, zone)
            pairs.append(
                (
                    overlap,
                    prior.zone.score,
                    prior.zone.low,
                    zone.score,
                    zone.low,
                    new_index,
                    prev_index,
                )
            )
    pairs.sort(key=lambda item: (-item[0], -item[1], item[2], -item[3], item[4], item[5], item[6]))
    assigned: dict[int, str] = {}
    used_prev: set[int] = set()
    used_ids: set[str] = set()
    for _overlap, _prev_score, _prev_low, _score, _low, new_index, prev_index in pairs:
        if new_index in assigned or prev_index in used_prev:
            continue
        assigned[new_index] = previous[prev_index].stable_id
        used_prev.add(prev_index)
        used_ids.add(previous[prev_index].stable_id)
    matched: list[_StableZone] = []
    for new_index, zone in enumerate(current):
        stable_id = assigned.get(new_index)
        if stable_id is None:
            stable_id = _fresh_stable_id(zone, used_ids)
            used_ids.add(stable_id)
        matched.append(_StableZone(zone, stable_id))
    return matched


def _book_for_recompute(
    day,
    tracked_day,
    previous: list[_StableZone],
    setups: dict[str, _ZoneSetup],
    zones: list[Zone],
) -> tuple[object, list[_StableZone], dict[str, _ZoneSetup], list[str]]:
    """Carry stable ids across a 15m recompute. A new session starts empty."""
    if day != tracked_day:
        previous = []
        setups = {}
        tracked_day = day
    if not zones:
        return tracked_day, previous, setups, []
    live = _match_stable_ids(previous, zones)
    ids = [item.stable_id for item in live]
    keep = set(ids)
    setups = {key: value for key, value in setups.items() if key in keep}
    return tracked_day, live, setups, ids


@dataclass
class SignalFunnel:
    """How many stack stages fired inside one ``signals`` call.

    ``touches``, ``holds``, and ``arms`` count bar/zone pairs on the entry
    timeframe. ``k_confirm_pass`` is how many of those arms cleared the
    optional-flag minimum. ``emits`` is the number of ``Signal`` objects
    returned. ``build_fail_no_ahead_zone`` and ``build_fail_zone_lt_1R``
    count skipped builds for ``target == "zone"`` only (SPEC v1.3.1 §5:
    skip when the zone target is missing or less than 1R away).
    """

    touches: int = 0
    holds: int = 0
    arms: int = 0
    k_confirm_pass: int = 0
    emits: int = 0
    build_fail_no_ahead_zone: int = 0
    build_fail_zone_lt_1R: int = 0
    # Formation detector, for tests B and F_*. Test A leaves these at 0.
    # ``emits`` is the emitted-signal count for every test.
    candidates: int = 0
    broken: int = 0
    invalidated: int = 0
    retest: int = 0


def signals_funnel(
    bars: BarSet,
    start: datetime,
    end: datetime,
    cfg: EngineCfg,
    sig: SignalCfg,
) -> SignalFunnel:
    """Run ``sig.test`` and return the funnel counts.

    Test A fills touch, hold, arm, and emit. Test B and ``F_*`` fill
    ``candidates``, ``broken``, ``invalidated``, ``retest``, and ``emits``.
    """
    funnel = SignalFunnel()
    list(signals(bars, start, end, cfg, sig, funnel=funnel))
    return funnel


def signals(
    bars: BarSet,
    start: datetime,
    end: datetime,
    cfg: EngineCfg,
    sig: SignalCfg,
    funnel: SignalFunnel | None = None,
) -> Iterator[Signal]:
    """Armed entries whose ``available_at`` is inside ``[start, end]``.

    ``sig.test`` selects Test A, Test B, or one formation kind
    (``F_W``, ``F_IHS``, ``F_M``, ``F_HS``). Formation tolerance is
    ``sig.pivot_tol_atr`` (FORMATIONS grid key). Test B forces 0.25.
    """
    if sig.test != "A":
        return _signals_fb(bars, start, end, cfg, sig, funnel)
    visible = bars.visible(end)
    if visible.empty or "symbol" not in visible.columns:
        return iter(())
    # Clamped highs and lows feed levels and zones only. The stack's touch,
    # arm, trigger, and stop read the unclamped extremes. prices_as_of(end)
    # hides clamps that are not visible by the window end; each 15m stamp
    # hides any clamp whose visible_at is still after that stamp.
    clamped = prices_as_of(visible, end)
    raw = _entry_prices(visible)
    start_at = as_et(start, "start")
    end_at = as_et(end, "end")
    cache_key = None
    if funnel is None:
        cache_key = (
            tape_token(clamped),
            start_at,
            end_at,
            zone_cfg_token(cfg),
            sig.variant_id,
            sig.target,
            int(sig.k_confirm),
            sig.entry_tf,
            sig.oscillator,
            float(sig.rvol_min),
            sig.test,
        )
        hit = cached_signals(cache_key)
        if hit is not None:
            return iter(hit)
        release_oversized_signals()
    found: list[Signal] = []
    source_ptr = _open_ptr(clamped) if len(clamped) and "open" in clamped.columns else 0
    raw_groups = {symbol: group for symbol, group in _symbol_frames(raw)}
    prepared = []
    group = None
    entry = None
    for symbol, group in _symbol_frames(clamped):
        if group.empty:
            continue
        entry = raw_groups.get(symbol)
        if entry is None or entry.empty:
            continue
        prep = _ensure_prep(symbol, group, entry, end_at, cfg, sig, source_ptr)
        if prep is not None and prep.available:
            prepared.append(prep)
    # Release the entry-tape copy and the groupby frames before the stamp
    # walk fills the level cache. The prepared arrays already own the numbers.
    del raw, raw_groups, clamped, group, entry
    for prep in prepared:
        found.extend(_emit(prep, start_at, end_at, cfg, sig, funnel))
    found.sort(key=lambda item: (item.available_at, item.symbol, -item.zone.score, item.zone.zone_id))
    if funnel is not None:
        funnel.emits += len(found)
    elif cache_key is not None:
        store_signals(cache_key, found)
    return iter(found)


def _symbol_frames(frame: pd.DataFrame):
    """One frame per symbol. A single symbol keeps the caller's block."""
    symbols = pd.unique(frame["symbol"])
    if len(symbols) == 1:
        yield str(symbols[0]), _five_minute(frame)
        return
    for symbol, group in frame.groupby("symbol", sort=True):
        yield str(symbol), _five_minute(group)


def _five_minute(group: pd.DataFrame) -> pd.DataFrame:
    if "tf" in group.columns:
        labels = group["tf"].to_numpy(copy=False)
        if not np.all(labels.astype(str) == "5m"):
            group = group.loc[group["tf"].astype(str) == "5m"]
    if len(group) > 1 and not group["ts"].is_monotonic_increasing:
        group = group.sort_values("ts")
    return group


def _entry_prices(frame: pd.DataFrame) -> pd.DataFrame:
    """High and low as printed. Levels and zones do not use this frame."""
    if frame.empty or "high_unclamped" not in frame.columns:
        return frame
    out = frame.copy(deep=False)
    out["high"] = np.array(frame["high_unclamped"].to_numpy(copy=False), copy=True)
    out["low"] = np.array(frame["low_unclamped"].to_numpy(copy=False), copy=True)
    return out


_PREP: dict[tuple, "_Prepared"] = {}
_PREP_MAX = 8
_SLICED = (
    "closes",
    "highs",
    "lows",
    "opens",
    "volume",
    "factors",
    "available",
    "sessions",
    "osc",
    "hist",
    "macd_line",
    "macd_signal",
    "volume_ratio",
)


def _clear_prep() -> None:
    _PREP.clear()


register_cache_clear(_clear_prep)


class _Prepared:
    """Indicator and level state for one clamped tape.

    A later call whose open array is a shorter prefix of this buffer reuses
    the arrays. Indicators at bar i depend only on bars up to i, so the slice
    matches a tape that was built on that prefix alone.
    """

    __slots__ = (
        "key",
        "symbol",
        "n_base",
        "base_close",
        "plan",
        "segments",
        "history",
        "zones_key_cfg",
        "levels_key_cfg",
        "tape_open",
        "tape_close",
        "tape_volume",
        "tape_avail",
        "day_index",
        "first_of_day",
        "ordered_days",
        "day_pos",
        "session_ord",
        "closes",
        "highs",
        "lows",
        "opens",
        "volume",
        "factors",
        "available",
        "sessions",
        "osc",
        "hist",
        "macd_line",
        "macd_signal",
        "volume_ratio",
        "oversold",
        "overbought",
        "width",
    )

    def view(self, end: datetime) -> "_Prepared":
        cut = bisect.bisect_right(self.available, end)
        if cut == len(self.available):
            return self
        twin = _Prepared.__new__(_Prepared)
        for name in self.__slots__:
            setattr(twin, name, getattr(self, name))
        for name in _SLICED:
            setattr(twin, name, getattr(self, name)[:cut])
        return twin


def _open_ptr(frame: pd.DataFrame) -> int:
    # The stored column may be float32. Casting here would allocate a new
    # buffer and the prefix identity would miss.
    values = frame["open"].to_numpy(copy=False)
    return int(values.__array_interface__["data"][0])


def _prep_identity(sig: SignalCfg, cfg: EngineCfg) -> tuple:
    return (
        sig.entry_tf,
        sig.oscillator,
        int(cfg.rvol_sessions),
        int(cfg.macd_fast),
        int(cfg.macd_slow),
        int(cfg.macd_signal),
        level_cfg_token(cfg),
    )


def _reuse_prep(
    base: pd.DataFrame,
    end: datetime,
    cfg: EngineCfg,
    sig: SignalCfg,
    source_ptr: int,
    symbol: str,
) -> _Prepared | None:
    if not source_ptr:
        return None
    key = (source_ptr, symbol, _prep_identity(sig, cfg))
    parent = _PREP.get(key)
    if parent is None:
        return None
    n = len(base)
    if n == 0 or n > parent.n_base:
        if n > parent.n_base:
            _PREP.pop(key, None)
        return None
    if float(base["close"].iloc[n - 1]) != float(parent.base_close[n - 1]):
        return None
    if float(base["open"].iloc[0]) != float(parent.tape_open[0]):
        return None
    return parent.view(end)


def _store_prep(prep: _Prepared) -> None:
    current = _PREP.get(prep.key)
    if current is not None and current.n_base > prep.n_base:
        return
    _PREP[prep.key] = prep
    while len(_PREP) > _PREP_MAX:
        _PREP.pop(next(iter(_PREP)))


def _base_donor(source_ptr: int, symbol: str, base: pd.DataFrame, cfg: EngineCfg) -> _Prepared | None:
    """Another entry timeframe on this same clamped tape.

    Pivots and the 5m touch arrays depend only on the clamped base. A second
    variant would otherwise build another full pivot list.
    """
    if not source_ptr or base.empty:
        return None
    token = level_cfg_token(cfg)
    n = len(base)
    last = float(base["close"].iloc[n - 1])
    first = float(base["open"].iloc[0])
    for prep in _PREP.values():
        if prep.key[0] != source_ptr or prep.key[1] != symbol or prep.key[2][-1] != token:
            continue
        if prep.n_base != n:
            continue
        if float(prep.base_close[n - 1]) != last or float(prep.tape_open[0]) != first:
            continue
        return prep
    return None


def _build_prep(
    symbol: str,
    base: pd.DataFrame,
    raw: pd.DataFrame,
    end: datetime,
    cfg: EngineCfg,
    sig: SignalCfg,
    source_ptr: int,
) -> _Prepared | None:
    tape = raw if sig.entry_tf == "5m" else timeframe_frame(raw, sig.entry_tf)
    if tape is None or tape.empty:
        return None
    if len(tape) > 1 and not tape["ts"].is_monotonic_increasing:
        tape = tape.sort_values("ts")
    if not (
        isinstance(tape.index, pd.RangeIndex)
        and tape.index.start == 0
        and getattr(tape.index, "step", 1) == 1
    ):
        tape = tape.reset_index(drop=True)
    if tape["available_at"].iloc[-1] > end or not tape["available_at"].is_monotonic_increasing:
        tape = tape.loc[tape["available_at"] <= end]
    if tape.empty:
        return None
    # Pivots are confirmed on the clamped tape, and a pivot whose window
    # contains a not-yet-visible bad print is dated at that clamp's visible_at.
    # Each 15m stamp then keeps only clamps with visible_at <= stamp. Touch,
    # arm, trigger, and stop stay on the unclamped entry tape.
    prep = _Prepared.__new__(_Prepared)
    prep.symbol = symbol
    prep.key = (source_ptr, symbol, _prep_identity(sig, cfg))
    prep.n_base = len(base)
    donor = _base_donor(source_ptr, symbol, base, cfg)
    if donor is None:
        prep.base_close = base["close"].to_numpy(dtype=np.float64)
        prep.plan = _LevelTape(symbol, base, cfg)
        prep.segments = _segments(base)
        prep.history = prep.plan.hashes
        prep.tape_open = base["open"].to_numpy(dtype=np.float64)
        prep.tape_close = base["close"].to_numpy(dtype=np.float64)
        prep.tape_volume = base["volume"].to_numpy(dtype=np.float64)
        prep.tape_avail = [_as_dt(value) for value in base["available_at"]]
        tape_session = [session_day(value) for value in base["session"]]
        prep.day_index = {day: pos for pos, day in enumerate(sorted(set(tape_session)))}
        first_of_day: dict = {}
        for pos, day in enumerate(tape_session):
            first_of_day.setdefault(day, pos)
        prep.first_of_day = first_of_day
        prep.ordered_days = sorted(first_of_day)
        prep.day_pos = {day: pos for pos, day in enumerate(prep.ordered_days)}
        prep.session_ord = np.array([prep.day_index[day] for day in tape_session], dtype=np.float64)
    else:
        prep.base_close = donor.base_close
        prep.plan = donor.plan
        prep.segments = donor.segments
        prep.history = donor.history
        prep.tape_open = donor.tape_open
        prep.tape_close = donor.tape_close
        prep.tape_volume = donor.tape_volume
        prep.tape_avail = donor.tape_avail
        prep.day_index = donor.day_index
        prep.first_of_day = donor.first_of_day
        prep.ordered_days = donor.ordered_days
        prep.day_pos = donor.day_pos
        prep.session_ord = donor.session_ord
    prep.zones_key_cfg = zone_cfg_token(cfg)
    prep.levels_key_cfg = level_cfg_token(cfg)
    prep.closes = tape["close"].to_numpy(dtype=np.float64)
    prep.highs = tape["high"].to_numpy(dtype=np.float64)
    prep.lows = tape["low"].to_numpy(dtype=np.float64)
    prep.opens = tape["open"].to_numpy(dtype=np.float64)
    prep.volume = tape["volume"].to_numpy(dtype=np.float64)
    if "adj_factor" in tape.columns:
        prep.factors = tape["adj_factor"].to_numpy(dtype=np.float64)
    else:
        prep.factors = np.ones(len(tape), dtype=np.float64)
    prep.available = [_as_dt(value) for value in tape["available_at"]]
    if "session" in tape.columns:
        prep.sessions = [session_day(value) for value in tape["session"]]
    else:
        prep.sessions = [prep.available[i].date() for i in range(len(tape))]
    prep.osc = _oscillator(sig.oscillator, prep.highs, prep.lows, prep.closes)
    prep.macd_line, prep.macd_signal, prep.hist = macd(
        prep.closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal
    )
    slot = minute_of_day(tape["ts"])
    session_codes = pd.factorize(np.array(prep.sessions, dtype=object), sort=False)[0]
    prep.volume_ratio = rvol(prep.volume, session_codes, slot, int(cfg.rvol_sessions))
    prep.oversold, prep.overbought = _thresholds(sig.oscillator)
    prep.width = timedelta(minutes=5 if sig.entry_tf == "5m" else 15)
    return prep


def _ensure_prep(
    symbol: str,
    clamped: pd.DataFrame,
    raw: pd.DataFrame,
    end: datetime,
    cfg: EngineCfg,
    sig: SignalCfg,
    source_ptr: int,
) -> _Prepared | None:
    if (
        isinstance(clamped.index, pd.RangeIndex)
        and clamped.index.start == 0
        and getattr(clamped.index, "step", 1) == 1
    ):
        base = clamped
    else:
        base = clamped.reset_index(drop=True)
    if base.empty:
        return None
    prep = _reuse_prep(base, end, cfg, sig, source_ptr, symbol)
    if prep is None:
        prep = _build_prep(symbol, base, raw, end, cfg, sig, source_ptr)
        if prep is None:
            return None
        _store_prep(prep)
    return prep


def _emit(
    prep: _Prepared,
    start: datetime,
    end: datetime,
    cfg: EngineCfg,
    sig: SignalCfg,
    funnel: SignalFunnel | None,
) -> list[Signal]:
    symbol = prep.symbol
    plan = prep.plan
    segments = prep.segments
    history = prep.history
    # K changes the zone key and not the level key. Take both from this call
    # so a reused tape does not serve another K's zones.
    zones_key_cfg = zone_cfg_token(cfg)
    levels_key_cfg = level_cfg_token(cfg)
    tape_open = prep.tape_open
    tape_close = prep.tape_close
    tape_volume = prep.tape_volume
    tape_avail = prep.tape_avail
    day_index = prep.day_index
    first_of_day = prep.first_of_day
    ordered_days = prep.ordered_days
    day_pos = prep.day_pos
    session_ord = prep.session_ord
    closes = prep.closes
    highs = prep.highs
    lows = prep.lows
    opens = prep.opens
    volume = prep.volume
    factors = prep.factors
    available = prep.available
    sessions = prep.sessions
    osc = prep.osc
    _line = prep.macd_line
    _signal = prep.macd_signal
    hist = prep.hist
    volume_ratio = prep.volume_ratio
    oversold = prep.oversold
    overbought = prep.overbought
    width = prep.width
    # Only the current 15m stamp's zones stay materialized. The cache holds
    # the compact history, so a full-span walk does not retain every Zone.
    current_key = None
    current_zones: list[Zone] = []
    current_ids: list[str] = []
    # Stable ids and setup state reset when the session changes.
    tracked_day = None
    previous_book: list[_StableZone] = []
    setups: dict[str, _ZoneSetup] = {}
    out: list[Signal] = []
    warmup = datetime.fromisoformat(str(cfg.warmup_date)).date()
    cutoff_cursor = 0
    for index in range(len(available)):
        stamp_at = available[index]
        if stamp_at < start or stamp_at > end:
            continue
        if sessions[index] < warmup:
            continue
        if stamp_at.astimezone(ET).time() > time(15, 0):
            continue
        recompute = floor_15m(stamp_at, sessions[index])
        if recompute is None:
            continue
        if recompute != current_key:
            current_key = recompute
            current_day = sessions[index]
            atr = plan.atr_by_day.get(current_day)
            if atr is None:
                atr = _atr_from_segments(segments, stamp_at, int(cfg.atr_length))
            if atr is None:
                current_zones = []
            else:
                while cutoff_cursor < len(tape_avail) and tape_avail[cutoff_cursor] <= recompute:
                    cutoff_cursor += 1
                cutoff = cutoff_cursor
                if cutoff == 0:
                    current_zones = []
                else:
                    origin = ordered_days[max(0, day_pos[current_day] - int(cfg.touch_sessions))]
                    begin = first_of_day[origin]
                    sl = slice(begin, cutoff)
                    ages = day_index[current_day] - session_ord[begin:cutoff]
                    digest = prefix_digest(history, cutoff - 1)
                    level_key = (digest, symbol, recompute, levels_key_cfg)
                    zone_key = (digest, symbol, sig.entry_tf, recompute, zones_key_cfg)

                    def build_zones(
                        symbol=symbol,
                        cutoff=cutoff,
                        recompute=recompute,
                        atr=atr,
                        sl=sl,
                        ages=ages,
                        level_key=level_key,
                        begin=begin,
                        origin=origin,
                    ):
                        live = cached_levels(level_key, lambda: plan.pack(plan.levels_at(cutoff, recompute)))
                        touch_low, touch_high = plan.asof_high_low(begin, cutoff, recompute)
                        return fast_zones(
                            live,
                            symbol=symbol,
                            lows=touch_low,
                            highs=touch_high,
                            opens=tape_open[sl],
                            closes=tape_close[sl],
                            volume=tape_volume[sl],
                            ages=ages,
                            last_close=float(tape_close[cutoff - 1]),
                            atr=float(atr),
                            stamp=recompute,
                            cfg=cfg,
                            pivot_not_before=origin,
                        )

                    current_zones = cached_zones(zone_key, build_zones)
            tracked_day, previous_book, setups, current_ids = _book_for_recompute(
                current_day,
                tracked_day,
                previous_book,
                setups,
                current_zones,
            )
        zones = current_zones
        if funnel is not None and zones:
            _tally(
                funnel,
                index,
                zones,
                opens,
                highs,
                lows,
                closes,
                osc,
                hist,
                _line,
                _signal,
                volume_ratio,
                oversold,
                overbought,
                cfg,
                sig,
            )
        if not zones:
            continue
        out.extend(
            _step(
                index=index,
                zones=zones,
                opens=opens,
                highs=highs,
                lows=lows,
                closes=closes,
                available=available,
                osc=osc,
                hist=hist,
                macd_line=_line,
                macd_signal=_signal,
                volume_ratio=volume_ratio,
                factors=factors,
                oversold=oversold,
                overbought=overbought,
                cfg=cfg,
                sig=sig,
                width=width,
                setups=setups,
                stable_ids=current_ids,
                funnel=funnel,
            )
        )
    return out


def _tally(
    funnel: SignalFunnel,
    index: int,
    zones: list[Zone],
    opens,
    highs,
    lows,
    closes,
    osc,
    hist,
    macd_line,
    macd_signal,
    volume_ratio,
    oversold: float,
    overbought: float,
    cfg: EngineCfg,
    sig: SignalCfg,
) -> None:
    window = int(cfg.touch_window_bars)
    for zone in zones:
        if not _is_touch(index, zone, lows, highs, closes):
            continue
        funnel.touches += 1
        rc = _hold_index(index, zone, opens, highs, lows, closes, window)
        if rc is None:
            continue
        funnel.holds += 1
        arm = _arm_index(rc, zone, highs, lows, closes, cfg)
        if arm is None:
            continue
        funnel.arms += 1
        flags = _optional_flags(
            index,
            rc,
            arm,
            zone,
            osc,
            hist,
            macd_line,
            macd_signal,
            volume_ratio,
            oversold,
            overbought,
            sig.rvol_min,
        )
        if sum(flags.values()) >= int(sig.k_confirm):
            funnel.k_confirm_pass += 1


def _step(
    *,
    index: int,
    zones: list[Zone],
    opens,
    highs,
    lows,
    closes,
    available,
    osc,
    hist,
    macd_line,
    macd_signal,
    volume_ratio,
    factors,
    oversold: float,
    overbought: float,
    cfg: EngineCfg,
    sig: SignalCfg,
    width: timedelta,
    setups: dict[str, _ZoneSetup] | None = None,
    stable_ids: list[str] | None = None,
    funnel: SignalFunnel | None = None,
) -> list[Signal]:
    found: list[Signal] = []
    window = int(cfg.touch_window_bars)
    cancel_bars = int(cfg.cancel_bars)
    lookback = window + cancel_bars + 2
    if setups is None:
        setups = {}
    for position, zone in enumerate(zones):
        sid = stable_ids[position] if stable_ids is not None else zone.zone_id
        state = setups.setdefault(sid, _ZoneSetup())
        if index <= state.block_until:
            continue
        touch_at = None
        rc_at = None
        for touch in range(index - 1, max(-1, index - lookback), -1):
            if not _is_touch(touch, zone, lows, highs, closes):
                continue
            rc = _hold_index(touch, zone, opens, highs, lows, closes, window)
            if rc is None or rc >= index:
                continue
            pair = (touch, rc)
            if pair in state.consumed:
                continue
            if index > rc + cancel_bars:
                state.consumed.add(pair)
                continue
            if _path_beyond(rc, index, zone, closes):
                state.consumed.add(pair)
                continue
            if pair in state.arm_for:
                arm = state.arm_for[pair]
            else:
                arm = _arm_index(rc, zone, highs, lows, closes, cfg)
                state.arm_for[pair] = arm
            if arm is None:
                state.consumed.add(pair)
                continue
            if arm == index:
                touch_at = touch
                rc_at = rc
                break
        if touch_at is None or rc_at is None:
            continue
        flags = _optional_flags(
            touch_at,
            rc_at,
            index,
            zone,
            osc,
            hist,
            macd_line,
            macd_signal,
            volume_ratio,
            oversold,
            overbought,
            sig.rvol_min,
        )
        if sum(flags.values()) < int(sig.k_confirm):
            continue
        signal, reason = _build(
            zone, rc_at, index, highs, lows, closes, available, factors, flags, zones, cfg, sig, width
        )
        if funnel is not None and reason == "no_ahead":
            funnel.build_fail_no_ahead_zone += 1
        elif funnel is not None and reason == "zone_lt_1r":
            funnel.build_fail_zone_lt_1R += 1
        if signal is None:
            state.consumed.add((touch_at, rc_at))
            continue
        state.consumed.add((touch_at, rc_at))
        state.block_until = index + cancel_bars
        found.append(signal)
    return found


def _path_beyond(rc: int, index: int, zone: Zone, closes) -> bool:
    """True when a close after the rejection has left the current zone."""
    for cursor in range(rc + 1, index + 1):
        if zone.side == "support" and closes[cursor] < zone.low:
            return True
        if zone.side == "resistance" and closes[cursor] > zone.high:
            return True
    return False


def _is_touch(index: int, zone: Zone, lows, highs, closes) -> bool:
    if zone.side == "support":
        return bool(lows[index] <= zone.high and closes[index] >= zone.low)
    return bool(highs[index] >= zone.low and closes[index] <= zone.high)


def _hold_index(touch: int, zone: Zone, opens, highs, lows, closes, window: int) -> int | None:
    stop = min(len(closes), touch + window)
    for cursor in range(touch, stop):
        span = max(highs[cursor] - lows[cursor], 1e-12)
        if zone.side == "support":
            close_out = closes[cursor] > zone.high
            wick = (min(opens[cursor], closes[cursor]) - lows[cursor]) >= 0.5 * span
            upper_half = closes[cursor] >= 0.5 * (highs[cursor] + lows[cursor])
            if close_out or (cursor == touch and wick and upper_half):
                return cursor
        else:
            close_out = closes[cursor] < zone.low
            wick = (highs[cursor] - max(opens[cursor], closes[cursor])) >= 0.5 * span
            lower_half = closes[cursor] <= 0.5 * (highs[cursor] + lows[cursor])
            if close_out or (cursor == touch and wick and lower_half):
                return cursor
    return _reclaim(touch, zone, closes, window)


def _reclaim(touch: int, zone: Zone, closes, window: int) -> int | None:
    stop = min(len(closes), touch + window)
    broke = False
    for cursor in range(touch, stop):
        if zone.side == "support" and closes[cursor] < zone.low:
            broke = True
        elif zone.side == "resistance" and closes[cursor] > zone.high:
            broke = True
        elif broke and zone.side == "support" and closes[cursor] > zone.high:
            return cursor
        elif broke and zone.side == "resistance" and closes[cursor] < zone.low:
            return cursor
    return None


def _arm_index(rc: int, zone: Zone, highs, lows, closes, cfg: EngineCfg) -> int | None:
    limit = min(len(closes), rc + 1 + int(cfg.cancel_bars))
    for cursor in range(rc + 1, limit):
        if zone.side == "support" and closes[cursor] < zone.low:
            return None
        if zone.side == "resistance" and closes[cursor] > zone.high:
            return None
        pullback = highs[cursor] < highs[rc] if zone.side == "support" else lows[cursor] > lows[rc]
        near = (
            lows[cursor] <= zone.high + float(cfg.arm_atr) * zone.atr_d
            if zone.side == "support"
            else highs[cursor] >= zone.low - float(cfg.arm_atr) * zone.atr_d
        )
        if pullback or near:
            return cursor
    return None


def _optional_flags(touch, rc, knowable, zone, osc, hist, macd_line, macd_signal, volume_ratio, oversold, overbought, rvol_min):
    direction_level = oversold if zone.side == "support" else overbought
    osc_flag = 0.0
    for cursor in range(max(0, touch - 2), touch + 1):
        value = osc[cursor]
        if not np.isfinite(value):
            continue
        if zone.side == "support" and value <= direction_level:
            osc_flag = 1.0
        if zone.side == "resistance" and value >= direction_level:
            osc_flag = 1.0
    macd_flag = 0.0
    for cursor in range(max(0, touch - 3), min(len(hist), touch + 4, knowable + 1)):
        if cursor < 3 or not np.isfinite(hist[cursor]):
            continue
        turn = hist[cursor] > hist[cursor - 1] < hist[cursor - 2] < hist[cursor - 3]
        if zone.side == "resistance":
            turn = hist[cursor] < hist[cursor - 1] > hist[cursor - 2] > hist[cursor - 3]
        cross = (
            np.isfinite(macd_line[cursor])
            and np.isfinite(macd_signal[cursor])
            and np.isfinite(macd_line[cursor - 1])
            and np.isfinite(macd_signal[cursor - 1])
            and (
                (macd_line[cursor - 1] <= macd_signal[cursor - 1] and macd_line[cursor] > macd_signal[cursor])
                if zone.side == "support"
                else (macd_line[cursor - 1] >= macd_signal[cursor - 1] and macd_line[cursor] < macd_signal[cursor])
            )
        )
        if turn or cross:
            macd_flag = 1.0
            break
    rvol_flag = 0.0
    for cursor in (touch, rc):
        if 0 <= cursor < len(volume_ratio) and np.isfinite(volume_ratio[cursor]) and volume_ratio[cursor] >= rvol_min:
            rvol_flag = 1.0
    return {"oscillator": osc_flag, "macd": macd_flag, "rvol": rvol_flag}


def _adjusted_offset(cfg: EngineCfg, factor: float) -> float:
    """$0.01 is an as-traded tick. Prices in the signal are adjusted."""
    if not np.isfinite(factor) or factor <= 0.0:
        factor = 1.0
    return float(cfg.entry_offset) / factor


def _build(zone, rc, arm, highs, lows, closes, available, factors, flags, zones, cfg, sig, width):
    """Build one signal, or ``(None, reason)`` when the zone target has no room.

    SPEC v1.3.1 §5 skips a trade only when the chosen target is ``zone`` and
    that zone is missing or less than 1R from the trigger. ``1R`` and ``2R``
    are measured from the stop and always build when risk is positive.
    ``targets['zone']`` is stored when an opposite zone sits ahead, including
    when it is nearer than 1R on a fixed-R variant.
    """
    direction = 1 if zone.side == "support" else -1
    offset = _adjusted_offset(cfg, float(factors[rc]))
    if direction == 1:
        trigger = float(highs[rc]) + offset
        pullback = float(np.min(lows[rc + 1 : arm + 1]))
        natural = min(zone.low, pullback) - float(cfg.stop_buffer_atr) * zone.atr_d
        floor = trigger - float(cfg.stop_floor_atr) * zone.atr_d
        stop = min(natural, floor)
        risk = trigger - stop
        ahead = [item.low for item in zones if item.side == "resistance" and item.low > trigger]
        target_zone = min(ahead) if ahead else None
        one_r = trigger + risk
        two_r = trigger + 2.0 * risk
        room = None if target_zone is None else target_zone - trigger
    else:
        trigger = float(lows[rc]) - offset
        pullback = float(np.max(highs[rc + 1 : arm + 1]))
        natural = max(zone.high, pullback) + float(cfg.stop_buffer_atr) * zone.atr_d
        floor = trigger + float(cfg.stop_floor_atr) * zone.atr_d
        stop = max(natural, floor)
        risk = stop - trigger
        ahead = [item.high for item in zones if item.side == "support" and item.high < trigger]
        target_zone = max(ahead) if ahead else None
        one_r = trigger - risk
        two_r = trigger - 2.0 * risk
        room = None if target_zone is None else trigger - target_zone
    if risk <= 0.0:
        return None, None
    if sig.target == "zone":
        if target_zone is None:
            return None, "no_ahead"
        if room < risk:
            return None, "zone_lt_1r"
    targets = {"1R": one_r, "2R": two_r}
    if target_zone is not None:
        targets["zone"] = float(target_zone)
    count = int(sum(flags.values()))
    signal = Signal(
        symbol=zone.symbol,
        tf=sig.entry_tf,
        direction=direction,
        test="A",
        zone=zone,
        formation=None,
        trigger=trigger,
        stop=stop,
        targets=targets,
        expires_at=available[arm] + int(cfg.cancel_bars) * width,
        components=flags,
        as_of_ts=available[arm],
        available_at=available[arm],
        variant_id=sig.variant_id,
        confluence=count,
    )
    return signal, None


def _oscillator(name: str, highs, lows, closes) -> np.ndarray:
    if name.startswith("stoch"):
        k_line, _d_line = stochastic(highs, lows, closes)
        return k_line
    return rsi_wilder(closes)


def _thresholds(name: str) -> tuple[float, float]:
    if name.startswith("stoch"):
        return 20.0, 80.0
    return 30.0, 70.0


def _as_dt(value) -> datetime:
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value


_FORMATION_KIND = {"F_W": "W", "F_IHS": "IHS", "F_M": "M", "F_HS": "HS"}


def _signals_fb(
    bars: BarSet,
    start: datetime,
    end: datetime,
    cfg: EngineCfg,
    sig: SignalCfg,
    funnel: SignalFunnel | None,
):
    """Test B and formation-only entries. Test A does not call this."""
    if sig.test not in ("B", "F_W", "F_IHS", "F_M", "F_HS"):
        return iter(())
    visible = bars.visible(end)
    if visible.empty or "symbol" not in visible.columns:
        return iter(())
    clamped = prices_as_of(visible, end)
    raw = _entry_prices(visible)
    start_at = as_et(start, "start")
    end_at = as_et(end, "end")
    tol = float(cfg.formation_pivot_tol if sig.test == "B" else sig.pivot_tol_atr)
    kind = None if sig.test == "B" else _FORMATION_KIND[sig.test]
    cache_key = None
    if funnel is None:
        cache_key = (
            "v1.3.5-fb",
            tape_token(clamped),
            start_at,
            end_at,
            zone_cfg_token(cfg),
            sig.variant_id,
            sig.target,
            int(sig.k_confirm),
            sig.entry_tf,
            sig.oscillator,
            float(sig.rvol_min),
            sig.test,
            tol,
        )
        hit = cached_signals(cache_key)
        if hit is not None:
            return iter(hit)
        release_oversized_signals()
    detector_stats = FormationStats()
    patterns = formations_at(
        bars,
        end_at,
        cfg,
        tol,
        tf=sig.entry_tf,
        kind=kind,
        stats=detector_stats,
    )
    if funnel is not None:
        funnel.candidates += detector_stats.candidates
        funnel.broken += detector_stats.broken
        funnel.invalidated += detector_stats.invalidated
        funnel.retest += detector_stats.retest
    by_symbol: dict[str, list[Formation]] = {}
    for formed in patterns:
        if formed.retest_ts is None:
            continue
        by_symbol.setdefault(formed.symbol, []).append(formed)
    found: list[Signal] = []
    source_ptr = _open_ptr(clamped) if len(clamped) and "open" in clamped.columns else 0
    raw_groups = {symbol: group for symbol, group in _symbol_frames(raw)}
    for symbol, group in _symbol_frames(clamped):
        entry = raw_groups.get(symbol)
        if entry is None or entry.empty:
            continue
        prep = _ensure_prep(symbol, group, entry, end_at, cfg, sig, source_ptr)
        if prep is None or not prep.available:
            continue
        found.extend(
            _emit_fb(
                prep,
                by_symbol.get(symbol, []),
                start_at,
                end_at,
                cfg,
                sig,
                funnel,
            )
        )
    found.sort(key=lambda item: (item.available_at, item.symbol, -item.zone.score, item.zone.zone_id))
    if funnel is not None:
        funnel.emits += len(found)
    elif cache_key is not None:
        store_signals(cache_key, found)
    return iter(found)


def _emit_fb(prep, patterns: list[Formation], start, end, cfg: EngineCfg, sig: SignalCfg, funnel: SignalFunnel | None) -> list[Signal]:
    if not patterns:
        return []
    avail = [as_et(stamp, "available_at") for stamp in prep.available]
    index_of = {stamp: index for index, stamp in enumerate(avail)}
    width = prep.width
    opens = [as_et(_as_dt(stamp), "ts") for stamp in _entry_opens(prep, sig.entry_tf)]
    if len(opens) != len(avail):
        opens = [stamp - width for stamp in avail]
    retest_at: dict[datetime, list[Formation]] = {}
    touch_at: dict[datetime, list[Formation]] = {}
    for formed in patterns:
        retest = as_et(formed.retest_ts, "retest_ts") if formed.retest_ts is not None else None
        if retest is None or retest not in index_of:
            continue
        retest_at.setdefault(retest, []).append(formed)
        touch_at.setdefault(as_et(formed.pivots[-1][0], "pivot"), []).append(formed)
    # stable_id -> zones touched by a formation's last pivot, plus the hold bar.
    armed: dict[str, list[tuple[str, Zone, int]]] = {}
    out: list[Signal] = []
    warmup = datetime.fromisoformat(str(cfg.warmup_date)).date()
    plan = prep.plan
    segments = prep.segments
    history = prep.history
    zones_key_cfg = zone_cfg_token(cfg)
    levels_key_cfg = level_cfg_token(cfg)
    current_key = None
    current_zones: list[Zone] = []
    current_ids: list[str] = []
    tracked_day = None
    previous_book: list[_StableZone] = []
    setups: dict[str, _ZoneSetup] = {}
    cutoff_cursor = 0
    tape_avail = prep.tape_avail
    for index, stamp in enumerate(avail):
        if prep.sessions[index] < warmup:
            continue
        if stamp.astimezone(ET).time() > time(15, 0):
            continue
        recompute = floor_15m(stamp, prep.sessions[index])
        if recompute is None:
            continue
        if recompute != current_key:
            current_key = recompute
            current_zones, current_ids, tracked_day, previous_book, setups, cutoff_cursor = _book_at(
                prep,
                index,
                recompute,
                cfg,
                sig,
                plan,
                segments,
                history,
                zones_key_cfg,
                levels_key_cfg,
                tape_avail,
                cutoff_cursor,
                tracked_day,
                previous_book,
                setups,
            )
        bar_open = opens[index]
        if sig.test == "B" and bar_open in touch_at:
            _remember_touches(
                armed,
                touch_at[bar_open],
                index,
                current_zones,
                current_ids,
                prep,
                cfg,
            )
        if stamp < start or stamp > end or stamp not in retest_at:
            continue
        for formed in retest_at[stamp]:
            built = _signals_from_formation(
                formed,
                index,
                index_of,
                current_zones,
                current_ids,
                armed,
                prep,
                cfg,
                sig,
            )
            for signal, reason in built:
                if funnel is not None and reason == "no_ahead":
                    funnel.build_fail_no_ahead_zone += 1
                elif funnel is not None and reason == "zone_lt_1r":
                    funnel.build_fail_zone_lt_1R += 1
                if signal is not None:
                    out.append(signal)
    return out


def _entry_opens(prep, tf: str) -> list:
    """Bar opens aligned with ``prep.available``. Falls back to close minus width."""
    frame = prep.plan.frame
    if tf != "5m":
        built = timeframe_frame(frame, tf)
        if built is None:
            return []
        frame = built
    if "ts" not in frame.columns:
        return []
    if len(frame) > 1 and not frame["ts"].is_monotonic_increasing:
        frame = frame.sort_values("ts")
    if frame["available_at"].iloc[-1] > prep.available[-1] or not frame["available_at"].is_monotonic_increasing:
        frame = frame.loc[frame["available_at"] <= prep.available[-1]]
    return list(frame["ts"])


def _book_at(
    prep,
    index: int,
    recompute: datetime,
    cfg: EngineCfg,
    sig: SignalCfg,
    plan,
    segments,
    history,
    zones_key_cfg: str,
    levels_key_cfg: str,
    tape_avail,
    cutoff_cursor: int,
    tracked_day,
    previous_book,
    setups,
):
    current_day = prep.sessions[index]
    atr = plan.atr_by_day.get(current_day)
    if atr is None:
        atr = _atr_from_segments(segments, recompute, int(cfg.atr_length))
    if atr is None:
        zones: list[Zone] = []
    else:
        while cutoff_cursor < len(tape_avail) and tape_avail[cutoff_cursor] <= recompute:
            cutoff_cursor += 1
        cutoff = cutoff_cursor
        if cutoff == 0:
            zones = []
        else:
            origin = prep.ordered_days[max(0, prep.day_pos[current_day] - int(cfg.touch_sessions))]
            begin = prep.first_of_day[origin]
            sl = slice(begin, cutoff)
            ages = prep.day_index[current_day] - prep.session_ord[begin:cutoff]
            digest = prefix_digest(history, cutoff - 1)
            level_key = (digest, prep.symbol, recompute, levels_key_cfg)
            zone_key = (digest, prep.symbol, sig.entry_tf, recompute, zones_key_cfg)

            def build_zones(
                symbol=prep.symbol,
                cutoff=cutoff,
                recompute=recompute,
                atr=atr,
                sl=sl,
                ages=ages,
                level_key=level_key,
                begin=begin,
                origin=origin,
            ):
                live = cached_levels(level_key, lambda: plan.pack(plan.levels_at(cutoff, recompute)))
                touch_low, touch_high = plan.asof_high_low(begin, cutoff, recompute)
                return fast_zones(
                    live,
                    symbol=symbol,
                    lows=touch_low,
                    highs=touch_high,
                    opens=prep.tape_open[sl],
                    closes=prep.tape_close[sl],
                    volume=prep.tape_volume[sl],
                    ages=ages,
                    last_close=float(prep.tape_close[cutoff - 1]),
                    atr=float(atr),
                    stamp=recompute,
                    cfg=cfg,
                    pivot_not_before=origin,
                )

            zones = cached_zones(zone_key, build_zones)
    tracked_day, previous_book, setups, ids = _book_for_recompute(
        current_day,
        tracked_day,
        previous_book,
        setups,
        zones,
    )
    return zones, ids, tracked_day, previous_book, setups, cutoff_cursor


def _remember_touches(armed, formed_list, index: int, zones, ids, prep, cfg: EngineCfg) -> None:
    if not zones:
        return
    window = int(cfg.touch_window_bars)
    for formed in formed_list:
        side = "support" if formed.kind in ("W", "IHS") else "resistance"
        saved = armed.setdefault(formed.formation_id, [])
        seen = {item[0] for item in saved}
        for zone, sid in zip(zones, ids):
            if sid in seen or zone.side != side:
                continue
            if not _is_touch(index, zone, prep.lows, prep.highs, prep.closes):
                continue
            rc = _hold_index(index, zone, prep.opens, prep.highs, prep.lows, prep.closes, window)
            if rc is None:
                continue
            saved.append((sid, zone, index, rc))
            seen.add(sid)


def _signals_from_formation(formed, index, index_of, zones, ids, armed, prep, cfg: EngineCfg, sig: SignalCfg):
    """Zero or more ``(signal, reason)`` rows. ``reason`` is set when a zone target is skipped."""
    direction = 1 if formed.kind in ("W", "IHS") else -1
    break_index = index_of.get(as_et(formed.break_ts, "break_ts"))
    if break_index is None:
        return []
    atr = prep.plan.atr_by_day.get(prep.sessions[index])
    if atr is None or not np.isfinite(atr) or atr <= 0.0:
        return []
    atr = float(atr)
    if sig.test == "B":
        touches = armed.get(formed.formation_id) or []
        if not touches:
            return []
        live = {sid: zone for zone, sid in zip(zones, ids)}
        built = []
        for sid, touched, touch_index, rc in touches:
            zone = live.get(sid, touched)
            flags = _optional_flags(
                touch_index,
                rc,
                index,
                touched,
                prep.osc,
                prep.hist,
                prep.macd_line,
                prep.macd_signal,
                prep.volume_ratio,
                prep.oversold,
                prep.overbought,
                sig.rvol_min,
            )
            if sum(flags.values()) < int(sig.k_confirm):
                continue
            built.append(
                _formation_signal(
                    formed,
                    zone,
                    direction,
                    index,
                    atr,
                    prep,
                    cfg,
                    sig,
                    flags,
                    zones,
                    pattern_and_zone=True,
                )
            )
        return built
    zone = _anchor_zone(formed, direction, atr, prep.available[index], cfg, sig.entry_tf)
    flags = {"oscillator": 0.0, "macd": 0.0, "rvol": 0.0}
    return [
        _formation_signal(
            formed,
            zone,
            direction,
            index,
            atr,
            prep,
            cfg,
            sig,
            flags,
            zones,
            pattern_and_zone=False,
        )
    ]


def _anchor_zone(formed: Formation, direction: int, atr: float, stamp, cfg: EngineCfg, tf: str) -> Zone:
    """Placeholder so a formation-only signal satisfies ``Signal.zone``.

    ``Formation.zone_id`` stays ``None``. The price is the pattern extreme.
    The zone target still comes from the real zone book.
    """
    price = float(formed.extreme[1])
    side = "support" if direction == 1 else "resistance"
    when = as_et(_as_dt(stamp), "available_at")
    return Zone(
        symbol=formed.symbol,
        low=price,
        high=price,
        side=side,
        score=0.0,
        components={},
        kinds=("formation",),
        as_of_ts=when,
        valid_from_ts=when,
        available_at=when,
        engine_cfg=_cfg_id(cfg),
        tf=tf,
        atr_d=float(atr),
    )


def _formation_signal(
    formed: Formation,
    zone: Zone,
    direction: int,
    index: int,
    atr: float,
    prep,
    cfg: EngineCfg,
    sig: SignalCfg,
    flags,
    zones,
    *,
    pattern_and_zone: bool,
):
    offset = _adjusted_offset(cfg, float(prep.factors[index]))
    extreme = float(formed.extreme[1])
    if direction == 1:
        trigger = float(prep.highs[index]) + offset
        anchor = min(float(zone.low), extreme) if pattern_and_zone else extreme
        natural = anchor - float(cfg.stop_buffer_atr) * atr
        floor = trigger - float(cfg.stop_floor_atr) * atr
        stop = min(natural, floor)
        risk = trigger - stop
        ahead = [float(item.low) for item in zones if item.side == "resistance" and float(item.low) > trigger]
        target_zone = min(ahead) if ahead else None
        one_r = trigger + risk
        two_r = trigger + 2.0 * risk
        room = None if target_zone is None else target_zone - trigger
    else:
        trigger = float(prep.lows[index]) - offset
        anchor = max(float(zone.high), extreme) if pattern_and_zone else extreme
        natural = anchor + float(cfg.stop_buffer_atr) * atr
        floor = trigger + float(cfg.stop_floor_atr) * atr
        stop = max(natural, floor)
        risk = stop - trigger
        ahead = [float(item.high) for item in zones if item.side == "support" and float(item.high) < trigger]
        target_zone = max(ahead) if ahead else None
        one_r = trigger - risk
        two_r = trigger - 2.0 * risk
        room = None if target_zone is None else trigger - target_zone
    if risk <= 0.0 or not np.isfinite(risk):
        return None, None
    if sig.target == "zone":
        if target_zone is None:
            return None, "no_ahead"
        if room < risk:
            return None, "zone_lt_1r"
    targets = {"1R": one_r, "2R": two_r}
    if target_zone is not None:
        targets["zone"] = float(target_zone)
    linked = replace(formed, zone_id=zone.zone_id) if pattern_and_zone else formed
    if linked.retest_ts is None:
        linked = replace(linked, retest_ts=as_et(prep.available[index], "retest_ts"))
    count = int(sum(flags.values()))
    signal = Signal(
        symbol=formed.symbol,
        tf=sig.entry_tf,
        direction=direction,
        test=sig.test,
        zone=zone,
        formation=linked,
        trigger=trigger,
        stop=stop,
        targets=targets,
        expires_at=as_et(prep.available[index], "available_at") + int(cfg.formation_retest_bars) * prep.width,
        components=flags,
        as_of_ts=as_et(prep.available[index], "available_at"),
        available_at=as_et(prep.available[index], "available_at"),
        variant_id=sig.variant_id,
        confluence=count,
    )
    return signal, None
