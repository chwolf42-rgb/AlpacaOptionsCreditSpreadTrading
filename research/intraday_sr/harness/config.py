"""Harness-side configuration dataclasses (proposed for types.py at CP0; kept here to avoid S0 collisions).

Values are SPEC v1.0 section 5/7 plus the v1.1 rulings (12 entries/day, 4 concurrent). Changing any default
requires a new spec version before the run that uses it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, time
from typing import Literal, Optional


@dataclass(frozen=True)
class RiskCfg:
    target: Literal["1R", "2R", "zone"] = "1R"
    risk_pct: float = 0.005                 # of day-start equity, per trade
    max_pos_notional_x: float = 1.0          # x equity per position
    max_gross_notional_x: float = 3.0        # x equity total
    max_concurrent: int = 4                  # v1.1 (v1.0: 3)
    max_entries_per_day: int = 12            # v1.1 (v1.0: 10)
    daily_loss_stop: float = 0.015           # realized + open, of day-start equity -> flatten, no more entries
    last_entry: time = time(15, 0)           # entry fills only on bars that OPEN before 15:00 ET
    forced_exit: time = time(15, 55)         # forced exit at the OPEN of the 15:55 5m bar
    forced_exit_early: time = time(12, 55)   # half days (13:00 close)
    min_stop_atr_d: float = 0.10             # stop widened to >= 0.10 ATR_d from the actual fill
    zone_target_min_r: float = 1.0           # zone target < 1R from the fill -> skip
    start_equity: float = 100_000.0
    capacity_release: Literal["next_bar"] = "next_bar"   # capacity freed by an exit on bar t is usable from t+1
    fill_bar_target: Literal["not_credited"] = "not_credited"  # on the entry bar only the stop is checked


@dataclass(frozen=True)
class GuardrailCfg:
    """Reporting overlays only (Architect): none, d2, w5, w6, d2+w5, d2+w6. They add no trials to N."""
    name: str = "none"
    daily_loss: Optional[float] = None       # realized + open day P&L <= -x of day-start equity -> flatten, stop the day
    weekly_loss: Optional[float] = None      # week-to-date P&L <= -x of week-start equity -> flatten, stop the week


GUARDRAILS = (
    GuardrailCfg("none"),
    GuardrailCfg("d2", daily_loss=0.02),
    GuardrailCfg("w5", weekly_loss=0.05),
    GuardrailCfg("w6", weekly_loss=0.06),
    GuardrailCfg("d2+w5", daily_loss=0.02, weekly_loss=0.05),
    GuardrailCfg("d2+w6", daily_loss=0.02, weekly_loss=0.06),
)


@dataclass(frozen=True)
class CostCfg:
    """SPEC section 7 equities (per side, as-traded price, $0 commission)."""
    t0_bp: float = 1.0                       # SPY, QQQ, IWM
    t1_bp: float = 2.0                       # prior-60-session median $ volume >= $3B/day
    t2_bp: float = 3.5                       # < $3B/day
    t1_min_dollar_volume: float = 3e9
    min_per_share: float = 0.01              # as-traded $
    stop_forced_mult: float = 2.0            # stop and forced exits pay 2x per-side cost
    target_through: float = 0.01             # limit target fills only if price trades through by $0.01 (as-traded)
    reg_fee_bp_sell: float = 0.3             # on sell notional
    mult: float = 1.0                        # sensitivity row: 1.5
    t0_symbols: tuple = ("SPY", "QQQ", "IWM")
    dv_lookback: int = 60


@dataclass(frozen=True)
class Fold:
    k: int
    train_start: date
    train_end: date                          # inclusive
    test_start: date
    test_end: date                           # inclusive
    trade_from: Optional[date] = None        # global warmup (2019-02-01)


def cfg_dict(obj) -> dict:
    d = asdict(obj)
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in d.items()}
