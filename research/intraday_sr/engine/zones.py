"""Causal zones for the lookahead gate.

One support and one resistance, each a pad around the nearest strict 5m
pivot, once Wilder ATR_d through yesterday is defined. D2-2 replaces the
clustering and the score. ``atr_d`` uses only sessions before the session
that contains ``as_of``.
"""

from __future__ import annotations

import math
from datetime import date, datetime

import numpy as np
import pandas as pd

from research.intraday_sr.engine.levels import _strict_mask
from research.intraday_sr.types import BarSet, EngineCfg, Zone


def _session_day(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _atr_through_yesterday(frame: pd.DataFrame, as_of: datetime, length: int) -> float | None:
    today = as_of.date()
    daily: list[tuple[date, float, float, float]] = []
    for session, group in frame.groupby("session", sort=True):
        day = _session_day(session)
        if day >= today:
            continue
        ordered = group.sort_values("ts")
        daily.append(
            (
                day,
                float(ordered["high"].max()),
                float(ordered["low"].min()),
                float(ordered["close"].iloc[-1]),
            )
        )
    if len(daily) < length + 1:
        return None
    true_ranges: list[float] = []
    for index in range(1, len(daily)):
        _, high, low, close = daily[index]
        prev_close = daily[index - 1][3]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr = sum(true_ranges[:length]) / length
    for true_range in true_ranges[length:]:
        atr = (atr * (length - 1) + true_range) / length
    if not math.isfinite(atr) or atr <= 0.0:
        return None
    return float(atr)


def _cfg_id(cfg: EngineCfg) -> str:
    return f"K{cfg.k_zones}|kc{cfg.k_cluster}"


def _one_zone(
    *,
    symbol: str,
    center: float,
    side: str,
    atr: float,
    as_of: datetime,
    cfg: EngineCfg,
) -> Zone:
    half = 0.5 * float(cfg.zone_pad_atr) * atr
    return Zone(
        symbol=symbol,
        low=center - half,
        high=center + half,
        side=side,  # type: ignore[arg-type]
        score=1.0,
        components={"touches": 1.0, "rejections": 0.0, "recency": 1.0, "volume": 0.0},
        kinds=("pivot_5m",),
        as_of_ts=as_of,
        valid_from_ts=as_of,
        available_at=as_of,
        engine_cfg=_cfg_id(cfg),
        tf="5m",
        atr_d=atr,
    )


def zones_at(bars: BarSet, as_of: datetime, cfg: EngineCfg) -> list[Zone]:
    """Zones knowable at ``as_of``. Empty until ATR_d through yesterday exists."""
    frame = bars.visible(as_of)
    if frame.empty or "symbol" not in frame.columns:
        return []
    atr = _atr_through_yesterday(frame, as_of, int(cfg.atr_length))
    if atr is None:
        return []
    half = 0.5 * float(cfg.zone_pad_atr) * atr
    n = int(cfg.n_5m)
    zones: list[Zone] = []
    for symbol, group in frame.groupby("symbol", sort=True):
        group = group.sort_values("ts")
        if "tf" in group.columns:
            group = group.loc[group["tf"] == "5m"]
        if group.empty:
            continue
        last = float(group["close"].iloc[-1])
        lows = group["low"].to_numpy(dtype=np.float64)
        highs = group["high"].to_numpy(dtype=np.float64)
        support_prices = lows[_strict_mask(lows, n, high=False)]
        resistance_prices = highs[_strict_mask(highs, n, high=True)]
        below = [float(price) for price in support_prices if float(price) + half < last]
        above = [float(price) for price in resistance_prices if float(price) - half > last]
        if below:
            zones.append(
                _one_zone(symbol=str(symbol), center=max(below), side="support", atr=atr, as_of=as_of, cfg=cfg)
            )
        if above:
            zones.append(
                _one_zone(symbol=str(symbol), center=min(above), side="resistance", atr=atr, as_of=as_of, cfg=cfg)
            )
    return zones
