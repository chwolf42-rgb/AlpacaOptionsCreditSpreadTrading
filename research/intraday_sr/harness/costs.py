"""Equity cost model (SPEC section 7). Costs are dollars per fill, computed on the AS-TRADED price.

per-side rate  = tier bp (T0 1.0 SPY/QQQ/IWM, T1 2.0 if prior-60-session median $vol >= $3B, T2 3.5), min $0.01/share
stop / forced  = 2x the per-side cost
limit target   = no slippage/half-spread (fills only if traded through by $0.01)
reg fee        = 0.3 bp of SELL notional (long exits, short entries)
`mult` scales the per-side part (1.5x sensitivity row); the reg fee is not scaled.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.intraday_sr.harness.config import CostCfg

ENTRY, TARGET, STOP, FORCED = "entry", "target", "stop", "forced"


def tier(symbol: str, median_dollar_volume: float, cfg: CostCfg = CostCfg()) -> str:
    if symbol in cfg.t0_symbols:
        return "T0"
    if np.isfinite(median_dollar_volume) and median_dollar_volume >= cfg.t1_min_dollar_volume:
        return "T1"
    return "T2"


def tier_bp(t: str, cfg: CostCfg = CostCfg()) -> float:
    return {"T0": cfg.t0_bp, "T1": cfg.t1_bp, "T2": cfg.t2_bp}[t]


def fill_cost(kind: str, side: int, qty: float, price_adj: float, adj_factor: float, tier_name: str,
              cfg: CostCfg = CostCfg()) -> float:
    """Dollar cost of one fill.

    kind: entry | target | stop | forced.  side: +1 buy, -1 sell (the fill's own direction).
    qty is in ADJUSTED shares; as-traded shares = qty / adj_factor, as-traded price = price_adj * adj_factor,
    so notional is identical in both units.
    """
    raw_px = price_adj * adj_factor
    raw_qty = qty / adj_factor
    notional = raw_qty * raw_px
    per_share = max(tier_bp(tier_name, cfg) * 1e-4 * raw_px, cfg.min_per_share)
    if kind == TARGET:
        per_side = 0.0
    elif kind in (STOP, FORCED):
        per_side = cfg.stop_forced_mult * per_share * raw_qty
    elif kind == ENTRY:
        per_side = per_share * raw_qty
    else:
        raise ValueError(kind)
    reg = cfg.reg_fee_bp_sell * 1e-4 * notional if side < 0 else 0.0
    return per_side * cfg.mult + reg


def prior_median_dollar_volume(daily: pd.DataFrame, lookback: int = 60) -> pd.Series:
    """PIT tier input: median of (close x volume, as-traded) over the `lookback` sessions BEFORE each session.

    daily: index = session date, columns close (adjusted), volume (adjusted shares), adj_factor.
    Adjusted close x adjusted volume equals as-traded dollar volume, so adj_factor is not needed here.
    """
    dv = daily["close"].astype(float) * daily["volume"].astype(float)
    return dv.shift(1).rolling(lookback, min_periods=lookback).median()
