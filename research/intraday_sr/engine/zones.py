"""Zones from single-linkage clusters, recomputed at each 15m close.

The set returned for ``as_of`` is the one computed at the latest 15m close
at or before ``as_of``. It stays valid until the next 15m close. Touches,
rejections, recency, and volume are percentile-ranked across that set, then
weighted 0.30 / 0.30 / 0.20 / 0.20. Touches use the prior 20 sessions plus
today on the 5m tape. Volume is that zone's share of the profile (the prior
5 sessions plus today). The top ``K`` zones per side inside 2·ATR_d are kept.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from research.intraday_sr.data.badprint import prices_as_of
from research.intraday_sr.engine.levels import _atr_from_segments, _segments, levels_at
from research.intraday_sr.engine.tape import floor_15m, session_day
from research.intraday_sr.engine.zone_cache import cached_zones, tape_token, zone_cfg_token
from research.intraday_sr.types import BarSet, EngineCfg, Level, Zone, as_et


def fast_zones(
    levels: list[Level],
    *,
    symbol: str,
    lows: np.ndarray,
    highs: np.ndarray,
    opens: np.ndarray,
    closes: np.ndarray,
    volume: np.ndarray,
    ages: np.ndarray,
    last_close: float,
    atr: float,
    stamp: datetime,
    cfg: EngineCfg,
) -> list[Zone]:
    """Cluster and score without rebuilding a DataFrame.

    ``lows`` .. ``ages`` are the touch window already sliced to bars that
    count at ``stamp``.
    """
    if not levels or not np.isfinite(atr) or atr <= 0.0:
        return []
    prices = np.array([level.price for level in levels], dtype=np.float64)
    threshold = float(cfg.k_cluster) * atr
    clusters = _linked_clusters(prices, threshold)
    band = float(cfg.candidate_band_atr) * atr
    min_width = float(cfg.zone_pad_atr) * atr
    drafted: list[tuple[float, float, tuple[str, ...]]] = []
    for members in clusters:
        member_prices = prices[members]
        low = float(member_prices.min())
        high = float(member_prices.max())
        if high - low < min_width:
            mid = 0.5 * (low + high)
            low = mid - 0.5 * min_width
            high = mid + 0.5 * min_width
        mid = 0.5 * (low + high)
        if abs(mid - last_close) > band or low <= last_close <= high:
            continue
        kinds = tuple(sorted({levels[index].kind for index in members}))
        drafted.append((low, high, kinds))
    if not drafted:
        return []
    zlow = np.array([item[0] for item in drafted])
    zhigh = np.array([item[1] for item in drafted])
    inside = (lows[:, None] <= zhigh) & (highs[:, None] >= zlow) if len(lows) else np.zeros((0, len(drafted)), dtype=bool)
    entered = inside.copy()
    if len(entered):
        entered[1:] &= ~inside[:-1]
    touches = entered.sum(axis=0).astype(np.float64) if len(entered) else np.zeros(len(drafted))
    half_life = float(cfg.recency_half_life_sessions)
    if len(entered):
        span = np.maximum(highs - lows, 1e-12)
        lower_wick = np.minimum(opens, closes) - lows
        upper_wick = highs - np.maximum(opens, closes)
        wick = float(cfg.hvn_rejection_wick)
        support = zhigh < last_close
        support_reject = entered & (lower_wick[:, None] >= wick * span[:, None]) & (closes[:, None] > zhigh)
        resist_reject = entered & (upper_wick[:, None] >= wick * span[:, None]) & (closes[:, None] < zlow)
        reject = np.where(support[None, :], support_reject, resist_reject)
        rejections = reject.sum(axis=0).astype(np.float64)
        positions = np.arange(len(ages))[:, None]
        last_index = np.where(entered, positions, -1).max(axis=0)
        has = last_index >= 0
        recency = np.zeros(len(drafted), dtype=np.float64)
        if half_life > 0:
            recency[has] = 0.5 ** (ages[last_index[has]] / half_life)
        else:
            recency[has] = 1.0
    else:
        rejections = np.zeros(len(drafted))
        recency = np.zeros(len(drafted))
    # Profile volume is the prior ``profile_sessions`` plus today, not the
    # longer touch window. ``ages`` is sessions before the recompute day.
    if len(volume):
        profile = ages <= float(cfg.profile_sessions)
        profile_volume = volume[profile]
        profile_total = float(profile_volume.sum())
        if profile_total > 0.0 and len(profile_volume):
            vol_share = inside[profile].T.astype(np.float64) @ profile_volume / profile_total
        else:
            vol_share = np.zeros(len(drafted))
    else:
        vol_share = np.zeros(len(drafted))
    raw = np.column_stack([touches, rejections, recency, vol_share])
    ranks = np.column_stack([_percentile_rank(raw[:, column]) for column in range(4)])
    weights = (
        float(cfg.score_touches),
        float(cfg.score_rejections),
        float(cfg.score_recency),
        float(cfg.score_volume),
    )
    per_side: dict[str, list[Zone]] = {"support": [], "resistance": []}
    for index, (low, high, kinds) in enumerate(drafted):
        side = "support" if high < last_close else "resistance"
        components = {
            "touches": float(ranks[index, 0]),
            "rejections": float(ranks[index, 1]),
            "recency": float(ranks[index, 2]),
            "volume": float(ranks[index, 3]),
            "n_kinds": float(len(kinds)),
        }
        score = sum(weights[column] * ranks[index, column] for column in range(4))
        per_side[side].append(
            Zone(
                symbol=symbol,
                low=low,
                high=high,
                side=side,  # type: ignore[arg-type]
                score=float(score),
                components=components,
                kinds=kinds,
                as_of_ts=stamp,
                valid_from_ts=stamp,
                available_at=stamp,
                engine_cfg=_cfg_id(cfg),
                tf="5m",
                atr_d=atr,
            )
        )
    kept: list[Zone] = []
    limit = int(cfg.k_zones)
    for side in ("support", "resistance"):
        ranked = sorted(per_side[side], key=lambda zone: (-zone.score, -len(zone.kinds), zone.low, zone.zone_id))
        kept.extend(ranked[:limit])
    return kept


def assemble_zones(levels: list[Level], frame: pd.DataFrame, stamp: datetime, cfg: EngineCfg) -> list[Zone]:
    """Cluster ``levels`` on the tape closed by ``stamp``."""
    return _zones_from_levels(levels, frame, stamp, cfg)


def zones_at(bars: BarSet, as_of: datetime, cfg: EngineCfg) -> list[Zone]:
    """Zones knowable at ``as_of``. Empty until ATR_d and a 15m close exist."""
    visible = prices_as_of(bars.visible(as_of), as_of)
    if visible.empty or "symbol" not in visible.columns:
        return []
    stamp_key = as_et(as_of, "as_of")
    key = (tape_token(visible), stamp_key, zone_cfg_token(cfg))
    return cached_zones(key, lambda: _zones_at_frame(visible, stamp_key, cfg))


def _zones_at_frame(visible: pd.DataFrame, as_of: datetime, cfg: EngineCfg) -> list[Zone]:
    current = session_day(visible["session"].iloc[-1])
    stamp = floor_15m(as_of, current)
    if stamp is None or stamp > as_of:
        return []
    # Levels and the profile use only bars closed by the recompute.
    prefix = BarSet(visible.loc[visible["available_at"] <= stamp].reset_index(drop=True))
    if prefix.visible(stamp).empty:
        return []
    return _zones_from_levels(levels_at(prefix, stamp, cfg), prefix.visible(stamp), stamp, cfg)


def _zones_from_levels(levels: list[Level], frame: pd.DataFrame, stamp: datetime, cfg: EngineCfg) -> list[Zone]:
    if frame.empty:
        return []
    zones: list[Zone] = []
    for symbol, group in frame.groupby("symbol", sort=True):
        group = group.sort_values("ts")
        if "tf" in group.columns:
            group = group.loc[group["tf"].astype(str) == "5m"]
        if group.empty:
            continue
        atr = _atr_from_segments(_segments(group), stamp, int(cfg.atr_length))
        if atr is None:
            continue
        own = [level for level in levels if level.symbol == str(symbol)]
        zones.extend(_cluster_symbol(str(symbol), own, group, stamp, atr, cfg))
    zones.sort(key=lambda zone: (zone.symbol, zone.side, -zone.score, zone.zone_id))
    return zones


def _linked_clusters(prices: np.ndarray, threshold: float) -> list[list[int]]:
    """Single-linkage clusters on a line. Members stay in index order.

    Consecutive sorted prices within ``threshold`` are one cluster, which is
    what linking each neighbour onto the previous one does. The root is the
    leftmost sorted price, and clusters are recorded in the order their first
    original index is seen.
    """
    n = int(prices.shape[0])
    if n == 0:
        return []
    if n == 1:
        return [[0]]
    order = np.argsort(prices, kind="mergesort")
    sorted_prices = prices[order]
    gaps = np.diff(sorted_prices) > threshold
    cid = np.empty(n, dtype=np.int64)
    cid[0] = 0
    cid[1:] = np.cumsum(gaps)
    starts = np.flatnonzero(np.diff(cid, prepend=np.int64(-1)))
    roots = np.empty(n, dtype=np.int64)
    roots[order] = order[starts[cid]]
    clusters: dict[int, list[int]] = {}
    for index in range(n):
        clusters.setdefault(int(roots[index]), []).append(index)
    return list(clusters.values())


def _cluster_symbol(
    symbol: str,
    levels: list[Level],
    group: pd.DataFrame,
    stamp: datetime,
    atr: float,
    cfg: EngineCfg,
) -> list[Zone]:
    """Same zones as ``fast_zones``. The touch window is the prior sessions plus today."""
    if not levels or group.empty:
        return []
    current = session_day(group["session"].iloc[-1])
    sessions = [session_day(value) for value in group["session"]]
    unique_days = sorted(set(sessions))
    prior_days = [day for day in unique_days if day < current][-int(cfg.touch_sessions) :]
    allowed = set(prior_days)
    allowed.add(current)
    mask = np.array([day in allowed for day in sessions], dtype=bool)
    window = group.loc[mask]
    if window.empty:
        return []
    day_index = {day: index for index, day in enumerate(unique_days)}
    window_days = [session_day(value) for value in window["session"]]
    ages = np.array([day_index[current] - day_index[day] for day in window_days], dtype=np.float64)
    return fast_zones(
        levels,
        symbol=symbol,
        lows=window["low"].to_numpy(dtype=np.float64),
        highs=window["high"].to_numpy(dtype=np.float64),
        opens=window["open"].to_numpy(dtype=np.float64),
        closes=window["close"].to_numpy(dtype=np.float64),
        volume=window["volume"].to_numpy(dtype=np.float64),
        ages=ages,
        last_close=float(window["close"].iloc[-1]),
        atr=atr,
        stamp=stamp,
        cfg=cfg,
    )


def _cluster_symbol_unused_anchor(
    symbol: str,
    levels: list[Level],
    group: pd.DataFrame,
    stamp: datetime,
    atr: float,
    cfg: EngineCfg,
) -> list[Zone]:
    if not levels:
        return []
    prices = np.array([level.price for level in levels], dtype=np.float64)
    order = np.argsort(prices, kind="mergesort")
    parent = np.arange(len(levels))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = int(parent[index])
        return index

    threshold = float(cfg.k_cluster) * atr
    for left, right in zip(order, order[1:]):
        if prices[right] - prices[left] <= threshold:
            parent[find(int(right))] = find(int(left))
    clusters: dict[int, list[int]] = {}
    for index in range(len(levels)):
        clusters.setdefault(find(index), []).append(index)
    last_close = float(group["close"].iloc[-1])
    band = float(cfg.candidate_band_atr) * atr
    drafted: list[tuple[float, float, tuple[str, ...], float]] = []
    min_width = float(cfg.zone_pad_atr) * atr
    for members in clusters.values():
        member_prices = prices[members]
        low = float(member_prices.min())
        high = float(member_prices.max())
        if high - low < min_width:
            mid = 0.5 * (low + high)
            low = mid - 0.5 * min_width
            high = mid + 0.5 * min_width
        mid = 0.5 * (low + high)
        if abs(mid - last_close) > band:
            continue
        if low <= last_close <= high:
            continue
        side = "support" if high < last_close else "resistance"
        kinds = tuple(sorted({levels[index].kind for index in members}))
        drafted.append((low, high, kinds, mid))
    if not drafted:
        return []
    scored = _score(drafted, group, stamp, atr, cfg)
    per_side: dict[str, list[Zone]] = {"support": [], "resistance": []}
    for (low, high, kinds, _mid), components, score in scored:
        side = "support" if high < last_close else "resistance"
        per_side[side].append(
            Zone(
                symbol=symbol,
                low=low,
                high=high,
                side=side,  # type: ignore[arg-type]
                score=score,
                components=components,
                kinds=kinds,
                as_of_ts=stamp,
                valid_from_ts=stamp,
                available_at=stamp,
                engine_cfg=_cfg_id(cfg),
                tf="5m",
                atr_d=atr,
            )
        )
    kept: list[Zone] = []
    limit = int(cfg.k_zones)
    for side in ("support", "resistance"):
        ranked = sorted(per_side[side], key=lambda zone: (-zone.score, -len(zone.kinds), zone.low, zone.zone_id))
        kept.extend(ranked[:limit])
    return kept


def _score(
    drafted: list[tuple[float, float, tuple[str, ...], float]],
    group: pd.DataFrame,
    stamp: datetime,
    atr: float,
    cfg: EngineCfg,
) -> list[tuple[tuple, dict, float]]:
    current = session_day(group["session"].iloc[-1])
    sessions = [session_day(value) for value in group["session"]]
    unique_days = sorted(set(sessions))
    prior_days = [day for day in unique_days if day < current][-int(cfg.touch_sessions) :]
    allowed = set(prior_days)
    allowed.add(current)
    mask = np.array([day in allowed for day in sessions], dtype=bool)
    window = group.loc[mask]
    lows = window["low"].to_numpy(dtype=np.float64)
    highs = window["high"].to_numpy(dtype=np.float64)
    closes = window["close"].to_numpy(dtype=np.float64)
    opens = window["open"].to_numpy(dtype=np.float64)
    volume = window["volume"].to_numpy(dtype=np.float64)
    day_index = {day: index for index, day in enumerate(unique_days)}
    window_days = [session_day(value) for value in window["session"]]
    ages = np.array([day_index[current] - day_index[day] for day in window_days], dtype=np.float64)
    profile_rows = ages <= float(cfg.profile_sessions)
    profile_volume = volume[profile_rows] if len(volume) else volume
    profile_total = float(profile_volume.sum()) if len(volume) else 0.0
    half_life = float(cfg.recency_half_life_sessions)
    touches = np.zeros(len(drafted))
    rejections = np.zeros(len(drafted))
    recency = np.zeros(len(drafted))
    vol_share = np.zeros(len(drafted))
    for index, (low, high, _kinds, _mid) in enumerate(drafted):
        inside = (lows <= high) & (highs >= low)
        if inside.size:
            entered = inside.copy()
            entered[1:] &= ~inside[:-1]
            touches[index] = float(entered.sum())
            span = np.maximum(highs - lows, 1e-12)
            lower_wick = np.minimum(opens, closes) - lows
            upper_wick = highs - np.maximum(opens, closes)
            supportish = high < float(window["close"].iloc[-1]) if len(window) else True
            if supportish:
                reject = entered & (lower_wick >= float(cfg.hvn_rejection_wick) * span) & (closes > high)
            else:
                reject = entered & (upper_wick >= float(cfg.hvn_rejection_wick) * span) & (closes < low)
            rejections[index] = float(reject.sum())
            if entered.any():
                last_age = float(ages[np.flatnonzero(entered)[-1]])
                recency[index] = 0.5 ** (last_age / half_life) if half_life > 0 else 1.0
        if profile_total > 0.0:
            vol_share[index] = float(volume[inside & profile_rows].sum()) / profile_total
    raw = np.column_stack([touches, rejections, recency, vol_share])
    ranks = np.column_stack([_percentile_rank(raw[:, column]) for column in range(4)])
    weights = (
        float(cfg.score_touches),
        float(cfg.score_rejections),
        float(cfg.score_recency),
        float(cfg.score_volume),
    )
    scored = []
    for index, draft in enumerate(drafted):
        components = {
            "touches": float(ranks[index, 0]),
            "rejections": float(ranks[index, 1]),
            "recency": float(ranks[index, 2]),
            "volume": float(ranks[index, 3]),
            "n_kinds": float(len(draft[2])),
        }
        score = sum(weights[column] * ranks[index, column] for column in range(4))
        scored.append((draft, components, float(score)))
    return scored


def _percentile_rank(values: np.ndarray) -> np.ndarray:
    """Average rank divided by n. A single zone ranks 1."""
    n = len(values)
    if n == 0:
        return values
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(n, dtype=np.float64)
    start = 0
    while start < n:
        stop = start + 1
        while stop < n and values[order[stop]] == values[order[start]]:
            stop += 1
        # 1-based average rank of this tie group.
        average = 0.5 * ((start + 1) + stop)
        for cursor in range(start, stop):
            ranks[order[cursor]] = average / n
        start = stop
    return ranks


def _cfg_id(cfg: EngineCfg) -> str:
    return f"K{cfg.k_zones}|kc{cfg.k_cluster}"
