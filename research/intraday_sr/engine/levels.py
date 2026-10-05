"""Causal level candidates (spec §4 and §5).

Pivots, prior-day H/L/C, the opening range, session VWAP, HVN shelf
edges and midpoints, and as-traded round numbers. A level is emitted only
once its confirming bar has closed. Prices are the clamped tape: callers
that still need the raw high or low read ``high_unclamped`` / ``low_unclamped``.
"""

from __future__ import annotations

import bisect
from datetime import datetime, time

import numpy as np
import pandas as pd

from research.intraday_sr.data.badprint import prices_as_of
from research.intraday_sr.engine.tape import at_time, session_day
from research.intraday_sr.types import ET, BarSet, EngineCfg, Level


def _strict_mask(values: np.ndarray, n: int, *, high: bool) -> np.ndarray:
    size = len(values)
    ok = np.ones(size, dtype=bool)
    if size < 2 * n + 1 or n < 1:
        ok[:] = False
        return ok
    ok[:n] = False
    ok[size - n :] = False
    for offset in range(1, n + 1):
        left = values[n - offset : size - n - offset]
        right = values[n + offset : size - n + offset]
        centre = values[n : size - n]
        if high:
            ok[n : size - n] &= (centre > left) & (centre > right)
        else:
            ok[n : size - n] &= (centre < left) & (centre < right)
    return ok


def timeframe_frame(group: pd.DataFrame, tf: str) -> pd.DataFrame | None:
    """Closed ``tf`` bars for the signal stack. ``5m`` returns ``group``."""
    if tf == "5m":
        return group
    built = _bucket_table(group, tf, full=True)
    if built is None or not built["high"]:
        return None
    return pd.DataFrame(built)


def levels_at(bars: BarSet, as_of: datetime, cfg: EngineCfg) -> list[Level]:
    """Candidates knowable at ``as_of``, inside ±2·ATR_d of the last close when ATR exists."""
    frame = prices_as_of(bars.visible(as_of), as_of)
    if frame.empty or "symbol" not in frame.columns:
        return []
    found: list[Level] = []
    for symbol, group in frame.groupby("symbol", sort=True):
        group = group.sort_values("ts")
        if "tf" in group.columns:
            group = group.loc[group["tf"].astype(str) == "5m"]
        if group.empty:
            continue
        found.extend(_symbol_levels(str(symbol), group, as_of, cfg))
    found.sort(key=lambda level: (level.symbol, level.available_at, level.kind, level.price))
    return found


def _symbol_levels(symbol: str, group: pd.DataFrame, as_of: datetime, cfg: EngineCfg) -> list[Level]:
    pivots, prices = _pivot_tape(symbol, group, cfg)
    return _levels_at_stamp(symbol, group, pivots, prices, as_of, cfg)


def _pivot_tape(
    symbol: str, group: pd.DataFrame, cfg: EngineCfg
) -> tuple[list[Level], np.ndarray]:
    """Pivots on the whole tape. ``available_at`` is the confirmation close.

    A later stamp keeps a pivot only when that confirmation is already in
    the past, so this list can be built once and sliced per 15m close.
    """
    found: list[Level] = []
    found.extend(_pivot_levels(symbol, group, "5m", int(cfg.n_5m)))
    found.extend(_higher_pivots(symbol, group, cfg))
    found.sort(key=lambda level: (level.available_at, level.kind, level.price))
    prices = np.array([level.price for level in found], dtype=np.float64)
    return found, prices


def _levels_at_stamp(
    symbol: str,
    prefix: pd.DataFrame,
    pivots: list[Level],
    prices: np.ndarray,
    as_of: datetime,
    cfg: EngineCfg,
) -> list[Level]:
    """Candidates at ``as_of`` from bars closed by ``as_of``.

    The band is ±2·ATR_d around ``prefix``'s last close, not a later close.
    """
    if prefix.empty:
        return []
    segments = _segments(prefix)
    if not segments:
        return []
    atr = _atr_from_segments(segments, as_of, int(cfg.atr_length))
    last_close = float(prefix["close"].iloc[-1])
    cut = bisect.bisect_right(pivots, as_of, key=lambda level: level.available_at)
    if atr is None:
        chosen = pivots[:cut]
    else:
        band = float(cfg.candidate_band_atr) * atr + 1e-9
        mask = np.abs(prices[:cut] - last_close) <= band
        chosen = [pivots[int(index)] for index in np.flatnonzero(mask)]
    _day, _high, _low, _close, start, stop = segments[-1]
    today = prefix.iloc[int(start) : int(stop)]
    levels: list[Level] = list(chosen)
    levels.extend(_prior_day(symbol, segments, as_of))
    levels.extend(_opening_range(symbol, today, as_of))
    vwap = _session_vwap(symbol, today)
    if vwap is not None:
        levels.append(vwap)
    if atr is not None:
        levels.extend(_hvn(symbol, prefix, segments, as_of, atr, cfg))
        levels.extend(_rounds(symbol, prefix, as_of, atr, cfg))
        band = float(cfg.candidate_band_atr) * atr + 1e-9
        levels = [level for level in levels if abs(level.price - last_close) <= band]
    levels.sort(key=lambda level: (level.symbol, level.available_at, level.kind, level.price))
    return levels


def _segments(group: pd.DataFrame) -> list[tuple]:
    """Contiguous sessions as (day, high, low, close, start, stop)."""
    if group.empty or "session" not in group.columns:
        return []
    codes, uniques = pd.factorize(group["session"], sort=False)
    changes = np.flatnonzero(np.diff(codes)) + 1
    starts = np.concatenate(([0], changes))
    ends = np.concatenate((changes, [len(codes)]))
    high = group["high"].to_numpy(dtype=np.float64)
    low = group["low"].to_numpy(dtype=np.float64)
    close = group["close"].to_numpy(dtype=np.float64)
    rows = []
    for start, stop, session in zip(starts, ends, uniques):
        start = int(start)
        stop = int(stop)
        rows.append(
            (
                session_day(session),
                float(high[start:stop].max()),
                float(low[start:stop].min()),
                float(close[stop - 1]),
                start,
                stop,
            )
        )
    return rows


def _atr_from_segments(segments: list[tuple], as_of: datetime, length: int) -> float | None:
    today = as_of.astimezone(ET).date()
    daily = [(high, low, close) for day, high, low, close, _s, _e in segments if day < today]
    if len(daily) < length + 1:
        return None
    true_ranges = []
    for index in range(1, len(daily)):
        high, low, close = daily[index]
        prev_close = daily[index - 1][2]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr = sum(true_ranges[:length]) / length
    for true_range in true_ranges[length:]:
        atr = (atr * (length - 1) + true_range) / length
    if not np.isfinite(atr) or atr <= 0.0:
        return None
    return float(atr)


def _pivot_levels(symbol: str, group: pd.DataFrame, tf: str, n: int) -> list[Level]:
    if len(group) < 2 * n + 1:
        return []
    lows = group["low"].to_numpy(dtype=np.float64)
    highs = group["high"].to_numpy(dtype=np.float64)
    low_at = _strict_mask(lows, n, high=False)
    high_at = _strict_mask(highs, n, high=True)
    stamps = group["available_at"].tolist()
    found: list[Level] = []
    for index in np.flatnonzero(low_at | high_at):
        available = stamps[int(index) + n]
        if bool(low_at[index]):
            found.append(_level(symbol, f"pivot_{tf}", float(lows[index]), available))
        if bool(high_at[index]):
            found.append(_level(symbol, f"pivot_{tf}", float(highs[index]), available))
    return found


def _higher_pivots(symbol: str, group: pd.DataFrame, cfg: EngineCfg) -> list[Level]:
    """Pivots on 15m, 1h, and 1d from closed buckets."""
    found: list[Level] = []
    tables = _all_buckets(group, full=False)
    for tf, n in (("15m", int(cfg.n_15m)), ("1h", int(cfg.n_1h)), ("1d", int(cfg.n_1d))):
        payload = tables[tf]
        if not payload["high"]:
            continue
        found.extend(_pivot_levels(symbol, pd.DataFrame(payload), tf, n))
    return found


def _bucket_table(group: pd.DataFrame, tf: str, *, full: bool) -> dict | None:
    tables = _all_buckets(group, full=full)
    return tables.get(tf)


def _all_buckets(group: pd.DataFrame, *, full: bool) -> dict[str, dict]:
    """One 5m walk. 15m and 1h need their closing bar. 1d needs the session close."""
    from datetime import timedelta

    from research.intraday_sr.data.calendar import session_close, session_open

    empty = {"high": [], "low": [], "ts": [], "available_at": []}
    if full:
        for key in ("open", "close", "volume", "vwap", "symbol", "session", "adj_factor"):
            empty[key] = []
    built = {tf: {key: [] for key in empty} for tf in ("15m", "1h", "1d")}
    if group.empty or "session" not in group.columns:
        return built
    codes, uniques = pd.factorize(group["session"], sort=False)
    changes = np.flatnonzero(np.diff(codes)) + 1
    starts = np.concatenate(([0], changes))
    ends = np.concatenate((changes, [len(codes)]))
    ts = [_as_dt(value) for value in group["ts"]]
    av = [_as_dt(value) for value in group["available_at"]]
    high = group["high"].to_numpy(dtype=np.float64)
    low = group["low"].to_numpy(dtype=np.float64)
    open_ = group["open"].to_numpy(dtype=np.float64) if full else None
    close = group["close"].to_numpy(dtype=np.float64) if full else None
    volume = group["volume"].to_numpy(dtype=np.float64) if full else None
    vwap = group["vwap"].to_numpy(dtype=np.float64) if full and "vwap" in group.columns else close
    symbol = group["symbol"].iloc[0] if full and "symbol" in group.columns else ""
    factor = float(group["adj_factor"].iloc[0]) if full and "adj_factor" in group.columns else 1.0
    width_15 = timedelta(minutes=15)
    width_1h = timedelta(hours=1)
    half_hour = timedelta(minutes=30)
    for start, stop, session in zip(starts, ends, uniques):
        day_key = session_day(session)
        open_at = session_open(day_key)
        close_at = session_close(day_key)
        if open_at is None or close_at is None:
            continue
        buckets_15: dict[datetime, list[int]] = {}
        buckets_1h: dict[datetime, list[int]] = {}
        closed = False
        day_members: list[int] = []
        for index in range(int(start), int(stop)):
            opened = ts[index]
            if opened < open_at or av[index] > close_at:
                continue
            day_members.append(index)
            if av[index] == close_at:
                closed = True
            step_15 = int((opened - open_at) // width_15)
            buckets_15.setdefault(open_at + step_15 * width_15, []).append(index)
            if opened >= close_at - half_hour:
                buckets_1h.setdefault(close_at - half_hour, []).append(index)
            else:
                step_h = int((opened - open_at) // width_1h)
                buckets_1h.setdefault(open_at + step_h * width_1h, []).append(index)
        _emit_closed(built["15m"], buckets_15, width_15, close_at, high, low, av, open_, close, volume, vwap, symbol, day_key, factor, full)
        _emit_closed(built["1h"], buckets_1h, width_1h, close_at, high, low, av, open_, close, volume, vwap, symbol, day_key, factor, full)
        if closed and day_members:
            _store_members(built["1d"], day_members, open_at, close_at, high, low, open_, close, volume, vwap, symbol, day_key, factor, full)
    return built


def _emit_closed(bucket, groups, width, close_at, high, low, available, open_, close, volume, vwap, symbol, day, factor, full) -> None:
    for key in sorted(groups):
        end = key + width
        if end > close_at:
            end = close_at
        members = groups[key]
        if not any(available[index] == end for index in members):
            continue
        _store_members(bucket, members, key, end, high, low, open_, close, volume, vwap, symbol, day, factor, full)


def _store_members(bucket, members, start, end, high, low, open_, close, volume, vwap, symbol, day, factor, full) -> None:
    bucket["high"].append(float(high[members].max()))
    bucket["low"].append(float(low[members].min()))
    bucket["ts"].append(start)
    bucket["available_at"].append(end)
    if not full:
        return
    bucket["open"].append(float(open_[members[0]]))
    bucket["close"].append(float(close[members[-1]]))
    vol = volume[members]
    bucket["volume"].append(float(vol.sum()))
    weights = vwap[members]
    total = float(vol.sum())
    bucket["vwap"].append(float((weights * vol).sum() / total) if total > 0 else float(close[members[-1]]))
    bucket["symbol"].append(symbol)
    bucket["session"].append(day)
    bucket["adj_factor"].append(factor)


def _as_dt(value) -> datetime:
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value


def _prior_day(symbol: str, segments: list[tuple], as_of: datetime) -> list[Level]:
    if len(segments) < 2:
        return []
    current = segments[-1][0]
    prior = None
    for day, high, low, close, _start, _stop in segments:
        if day < current:
            prior = (day, high, low, close)
    if prior is None:
        return []
    available = at_time(current, time(9, 30))
    if available > as_of:
        return []
    _day, high, low, close = prior
    return [
        _level(symbol, "pdh", high, available),
        _level(symbol, "pdl", low, available),
        _level(symbol, "pdc", close, available),
    ]


def _opening_range(symbol: str, group: pd.DataFrame, as_of: datetime) -> list[Level]:
    current = session_day(group["session"].iloc[-1])
    available = at_time(current, time(10, 0))
    if available > as_of:
        return []
    start = at_time(current, time(9, 30))
    stamps = group["ts"]
    window = group.loc[(stamps >= start) & (stamps < available)]
    if window.empty:
        return []
    return [
        _level(symbol, "orh", float(window["high"].max()), available),
        _level(symbol, "orl", float(window["low"].min()), available),
    ]


def _session_vwap(symbol: str, group: pd.DataFrame) -> Level | None:
    current = session_day(group["session"].iloc[-1])
    today = group.loc[[session_day(value) == current for value in group["session"]]]
    if today.empty:
        return None
    volume = today["volume"].to_numpy(dtype=np.float64)
    price = today["vwap"].to_numpy(dtype=np.float64) if "vwap" in today.columns else today["close"].to_numpy(dtype=np.float64)
    total = float(volume.sum())
    if total <= 0.0:
        value = float(today["close"].iloc[-1])
    else:
        value = float((price * volume).sum() / total)
    return _level(symbol, "vwap", value, today["available_at"].iloc[-1])


def _hvn(symbol: str, group: pd.DataFrame, segments: list[tuple], as_of: datetime, atr: float, cfg: EngineCfg) -> list[Level]:
    if not segments:
        return []
    current = segments[-1][0]
    chosen = [row for row in segments if row[0] < current][-int(cfg.profile_sessions) :]
    chosen = [row for row in chosen if row[0] <= current]
    today = [row for row in segments if row[0] == current]
    chosen = chosen + today
    if not chosen:
        return []
    high = group["high"].to_numpy(dtype=np.float64)
    low = group["low"].to_numpy(dtype=np.float64)
    close = group["close"].to_numpy(dtype=np.float64)
    vol = group["volume"].to_numpy(dtype=np.float64)
    parts_t = []
    parts_v = []
    for _day, _h, _l, _c, start, stop in chosen:
        parts_t.append((high[start:stop] + low[start:stop] + close[start:stop]) / 3.0)
        parts_v.append(vol[start:stop])
    typical = np.concatenate(parts_t)
    volume = np.concatenate(parts_v)
    bin_size = float(cfg.profile_bin_atr) * atr
    if bin_size <= 0.0:
        return []
    keys = np.round(typical / bin_size) * bin_size
    order = np.argsort(keys)
    keys = keys[order]
    volume = volume[order]
    unique, starts = np.unique(keys, return_index=True)
    totals = np.add.reduceat(volume, starts)
    if totals.size == 0:
        return []
    ranked = np.sort(totals)
    cutoff_index = min(len(ranked) - 1, max(0, int(len(ranked) * float(cfg.profile_percentile))))
    cutoff = ranked[cutoff_index]
    hot = unique[totals >= cutoff]
    if hot.size == 0:
        return []
    shelves: list[tuple[float, float]] = []
    start = prev = float(hot[0])
    for price in hot[1:]:
        price = float(price)
        if abs(price - prev - bin_size) <= bin_size * 0.51:
            prev = price
        else:
            shelves.append((start, prev))
            start = prev = price
    shelves.append((start, prev))
    available = group["available_at"].iloc[-1]
    found: list[Level] = []
    for low, high in shelves:
        mid = 0.5 * (low + high)
        found.append(_level(symbol, "hvn", low, available))
        found.append(_level(symbol, "hvn", high, available))
        found.append(_level(symbol, "hvn", mid, available))
    return found


def _rounds(symbol: str, group: pd.DataFrame, as_of: datetime, atr: float, cfg: EngineCfg) -> list[Level]:
    last = float(group["close"].iloc[-1])
    factor = float(group["adj_factor"].iloc[-1]) if "adj_factor" in group.columns else 1.0
    if not np.isfinite(factor) or factor <= 0.0:
        factor = 1.0
    traded = last * factor
    step = _round_step(traded, cfg)
    band = float(cfg.candidate_band_atr) * atr * factor
    if step <= 0.0 or band < 0.0:
        return []
    low = traded - band
    high = traded + band
    first = np.floor(low / step) * step
    cursor = float(first)
    available = group["available_at"].iloc[-1]
    found: list[Level] = []
    # A wide ATR can span many rounds. Cap the walk so a bad ATR cannot explode.
    guard = 0
    while cursor <= high + step * 0.5 and guard < 400:
        if cursor >= low - step * 0.5 and cursor > 0.0:
            found.append(_level(symbol, "round", cursor / factor, available))
        cursor += step
        guard += 1
    return found


def _round_step(traded: float, cfg: EngineCfg) -> float:
    if traded < 50.0:
        return float(cfg.round_step_under_50)
    if traded < 250.0:
        return float(cfg.round_step_under_250)
    if traded < 1000.0:
        return float(cfg.round_step_under_1000)
    return float(cfg.round_step_else)


def _level(symbol: str, kind: str, price: float, available) -> Level:
    stamp = available.to_pydatetime() if isinstance(available, pd.Timestamp) else available
    return Level(
        symbol=symbol,
        kind=kind,
        price=float(price),
        weight=1.0,
        as_of_ts=stamp,
        available_at=stamp,
    )
