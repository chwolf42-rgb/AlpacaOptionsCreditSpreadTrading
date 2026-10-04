"""Causal pivot levels.

This is the lookahead gate's level source. D2-2 replaces it with the full
candidate set (pivots on every timeframe, prior-day levels, opening range,
VWAP, HVN, round numbers). The gate already refuses bars that are not yet
closed.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from research.intraday_sr.data.badprint import prices_as_of
from research.intraday_sr.types import BarSet, EngineCfg, Level


def _strict_mask(values: np.ndarray, n: int, *, high: bool) -> np.ndarray:
    size = len(values)
    ok = np.ones(size, dtype=bool)
    if size < 2 * n + 1:
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


def levels_at(bars: BarSet, as_of: datetime, cfg: EngineCfg) -> list[Level]:
    """Strict 5m pivots whose confirming bar has already closed."""
    frame = prices_as_of(bars.visible(as_of), as_of)
    if frame.empty or "symbol" not in frame.columns:
        return []
    n = int(cfg.n_5m)
    found: list[Level] = []
    for symbol, group in frame.groupby("symbol", sort=True):
        group = group.sort_values("ts")
        if "tf" in group.columns:
            group = group.loc[group["tf"] == "5m"]
        if len(group) < 2 * n + 1:
            continue
        lows = group["low"].to_numpy(dtype=np.float64)
        highs = group["high"].to_numpy(dtype=np.float64)
        low_at = _strict_mask(lows, n, high=False)
        high_at = _strict_mask(highs, n, high=True)
        stamps = group["available_at"].tolist()
        for index in np.flatnonzero(low_at | high_at):
            confirm = int(index) + n
            available = stamps[confirm]
            if bool(low_at[index]):
                found.append(
                    Level(
                        symbol=str(symbol),
                        kind="pivot_5m",
                        price=float(lows[index]),
                        weight=1.0,
                        as_of_ts=available,
                        available_at=available,
                    )
                )
            if bool(high_at[index]):
                found.append(
                    Level(
                        symbol=str(symbol),
                        kind="pivot_5m",
                        price=float(highs[index]),
                        weight=1.0,
                        as_of_ts=available,
                        available_at=available,
                    )
                )
    return found
