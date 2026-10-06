"""W, IHS, M, and HS detection (SPEC v1.3.5 F1–F7, F10).

spec_doc v1.3.5 (8504fd7c19136141a32746234b63dd084106369d).
engine_spec v1.3.5.

A formation is emitted once its break bar has closed. ``retest_ts`` is set
only when that retest bar has also closed. Neckline geometry is frozen at
the break, using clamps knowable at that bar (F10 / C1). ``zone_id`` stays
``None``; Test B fills it when it builds a signal.

``pivot_tol_atr`` defaults to ``EngineCfg.formation_pivot_tol`` (0.25). The
FORMATIONS grid passes ``SignalCfg.pivot_tol_atr`` (0.15 or 0.25).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time
from typing import Iterator, Mapping

import numpy as np
import pandas as pd

from research.intraday_sr.data.badprint import prices_as_of
from research.intraday_sr.engine.levels import (
    _all_buckets,
    _as_dt,
    _atr_from_segments,
    _epoch_ns,
    _events_from_members,
    _pivot_levels,
    _segments,
    _strict_mask,
    timeframe_frame,
)
from research.intraday_sr.engine.tape import at_time, session_day
from research.intraday_sr.engine.zone_cache import prefix_digest, prefix_hashes, register_cache_clear
from research.intraday_sr.types import BarSet, EngineCfg, Formation, Signal, as_et


@dataclass
class FormationStats:
    """Detector counts for one ``formations_at`` / signal scan.

    ``candidates`` passed spacing and tolerance (one per last pivot).
    ``invalidated`` died on a close beyond the extreme before the break.
    ``broken`` broke inside the window. ``retest`` is the subset of
    ``broken`` whose retest bar has closed.
    """

    candidates: int = 0
    broken: int = 0
    invalidated: int = 0
    retest: int = 0

    def add(self, other: "FormationStats") -> None:
        self.candidates += other.candidates
        self.broken += other.broken
        self.invalidated += other.invalidated
        self.retest += other.retest


_CACHE: dict[tuple, tuple[list[Formation], FormationStats]] = {}
_PARENT: dict[tuple, dict] = {}
_CACHE_MAX = 8
_PARENT_MAX = 4


def _clear_formation_cache() -> None:
    _CACHE.clear()
    _PARENT.clear()


register_cache_clear(_clear_formation_cache)


def formations_at(
    bars: BarSet,
    as_of: datetime,
    cfg: EngineCfg,
    pivot_tol_atr: float | None = None,
    *,
    tf: str | None = None,
    kind: str | None = None,
    stats: FormationStats | None = None,
) -> list[Formation]:
    """Formations whose break bar has closed at or before ``as_of``.

    ``tf`` and ``kind`` filter the result. Omit them to return every
    entry timeframe and kind. ``stats``, when passed, counts the filtered
    set (candidates, broken, invalidated, retest).
    """
    visible = bars.visible(as_of)
    if visible.empty or "symbol" not in visible.columns:
        return []
    tol = float(cfg.formation_pivot_tol if pivot_tol_atr is None else pivot_tol_atr)
    if not np.isfinite(tol) or tol <= 0.0:
        raise ValueError("pivot_tol_atr must be finite and > 0")
    clamped = prices_as_of(visible, as_of)
    found: list[Formation] = []
    totals = FormationStats()
    for symbol, group in _groups(clamped):
        raw = _raw_group(visible, symbol)
        patterns, part = _symbol_patterns(symbol, group, raw, cfg, tol, tf=tf, kind=kind, stats=stats is not None)
        found.extend(patterns)
        totals.add(part)
    found.sort(key=lambda item: (item.available_at, item.symbol, item.tf, item.kind, item.formation_id))
    if stats is not None:
        stats.add(totals)
    return found


def _groups(frame: pd.DataFrame):
    symbols = pd.unique(frame["symbol"])
    if len(symbols) == 1:
        yield str(symbols[0]), _five(frame)
        return
    for symbol, group in frame.groupby("symbol", sort=True):
        yield str(symbol), _five(group)


def _five(group: pd.DataFrame) -> pd.DataFrame:
    if "tf" in group.columns:
        labels = group["tf"].to_numpy(copy=False)
        if not np.all(labels.astype(str) == "5m"):
            group = group.loc[group["tf"].astype(str) == "5m"]
    if len(group) > 1 and not group["ts"].is_monotonic_increasing:
        group = group.sort_values("ts")
    if not (
        isinstance(group.index, pd.RangeIndex)
        and group.index.start == 0
        and getattr(group.index, "step", 1) == 1
    ):
        group = group.reset_index(drop=True)
    return group


def _raw_group(visible: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Unclamped highs and lows for the retest. Same row order as the clamped tape."""
    if "symbol" not in visible.columns:
        return visible
    symbols = pd.unique(visible["symbol"])
    if len(symbols) == 1:
        group = visible
    else:
        group = visible.loc[visible["symbol"].astype(str) == symbol]
    group = _five(group)
    if "high_unclamped" not in group.columns:
        return group
    high_u = group["high_unclamped"].to_numpy(copy=False)
    low_u = group["low_unclamped"].to_numpy(copy=False)
    if np.array_equal(high_u, group["high"].to_numpy(copy=False)) and np.array_equal(
        low_u, group["low"].to_numpy(copy=False)
    ):
        return group
    out = group.copy(deep=False)
    out["high"] = np.array(high_u, copy=True)
    out["low"] = np.array(low_u, copy=True)
    return out


def _geom_token(cfg: EngineCfg, tol: float) -> tuple:
    return (
        int(cfg.n_5m),
        int(cfg.n_15m),
        int(cfg.formation_pivot_gap_min),
        int(cfg.formation_pivot_gap_max),
        int(cfg.formation_break_bars),
        float(cfg.formation_head_margin_atr),
        int(cfg.formation_retest_bars),
        float(cfg.formation_retest_tol_atr),
        int(cfg.atr_length),
        round(float(tol), 8),
    )


def _symbol_patterns(
    symbol: str,
    clamped: pd.DataFrame,
    raw: pd.DataFrame,
    cfg: EngineCfg,
    tol: float,
    *,
    tf: str | None,
    kind: str | None,
    stats: bool,
) -> tuple[list[Formation], FormationStats]:
    if clamped.empty:
        return [], FormationStats()
    hashes = prefix_hashes(clamped)
    digest = prefix_digest(hashes, -1)
    token = _geom_token(cfg, tol)
    parent_key = (symbol, token)
    exact_key = (symbol, token, digest, tf, kind)
    cached = _CACHE.get(exact_key)
    if cached is not None:
        return cached
    parent = _PARENT.get(parent_key)
    if (
        not stats
        and parent is not None
        and len(hashes) <= len(parent["hashes"])
        and prefix_digest(parent["hashes"], len(hashes) - 1) == digest
    ):
        end = as_et(_as_dt(clamped["available_at"].iloc[-1]), "available_at")
        patterns = []
        for item in parent["patterns"]:
            if item.available_at > end or not _keep(item, tf, kind):
                continue
            if item.retest_ts is not None and item.retest_ts > end:
                item = replace(item, retest_ts=None)
            patterns.append(item)
        return patterns, FormationStats()
    atr = _atr_by_index(clamped, cfg)
    patterns: list[Formation] = []
    totals = FormationStats()
    timeframes = (tf,) if tf in ("5m", "15m") else ("5m", "15m")
    for entry_tf in timeframes:
        formed, part = _scan_tf(symbol, clamped, raw, cfg, tol, atr, entry_tf, kind)
        patterns.extend(formed)
        totals.add(part)
    patterns.sort(key=lambda item: (item.available_at, item.tf, item.kind, item.formation_id))
    _CACHE[exact_key] = (patterns, totals)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    if tf is None and kind is None and (parent is None or len(hashes) >= len(parent["hashes"])):
        _PARENT[parent_key] = {"hashes": hashes, "patterns": patterns}
        while len(_PARENT) > _PARENT_MAX:
            _PARENT.pop(next(iter(_PARENT)))
    return patterns, totals


def _keep(item: Formation, tf: str | None, kind: str | None) -> bool:
    if tf is not None and item.tf != tf:
        return False
    if kind is not None and item.kind != kind:
        return False
    return True


def _atr_by_index(frame: pd.DataFrame, cfg: EngineCfg) -> np.ndarray:
    out = np.full(len(frame), np.nan, dtype=np.float64)
    segments = _segments(frame)
    if not segments or "session" not in frame.columns:
        return out
    by_day: dict = {}
    running: list = []
    length = int(cfg.atr_length)
    for segment in segments:
        running.append(segment)
        by_day[segment[0]] = _atr_from_segments(running, at_time(segment[0], time(12, 0)), length)
    sessions = [session_day(value) for value in frame["session"]]
    for index, day in enumerate(sessions):
        value = by_day.get(day)
        if value is not None:
            out[index] = float(value)
    return out


def _scan_tf(
    symbol: str,
    clamped_5m: pd.DataFrame,
    raw_5m: pd.DataFrame,
    cfg: EngineCfg,
    tol: float,
    atr_5m: np.ndarray,
    tf: str,
    kind: str | None,
) -> tuple[list[Formation], FormationStats]:
    n_pivot = int(cfg.n_5m if tf == "5m" else cfg.n_15m)
    frame, events = _tf_frame(symbol, clamped_5m, tf)
    if frame is None or frame.empty or len(frame) < 2 * n_pivot + 1:
        return [], FormationStats()
    raw_frame, _raw_events = _tf_frame(symbol, raw_5m, tf) if raw_5m is not clamped_5m else (frame, events)
    if raw_frame is None or len(raw_frame) != len(frame):
        raw_frame = frame
    if tf == "5m":
        atr = atr_5m
    else:
        atr = _atr_by_index(frame, cfg) if "session" in frame.columns else _map_atr(frame, clamped_5m, atr_5m)
    rows = _pivot_rows(symbol, frame, tf, n_pivot, events)
    if not rows:
        return [], FormationStats()
    return _scan_rows(symbol, frame, raw_frame, rows, atr, cfg, tol, tf, n_pivot, kind)


def _map_atr(frame: pd.DataFrame, base: pd.DataFrame, atr_5m: np.ndarray) -> np.ndarray:
    """Prior-session ATR keyed by the 5m session, written onto ``frame`` bars."""
    out = np.full(len(frame), np.nan, dtype=np.float64)
    if "session" not in frame.columns or "session" not in base.columns or len(base) != len(atr_5m):
        return out
    day_atr: dict = {}
    sessions = [session_day(value) for value in base["session"]]
    for index, day in enumerate(sessions):
        if np.isfinite(atr_5m[index]):
            day_atr[day] = float(atr_5m[index])
    for index, value in enumerate(frame["session"]):
        found = day_atr.get(session_day(value))
        if found is not None:
            out[index] = found
    return out


def _tf_frame(symbol: str, group: pd.DataFrame, tf: str):
    if tf == "5m":
        return group, None
    if group.empty:
        return None, None
    bad = "bad_print" in group.columns and bool(np.any(group["bad_print"].to_numpy(dtype=bool)))
    if not bad:
        return timeframe_frame(group, tf), None
    tables = _all_buckets(group, full=True)
    payload = tables.get(tf)
    if not payload or not payload.get("high"):
        return None, None
    recorded = payload.pop("_record", None)
    frame = pd.DataFrame(payload)
    if recorded is None:
        return frame, None
    high_events, low_events = _events_from_members(
        group, recorded, frame["high"].to_numpy(dtype=np.float64), frame["low"].to_numpy(dtype=np.float64)
    )
    return frame, (high_events, low_events)


def _pivot_rows(symbol: str, frame: pd.DataFrame, tf: str, n: int, events) -> list[tuple[int, bool, int]]:
    """``(centre index, is_high, confirm index)`` in emission order."""
    high_events = low_events = None
    if events is not None:
        high_events, low_events = events
    levels = _pivot_levels(symbol, frame, tf, n, high_events, low_events)
    lows = frame["low"].to_numpy(dtype=np.float64)
    highs = frame["high"].to_numpy(dtype=np.float64)
    low_at = _strict_mask(lows, n, high=False)
    high_at = _strict_mask(highs, n, high=True)
    avail_ns = _epoch_ns(frame["available_at"])
    rows: list[tuple[int, bool, int]] = []
    cursor = 0
    for index in np.flatnonzero(low_at | high_at):
        index = int(index)
        for is_high, mask in ((False, low_at), (True, high_at)):
            if not bool(mask[index]):
                continue
            if cursor >= len(levels):
                return rows
            level = levels[cursor]
            cursor += 1
            target = np.int64(pd.Timestamp(level.available_at).value)
            pos = int(np.searchsorted(avail_ns, target))
            if pos >= len(avail_ns) or int(avail_ns[pos]) != int(target):
                continue
            rows.append((index, is_high, pos))
    return rows


def _scan_rows(
    symbol: str,
    frame: pd.DataFrame,
    raw: pd.DataFrame,
    rows: list[tuple[int, bool, int]],
    atr: np.ndarray,
    cfg: EngineCfg,
    tol: float,
    tf: str,
    n_pivot: int,
    kind_filter: str | None,
) -> tuple[list[Formation], FormationStats]:
    close = frame["close"].to_numpy(dtype=np.float64)
    high = frame["high"].to_numpy(dtype=np.float64)
    low = frame["low"].to_numpy(dtype=np.float64)
    raw_high = raw["high"].to_numpy(dtype=np.float64) if "high" in raw.columns else high
    raw_low = raw["low"].to_numpy(dtype=np.float64) if "low" in raw.columns else low
    if "high_unclamped" in frame.columns:
        raw_high = frame["high_unclamped"].to_numpy(dtype=np.float64)
        raw_low = frame["low_unclamped"].to_numpy(dtype=np.float64)
    if len(raw_high) != len(high):
        raw_high = high
        raw_low = low
    bad_idx, vis_ns = _bad_view(frame)
    ts = frame["ts"]
    available = frame["available_at"]
    gap_min = int(cfg.formation_pivot_gap_min)
    gap_max = int(cfg.formation_pivot_gap_max)
    break_bars = int(cfg.formation_break_bars)
    margin = float(cfg.formation_head_margin_atr)
    retest_bars = int(cfg.formation_retest_bars)
    retest_tol = float(cfg.formation_retest_tol_atr)
    lows_i, lows_c = _side_index(rows, False)
    highs_i, highs_c = _side_index(rows, True)
    found: list[Formation] = []
    stats = FormationStats()
    jobs = (
        ("W", "IHS", True, lows_i, lows_c, low, high, raw_high),
        ("M", "HS", False, highs_i, highs_c, high, low, raw_low),
    )
    for two, three, long, idx, confirm, pivot_px, span_px, span_raw in jobs:
        if idx.size == 0:
            continue
        want_two = kind_filter in (None, two)
        want_three = kind_filter in (None, three)
        if not want_two and not want_three:
            continue
        _scan_side(
            found,
            stats,
            symbol=symbol,
            tf=tf,
            two=two,
            three=three,
            want_two=want_two,
            want_three=want_three,
            long=long,
            idx=idx,
            confirm=confirm,
            pivot_px=pivot_px,
            span_px=span_px,
            span_raw=span_raw,
            close=close,
            raw_high=raw_high,
            raw_low=raw_low,
            atr=atr,
            bad_idx=bad_idx,
            vis_ns=vis_ns,
            ts=ts,
            available=available,
            tol=tol,
            gap_min=gap_min,
            gap_max=gap_max,
            break_bars=break_bars,
            margin=margin,
            retest_bars=retest_bars,
            retest_tol=retest_tol,
            n_pivot=n_pivot,
        )
    return found, stats


def _side_index(rows: list[tuple[int, bool, int]], is_high: bool) -> tuple[np.ndarray, np.ndarray]:
    chosen = [(index, confirm) for index, high, confirm in rows if high is is_high]
    if not chosen:
        return np.empty(0, dtype=np.int32), np.empty(0, dtype=np.int32)
    idx = np.array([item[0] for item in chosen], dtype=np.int32)
    confirm = np.array([item[1] for item in chosen], dtype=np.int32)
    order = np.argsort(idx, kind="mergesort")
    return idx[order], confirm[order]


def _bad_view(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    n = len(frame)
    vis = np.zeros(n, dtype=np.int64)
    if "bad_print" not in frame.columns or "bad_print_visible_at" not in frame.columns:
        return np.empty(0, dtype=np.int32), vis
    bad = frame["bad_print"].to_numpy(dtype=bool)
    if not bool(bad.any()):
        return np.empty(0, dtype=np.int32), vis
    vis_all = _epoch_ns(frame["bad_print_visible_at"])
    idx = np.flatnonzero(bad).astype(np.int32)
    vis[idx] = vis_all[idx]
    return idx, vis


def _scan_side(
    found: list[Formation],
    stats: FormationStats,
    *,
    symbol: str,
    tf: str,
    two: str,
    three: str,
    want_two: bool,
    want_three: bool,
    long: bool,
    idx: np.ndarray,
    confirm: np.ndarray,
    pivot_px: np.ndarray,
    span_px: np.ndarray,
    span_raw: np.ndarray,
    close: np.ndarray,
    raw_high: np.ndarray,
    raw_low: np.ndarray,
    atr: np.ndarray,
    bad_idx: np.ndarray,
    vis_ns: np.ndarray,
    ts,
    available,
    tol: float,
    gap_min: int,
    gap_max: int,
    break_bars: int,
    margin: float,
    retest_bars: int,
    retest_tol: float,
    n_pivot: int,
) -> None:
    n = len(close)
    prices = pivot_px[idx]
    for last_pos in range(len(idx)):
        last = int(idx[last_pos])
        confirm_at = int(confirm[last_pos])
        if confirm_at >= n:
            continue
        atr_c = atr[confirm_at] if confirm_at < len(atr) else np.nan
        if not np.isfinite(atr_c) or atr_c <= 0.0:
            continue
        left = int(np.searchsorted(idx, last - gap_max, side="left"))
        partners = idx[left:last_pos]
        partner_px = prices[left:last_pos]
        keep = partners <= last - gap_min
        partners = partners[keep]
        partner_px = partner_px[keep]
        if partners.size == 0:
            continue
        near = np.abs(partner_px - prices[last_pos]) <= tol * atr_c
        partners = partners[near]
        partner_px = partner_px[near]
        if partners.size == 0:
            continue
        if want_two:
            gaps = last - partners
            dists = np.abs(partner_px - prices[last_pos])
            best = int(np.lexsort((dists, gaps))[0])
            first = int(partners[best])
            _emit_pair(
                found,
                stats,
                kind=two,
                long=long,
                symbol=symbol,
                tf=tf,
                first=first,
                head=None,
                last=last,
                confirm_at=confirm_at,
                pivot_px=pivot_px,
                span_px=span_px,
                span_raw=span_raw,
                close=close,
                raw_high=raw_high,
                raw_low=raw_low,
                atr=atr,
                atr_c=float(atr_c),
                bad_idx=bad_idx,
                vis_ns=vis_ns,
                ts=ts,
                available=available,
                break_bars=break_bars,
                margin=margin,
                retest_bars=retest_bars,
                retest_tol=retest_tol,
                n_pivot=n_pivot,
            )
        if not want_three:
            continue
        best_key = None
        best_pair = None
        last_px = float(prices[last_pos])
        for first, first_px in zip(partners.tolist(), partner_px.tolist()):
            first = int(first)
            heads = idx[(idx > first) & (idx < last)]
            if heads.size == 0:
                continue
            shoulder_gap = abs(float(first_px) - last_px)
            for head in heads.tolist():
                head = int(head)
                if head < first + 2 or head > last - 2:
                    continue
                head_px = float(pivot_px[head])
                beyond = min(float(first_px), last_px) - margin * atr_c if long else max(float(first_px), last_px) + margin * atr_c
                if long and head_px > beyond:
                    continue
                if not long and head_px < beyond:
                    continue
                key = (last - first, shoulder_gap, last - head, head)
                if best_key is None or key < best_key:
                    best_key = key
                    best_pair = (first, head)
        if best_pair is None:
            continue
        _emit_pair(
            found,
            stats,
            kind=three,
            long=long,
            symbol=symbol,
            tf=tf,
            first=best_pair[0],
            head=best_pair[1],
            last=last,
            confirm_at=confirm_at,
            pivot_px=pivot_px,
            span_px=span_px,
            span_raw=span_raw,
            close=close,
            raw_high=raw_high,
            raw_low=raw_low,
            atr=atr,
            atr_c=float(atr_c),
            bad_idx=bad_idx,
            vis_ns=vis_ns,
            ts=ts,
            available=available,
            break_bars=break_bars,
            margin=margin,
            retest_bars=retest_bars,
            retest_tol=retest_tol,
            n_pivot=n_pivot,
        )


def _emit_pair(found, stats, **kw) -> None:
    stats.candidates += 1
    outcome = _resolve_pair(**kw)
    if outcome is None:
        return
    status, formed, has_retest = outcome
    if status == "invalidated":
        stats.invalidated += 1
        return
    if status != "broken" or formed is None:
        return
    stats.broken += 1
    if has_retest:
        stats.retest += 1
    found.append(formed)


def _resolve_pair(
    *,
    kind: str,
    long: bool,
    symbol: str,
    tf: str,
    first: int,
    head: int | None,
    last: int,
    confirm_at: int,
    pivot_px: np.ndarray,
    span_px: np.ndarray,
    span_raw: np.ndarray,
    close: np.ndarray,
    raw_high: np.ndarray,
    raw_low: np.ndarray,
    atr: np.ndarray,
    atr_c: float,
    bad_idx: np.ndarray,
    vis_ns: np.ndarray,
    ts,
    available,
    break_bars: int,
    margin: float,
    retest_bars: int,
    retest_tol: float,
    n_pivot: int,
):
    del margin, atr_c, n_pivot
    n = len(close)
    if head is None:
        spans = ((first, last),)
    else:
        spans = ((first, head), (head, last))
    end = min(n - 1, last + break_bars)
    start = confirm_at
    extreme = float(pivot_px[first])
    points = [first, last] if head is None else [first, head, last]
    for point in points:
        price = float(pivot_px[point])
        if long:
            extreme = min(extreme, price)
        else:
            extreme = max(extreme, price)
    if first + 1 < start and _killed(close[first + 1 : start], extreme, long):
        return ("invalidated", None, False)
    if start > end:
        return ("expired", None, False)
    window = np.arange(start, end + 1)
    neck = _neckline_at(spans, span_px, span_raw, bad_idx, vis_ns, available, window, long)
    if neck is None:
        return ("expired", None, False)
    segment = close[start : end + 1]
    if long:
        inv = np.flatnonzero(segment < extreme)
        brk = np.flatnonzero(segment > neck)
    else:
        inv = np.flatnonzero(segment > extreme)
        brk = np.flatnonzero(segment < neck)
    first_inv = int(inv[0]) if inv.size else None
    first_brk = int(brk[0]) if brk.size else None
    if first_inv is not None and (first_brk is None or first_inv <= first_brk):
        return ("invalidated", None, False)
    if first_brk is None:
        return ("expired", None, False)
    break_idx = start + first_brk
    slope, price_at_break = _line_at_break(
        spans, span_px, span_raw, bad_idx, vis_ns, available, break_idx, long
    )
    if price_at_break is None or slope is None:
        return ("expired", None, False)
    retest_idx = _retest_index(
        break_idx,
        price_at_break,
        slope,
        extreme,
        long,
        close,
        raw_low,
        raw_high,
        atr,
        retest_bars,
        retest_tol,
    )
    pivots = _pivot_tuples(ts, pivot_px, points)
    break_ts = _as_dt(available.iloc[break_idx])
    confirmed_ts = _as_dt(available.iloc[confirm_at])
    retest_ts = None if retest_idx is None else _as_dt(available.iloc[retest_idx])
    formed = Formation(
        kind=kind,
        symbol=symbol,
        tf=tf,
        pivots=pivots,
        neckline=(price_at_break, slope),
        invalidation=extreme,
        break_ts=break_ts,
        retest_ts=retest_ts,
        confirmed_ts=confirmed_ts,
        as_of_ts=break_ts,
        available_at=break_ts,
        zone_id=None,
    )
    return ("broken", formed, retest_idx is not None)


def _killed(segment: np.ndarray, extreme: float, long: bool) -> bool:
    if segment.size == 0:
        return False
    if long:
        return bool(np.any(segment < extreme))
    return bool(np.any(segment > extreme))


def _neckline_at(spans, span_px, span_raw, bad_idx, vis_ns, available, window, long: bool):
    """Neckline price at each index in ``window``. None when a span is empty."""
    if _spans_clean(spans, bad_idx):
        anchors = []
        for left, right in spans:
            point = _span_point(span_px, left, right, long)
            if point is None:
                return None
            anchors.append(point)
        if len(anchors) == 1:
            return np.full(window.shape, anchors[0][0], dtype=np.float64)
        (p1, i1), (p2, i2) = anchors
        if i2 == i1:
            return None
        slope = (p2 - p1) / float(i2 - i1)
        return p1 + slope * (window.astype(np.float64) - float(i1))
    out = np.empty(window.shape, dtype=np.float64)
    avail_ns = _epoch_ns(available)
    for cursor, bar in enumerate(window.tolist()):
        _slope, price = _line_at_break(spans, span_px, span_raw, bad_idx, vis_ns, available, int(bar), long, avail_ns)
        if price is None:
            return None
        out[cursor] = price
    return out


def _line_at_break(spans, span_px, span_raw, bad_idx, vis_ns, available, break_idx: int, long: bool, avail_ns=None):
    if avail_ns is None:
        avail_ns = _epoch_ns(available)
    t_ns = int(avail_ns[break_idx])
    anchors = []
    for left, right in spans:
        use = span_px
        if bad_idx.size and _span_has_bad(left, right, bad_idx):
            use = _asof_span(span_px, span_raw, bad_idx, vis_ns, left, right, t_ns)
            point = _span_point(use, left, right, long)
        else:
            point = _span_point(use, left, right, long)
        if point is None:
            return None, None
        anchors.append(point)
    if len(anchors) == 1:
        return 0.0, anchors[0][0]
    (p1, i1), (p2, i2) = anchors
    if i2 == i1:
        return None, None
    slope = (p2 - p1) / float(i2 - i1)
    return slope, p1 + slope * (float(break_idx) - float(i1))


def _spans_clean(spans, bad_idx: np.ndarray) -> bool:
    if bad_idx.size == 0:
        return True
    return all(not _span_has_bad(left, right, bad_idx) for left, right in spans)


def _span_has_bad(left: int, right: int, bad_idx: np.ndarray) -> bool:
    if bad_idx.size == 0 or right - left < 2:
        return False
    pos = int(np.searchsorted(bad_idx, left + 1, side="left"))
    return pos < len(bad_idx) and int(bad_idx[pos]) < right


def _asof_span(values, raw, bad_idx, vis_ns, left: int, right: int, t_ns: int) -> np.ndarray:
    out = values
    changed = False
    for bar in bad_idx.tolist():
        bar = int(bar)
        if left < bar < right and int(vis_ns[bar]) > t_ns:
            if not changed:
                out = values.copy()
                changed = True
            out[bar] = raw[bar]
    return out


def _span_point(values: np.ndarray, left: int, right: int, long: bool):
    if right - left < 2:
        return None
    segment = values[left + 1 : right]
    if segment.size == 0 or not np.any(np.isfinite(segment)):
        return None
    # Long neckline is the max high. Short neckline is the min low.
    offset = int(np.argmax(segment) if long else np.argmin(segment))
    return float(segment[offset]), left + 1 + offset


def _retest_index(
    break_idx: int,
    neck: float,
    slope: float,
    extreme: float,
    long: bool,
    close: np.ndarray,
    raw_low: np.ndarray,
    raw_high: np.ndarray,
    atr: np.ndarray,
    retest_bars: int,
    retest_tol: float,
) -> int | None:
    last = min(len(close) - 1, break_idx + retest_bars)
    for bar in range(break_idx + 1, last + 1):
        if long and close[bar] < extreme:
            return None
        if not long and close[bar] > extreme:
            return None
        atr_bar = atr[bar] if bar < len(atr) else np.nan
        if not np.isfinite(atr_bar) or atr_bar <= 0.0:
            continue
        band = retest_tol * float(atr_bar)
        level = neck + slope * float(bar - break_idx)
        if long:
            if raw_low[bar] <= level + band and close[bar] >= level - band:
                return bar
        elif raw_high[bar] >= level - band and close[bar] <= level + band:
            return bar
    return None


def _pivot_tuples(ts, pivot_px: np.ndarray, points: list[int]) -> tuple[tuple[datetime, float], ...]:
    ordered = sorted(points)
    return tuple((_as_dt(ts.iloc[index]), float(pivot_px[index])) for index in ordered)


_FORMATION_TEST = {"F_W": "W", "F_IHS": "IHS", "F_M": "M", "F_HS": "HS"}


def formation_signals(
    bars: BarSet,
    start: datetime,
    end: datetime,
    cfg: EngineCfg,
    variant: Mapping,
) -> Iterator[Signal]:
    """Signals for one FORMATIONS-grid row.

    The zone target for formations is locked at ``k_zones=5``. The harness
    passes ``EngineCfg()`` or ``EngineCfg(k_zones=5)``. This function uses
    ``cfg`` as given and does not override ``k_zones``.
    """
    # Imported here: signals.py already imports this module.
    from research.intraday_sr.engine.signals import signals
    from research.intraday_sr.types import SignalCfg

    test = str(variant["test"])
    kind = _FORMATION_TEST.get(test)
    if kind is None or str(variant["kind"]) != kind:
        raise ValueError(f"formation_signals variant must be an F_* row, got {test!r}/{variant.get('kind')!r}")
    sig = SignalCfg(
        oscillator="rsi14_30_70",
        rvol_min=1.5,
        entry_tf=variant["entry_tf"],
        target=variant["target"],
        k_confirm=0,
        variant_id=str(variant["variant_id"]),
        test=test,  # type: ignore[arg-type]
        pivot_tol_atr=float(variant["pivot_tol_atr"]),
    )
    return signals(bars, start, end, cfg, sig)


def formations_in(
    bars: BarSet,
    start: datetime,
    end: datetime,
    cfg: EngineCfg,
    pivot_tol: float,
) -> Iterator[Formation]:
    """Formations whose break has closed inside ``[start, end]``.

    Both entry timeframes and every kind are included. Each object is the
    one ``formations_at`` would return at that formation's own
    ``available_at``, so ``retest_ts`` is None (the retest bar is later).
    With no delayed bad-print clamp, one scan at ``end`` plus clearing
    ``retest_ts`` is that object. A clamp whose bar has closed by the break
    but whose ``bad_print_visible_at`` is still after the break can change
    the prefix, so those break stamps are rescanned.
    """
    start_at = as_et(start, "start")
    end_at = as_et(end, "end")
    affected = set(_clamp_break_stamps(bars, start_at, end_at))
    chosen: dict[str, Formation] = {}

    def keep(item: Formation) -> None:
        if item.retest_ts is not None:
            item = replace(item, retest_ts=None)
        previous = chosen.get(item.formation_id)
        if previous is None or item.available_at < previous.available_at:
            chosen[item.formation_id] = item

    for item in formations_at(bars, end_at, cfg, pivot_tol):
        if item.available_at < start_at or item.available_at > end_at:
            continue
        if item.available_at in affected:
            continue
        keep(item)
    for stamp in sorted(affected):
        for item in formations_at(bars, stamp, cfg, pivot_tol):
            if item.available_at == stamp:
                keep(item)
    ordered = sorted(chosen.values(), key=lambda item: (item.available_at, item.formation_id))
    return iter(ordered)


def _clamp_break_stamps(bars: BarSet, start: datetime, end: datetime) -> list[datetime]:
    """Bar closes in ``[start, end]`` whose prefix a not-yet-visible clamp can change."""
    visible = bars.visible(end)
    if (
        visible.empty
        or "bad_print" not in visible.columns
        or "bad_print_visible_at" not in visible.columns
        or "available_at" not in visible.columns
    ):
        return []
    bad = visible["bad_print"].to_numpy(dtype=bool)
    if not bool(np.any(bad)):
        return []
    avail = visible["available_at"]
    vis = visible["bad_print_visible_at"]
    windows: list[tuple[datetime, datetime]] = []
    for idx in np.flatnonzero(bad):
        bar_at = as_et(_as_dt(avail.iloc[int(idx)]), "available_at")
        vis_at = as_et(_as_dt(vis.iloc[int(idx)]), "bad_print_visible_at")
        if vis_at > bar_at:
            windows.append((bar_at, vis_at))
    if not windows:
        return []
    stamps: list[datetime] = []
    seen: set[datetime] = set()
    for value in avail:
        stamp = as_et(_as_dt(value), "available_at")
        if stamp < start or stamp > end or stamp in seen:
            continue
        if any(bar_at <= stamp < vis_at for bar_at, vis_at in windows):
            seen.add(stamp)
            stamps.append(stamp)
    return stamps
