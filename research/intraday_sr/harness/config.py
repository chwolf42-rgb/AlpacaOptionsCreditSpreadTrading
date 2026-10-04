"""Harness-side configuration dataclasses (proposed for types.py at CP0; kept here to avoid S0 collisions).

Values are SPEC v1.0 section 5/7 plus the v1.1 rulings (12 entries/day, 4 concurrent). Changing any default
requires a new spec version before the run that uses it.
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


# SPEC v1.3 G1 primary guardrail. Read from grids.PRIMARY_GUARDRAIL (Developer 2: dict
# {max_losses_day: 2, max_losses_week: 5}; a (2, 5) tuple is accepted too) when grids.py is importable, and
# asserted equal to the spec value. The daily stop is SIGNED like grids.DAILY_LOSS_STOP (-0.015).
SPEC_PRIMARY_GUARDRAIL = {"max_losses_day": 2, "max_losses_week": 5}
SPEC_DAILY_LOSS_STOP = -0.015


def guardrail_from(g: Any) -> dict:
    if isinstance(g, dict):
        return {"max_losses_day": g["max_losses_day"], "max_losses_week": g["max_losses_week"]}
    if isinstance(g, (tuple, list)) and len(g) == 2:
        return {"max_losses_day": g[0], "max_losses_week": g[1]}
    raise ValueError(f"PRIMARY_GUARDRAIL must be a dict with max_losses_day/max_losses_week, got {g!r}")


def primary_from_grids(mod=None) -> tuple[dict, float, str]:
    """(guardrail dict, daily loss stop, source). Asserts grids.py agrees with SPEC v1.3."""
    if mod is None:
        try:
            import importlib
            mod = importlib.import_module("research.intraday_sr.grids")
        except Exception:  # noqa: BLE001  (S0 not merged yet)
            return dict(SPEC_PRIMARY_GUARDRAIL), SPEC_DAILY_LOSS_STOP, "spec (grids.py not importable)"
    src = "spec"
    g = dict(SPEC_PRIMARY_GUARDRAIL)
    if hasattr(mod, "PRIMARY_GUARDRAIL"):
        g, src = guardrail_from(mod.PRIMARY_GUARDRAIL), "grids.PRIMARY_GUARDRAIL"
        if g != SPEC_PRIMARY_GUARDRAIL:
            raise AssertionError(f"grids.PRIMARY_GUARDRAIL {g} != SPEC v1.3 G1 {SPEC_PRIMARY_GUARDRAIL}")
    stop = float(getattr(mod, "DAILY_LOSS_STOP", SPEC_DAILY_LOSS_STOP))
    if stop != SPEC_DAILY_LOSS_STOP:
        raise AssertionError(f"grids.DAILY_LOSS_STOP {stop} != {SPEC_DAILY_LOSS_STOP} (signed, of day-start equity)")
    return g, stop, src


_PG, _DLS, PRIMARY_SOURCE = primary_from_grids()


@dataclass(frozen=True)
class RiskCfg:
    target: Literal["1R", "2R", "zone", "next_zone"] = "1R"   # canonical "zone"; "next_zone" = temporary alias
    risk_pct: float = 0.005                 # of day-start equity, per trade
    max_pos_notional_x: float = 1.0          # x equity per position
    max_gross_notional_x: float = 3.0        # x equity total
    max_concurrent: int = 4                  # v1.1 (v1.0: 3)
    max_entries_per_day: int = 12            # v1.1 (v1.0: 10)
    daily_loss_stop: float = _DLS            # SIGNED (-0.015): realized + open <= this x day-start equity -> flatten
    last_entry: time = time(15, 0)           # entry fills only on bars that OPEN before 15:00 ET
    forced_exit: time = time(15, 55)         # forced exit at the OPEN of the 15:55 5m bar
    forced_exit_early: time = time(12, 55)   # half days (13:00 close)
    min_stop_atr_d: float = 0.10             # stop widened to >= 0.10 ATR_d from the actual fill
    zone_target_min_r: float = 1.0           # zone target < 1R from the fill -> skip
    start_equity: float = 100_000.0
    capacity_release: Literal["next_bar"] = "next_bar"   # capacity freed by an exit on bar t is usable from t+1
    fill_bar_target: Literal["not_credited"] = "not_credited"  # on the entry bar only the stop is checked
    # SPEC v1.3 G1/G3: loss guardrail d2+w5 is the PRIMARY configuration and the default (grids.PRIMARY_GUARDRAIL).
    # None = no limit. A loss (R < 0 after costs) counts at its exit fill, INSIDE the bar: exits run before
    # entries in every bar, so a loss that reaches the limit blocks entries in that same bar (and a fill-bar stop
    # counts before the next entry candidate of that bar).
    max_losses_day: Optional[int] = _PG["max_losses_day"]    # 2: no new entries for the rest of the session
    max_losses_week: Optional[int] = _PG["max_losses_week"]  # 5: ... and for the rest of the Mon-Fri ET week

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


PRIMARY = GuardrailCfg("d2+w5", _PG["max_losses_day"], _PG["max_losses_week"])
assert (PRIMARY.max_losses_day, PRIMARY.max_losses_week) == (2, 5)
PRIMARY_LABEL = PRIMARY.name
COMPARISON = (GuardrailCfg("none", None, None), GuardrailCfg("d2+w6", 2, 6))
EXTRA_COMPARISON = (GuardrailCfg("d2", 2, None), GuardrailCfg("w5", None, 5), GuardrailCfg("w6", None, 6))  # cheap, table only
GUARDRAILS = (PRIMARY,) + COMPARISON


def guardrail_label(risk: "RiskCfg") -> str:
    for g in GUARDRAILS + EXTRA_COMPARISON:
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


# SPEC v1.3 G2: declared trial counts (guardrail configs add 0). DSR uses N_TOTAL (see readout notes).
N_DECLARED = {"A": 192, "B": 192, "F": 48, "options": 18}
N_TOTAL = sum(N_DECLARED.values())          # 450
