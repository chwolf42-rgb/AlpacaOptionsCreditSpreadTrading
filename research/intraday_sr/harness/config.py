"""Harness-side configuration dataclasses (RiskCfg, CostCfg, Fold, GuardrailCfg; Developer 1).

Frozen values are read from grids.py via harness/s0grids.py (SPEC v1.3.1 C3: single source): PRIMARY_GUARDRAIL,
COMPARISON_GUARDRAILS, FIXED (daily stop, entries/day, concurrency, ...), N. Changing any of them requires a new spec
version before the run that uses it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, time
from typing import Any, Literal, Optional

# Target labels. Canonical (Developer 2, 2026-10-04): "zone". "next_zone" is a temporary alias (S0 grids). Anything
# else is rejected; it is never silently treated as a zone target.
TARGETS = ("1R", "2R", "zone")
TARGET_ALIASES = {"next_zone": "zone"}


class UnknownTarget(ValueError):
    pass


def canonical_target(label: str) -> str:
    t = TARGET_ALIASES.get(label, label)
    if t not in TARGETS:
        raise UnknownTarget(f"unknown target label {label!r}; accepted: {TARGETS + tuple(TARGET_ALIASES)}")
    return t


# Frozen constants come from grids.py through the s0grids shim (SPEC v1.3.1 C3 / CP0 R5); nothing is redefined here.
from research.intraday_sr.harness import s0grids as _G  # noqa: E402

_PG_DAY, _PG_WEEK = _G.primary_guardrail()                 # grids.PRIMARY_GUARDRAIL {daily_losses, weekly_losses}
_DLS = float(_G.fixed("daily_loss_stop"))                  # signed, -0.015
_ENTRIES = int(_G.fixed("entries_per_day"))
_CONC = int(_G.fixed("max_concurrent"))

# Frozen harness constants that grids.FIXED does not carry YET (CP0 R5 asks Developer 2 to add them). Each is taken
# from grids.FIXED as soon as the key exists; until then the SPEC value is used and listed in PENDING_R5 (manifest).
# FIXED key aliases (grids.FIXED names on the left of the SPEC names the RiskCfg fields still use).
_FIXED_ALIASES = {"risk_pct": "risk_fraction", "max_pos_notional_x": "notional_cap_position",
                  "max_gross_notional_x": "notional_cap_total", "last_entry": "no_new_entries_after_et",
                  "forced_exit": "forced_exit_bar_open_et", "start_equity": "model_equity",
                  "min_stop_atr_d": "stop_floor_atr"}
_PENDING_SPEC = {"risk_pct": 0.005, "max_pos_notional_x": 1.0, "max_gross_notional_x": 3.0, "last_entry": "15:00",
                 "forced_exit": "15:55", "forced_exit_early": "12:55", "min_stop_atr_d": 0.10,
                 "zone_target_min_r": 1.0, "start_equity": 100_000.0}
PENDING_R5 = tuple(k for k in _PENDING_SPEC
                   if k not in _G.grids().FIXED and _FIXED_ALIASES.get(k, k) not in _G.grids().FIXED)


def _fx(key):
    f = _G.grids().FIXED
    v = f[key] if key in f else f.get(_FIXED_ALIASES.get(key, key), _PENDING_SPEC[key])
    return time.fromisoformat(v) if isinstance(v, str) and ":" in v else v


@dataclass(frozen=True)
class RiskCfg:
    target: Literal["1R", "2R", "zone", "next_zone"] = "1R"   # canonical "zone"; "next_zone" = temporary alias
    risk_pct: float = _fx("risk_pct")                    # of day-start equity, per trade
    max_pos_notional_x: float = _fx("max_pos_notional_x")  # x equity per position
    max_gross_notional_x: float = _fx("max_gross_notional_x")  # x equity total
    max_concurrent: int = _CONC              # grids.FIXED["max_concurrent"] (4)
    max_entries_per_day: int = _ENTRIES      # grids.FIXED["entries_per_day"] (12)
    daily_loss_stop: float = _DLS            # SIGNED (-0.015): realized + open <= this x day-start equity -> flatten
    last_entry: time = _fx("last_entry")     # entry fills only on bars that OPEN before 15:00 ET
    forced_exit: time = _fx("forced_exit")   # forced exit at the OPEN of the 15:55 5m bar
    forced_exit_early: time = _fx("forced_exit_early")   # half days (13:00 close)
    min_stop_atr_d: float = _fx("min_stop_atr_d")        # stop widened to >= 0.10 ATR_d from the actual fill
    zone_target_min_r: float = _fx("zone_target_min_r")  # zone target < 1R from the fill -> skip
    start_equity: float = _fx("start_equity")
    capacity_release: Literal["next_bar"] = "next_bar"   # capacity freed by an exit on bar t is usable from t+1
    fill_bar_target: Literal["not_credited"] = "not_credited"  # on the entry bar only the stop is checked
    # SPEC v1.3 G1/G3: loss guardrail d2+w5 is the PRIMARY configuration and the default (grids.PRIMARY_GUARDRAIL).
    # None = no limit. A loss (R < 0 after costs) counts at its exit fill, INSIDE the bar: exits run before
    # entries in every bar, so a loss that reaches the limit blocks entries in that same bar (and a fill-bar stop
    # counts before the next entry candidate of that bar).
    max_losses_day: Optional[int] = _PG_DAY    # grids.PRIMARY_GUARDRAIL["daily_losses"] (2)
    max_losses_week: Optional[int] = _PG_WEEK  # grids.PRIMARY_GUARDRAIL["weekly_losses"] (5)

    def __post_init__(self):
        canonical_target(self.target)
        assert self.daily_loss_stop < 0, "daily_loss_stop is signed like grids.DAILY_LOSS_STOP (e.g. -0.015)"


@dataclass(frozen=True)
class GuardrailCfg:
    """A named loss-guardrail setting (SPEC v1.3 G1-G3). Applied by copying into RiskCfg via `apply()`; the
    simulator reads RiskCfg only. PRIMARY (d2+w5) feeds every result, selection, freeze, pass bar and DSR.
    COMPARISON configs run after selection on the same picks and go to guardrail_compare.parquet only."""
    name: str
    max_losses_day: Optional[int]
    max_losses_week: Optional[int]

    def apply(self, risk: "RiskCfg") -> "RiskCfg":
        return replace(risk, max_losses_day=self.max_losses_day, max_losses_week=self.max_losses_week)


# Built from grids.PRIMARY_GUARDRAIL and grids.COMPARISON_GUARDRAILS (one source). v1.3 dropped d2/w5/w6.
PRIMARY = GuardrailCfg(f"d{_PG_DAY}+w{_PG_WEEK}", _PG_DAY, _PG_WEEK)
PRIMARY_LABEL = PRIMARY.name
COMPARISON = tuple(GuardrailCfg(n, d, w) for n, d, w in _G.comparison_guardrails())
GUARDRAILS = (PRIMARY,) + COMPARISON


def guardrail_label(risk: "RiskCfg") -> str:
    for g in GUARDRAILS:
        if (g.max_losses_day, g.max_losses_week) == (risk.max_losses_day, risk.max_losses_week):
            return g.name
    return f"d{risk.max_losses_day}+w{risk.max_losses_week}"


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


# SPEC v1.3 G2: declared trial counts from grids.py (guardrail configs add 0). DSR N = max(N_TOTAL, program ledger).
_g = _G.grids()
N_DECLARED = {"A": len(_g.TEST_A), "B": len(_g.TEST_B), "F": len(_g.FORMATIONS),
              "options": len(_g.OPTIONS) + len(_g.OPTIONS_0DTE)}
N_TOTAL = int(_g.N_TRIALS)
assert sum(N_DECLARED.values()) == N_TOTAL == 450
