"""Shared dataclasses from spec §3 (v1.3.1).

Harness config classes (``RiskCfg``, ``CostCfg``, ``Fold``) are Developer 1's
and are not defined here. Frozen numbers live in ``grids.py``. ``EngineCfg``
defaults are those numbers.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Literal, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from research.intraday_sr import grids as _grids

TZ = "America/New_York"
ET = ZoneInfo(TZ)

Side = Literal["support", "resistance"]
BarTF = Literal["5m", "15m", "1h", "1d"]
EntryTF = Literal["5m", "15m"]
Direction = Literal[1, -1]
TestName = Literal["A", "B", "F_W", "F_IHS", "F_M", "F_HS"]
FormationKind = Literal["W", "IHS", "M", "HS"]
TargetName = Literal["1R", "2R", "zone"]
_OPTIONAL_FLAGS = ("oscillator", "macd", "rvol")
_TARGET_KEYS = frozenset({"1R", "2R", "zone"})
_ENTRY_WIDTH = {"5m": timedelta(minutes=5), "15m": timedelta(minutes=15)}
OscillatorName = Literal["rsi14_30_70", "stoch14_3_3_20_80"]


def as_et(ts: datetime, field_name: str) -> datetime:
    """Require a tz-aware timestamp and return it in America/New_York."""
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{field_name} must be tz-aware ({TZ})")
    return ts.astimezone(ET)


def freeze_map(mapping: Mapping[str, float] | None) -> Mapping[str, float]:
    if not mapping:
        return MappingProxyType({})
    if isinstance(mapping, MappingProxyType):
        return mapping
    return MappingProxyType({str(k): float(v) for k, v in mapping.items()})


def round_half_up(value: float, places: int) -> float:
    """Half-up rounding. Zone ids use 4 decimal places on the midpoint."""
    factor = 10**places
    scaled = float(value) * factor
    if scaled >= 0:
        return math.floor(scaled + 0.5) / factor
    return math.ceil(scaled - 0.5) / factor


def zone_id_for(
    symbol: str,
    side: str,
    low: float,
    high: float,
    engine_cfg: str,
    tf: str,
) -> str:
    """Stable id: sha256 of symbol, side, tf, midpoint rounded half-up to 1e-4, engine_cfg.

    ``tf`` is part of the identity so a 5m zone and a 15m zone cannot collide.
    """
    mid = (float(low) + float(high)) / 2.0
    rounded = round_half_up(mid, 4)
    payload = f"{symbol}|{side}|{tf}|{rounded:.4f}|{engine_cfg}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Bar:
    """One row of the bar frame. ``ts`` is the bar open; ``available_at`` is the close."""

    symbol: str
    tf: BarTF
    ts: datetime
    available_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float
    trades: int
    session: date
    adj_factor: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", as_et(self.ts, "Bar.ts"))
        object.__setattr__(self, "available_at", as_et(self.available_at, "Bar.available_at"))
        object.__setattr__(self, "open", float(self.open))
        object.__setattr__(self, "high", float(self.high))
        object.__setattr__(self, "low", float(self.low))
        object.__setattr__(self, "close", float(self.close))
        object.__setattr__(self, "volume", float(self.volume))
        object.__setattr__(self, "vwap", float(self.vwap))
        object.__setattr__(self, "trades", int(self.trades))
        object.__setattr__(self, "adj_factor", float(self.adj_factor))


@dataclass(frozen=True)
class Level:
    """One candidate before clustering."""

    symbol: str
    kind: str
    price: float
    weight: float
    as_of_ts: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", float(self.price))
        object.__setattr__(self, "weight", float(self.weight))
        object.__setattr__(self, "as_of_ts", as_et(self.as_of_ts, "Level.as_of_ts"))
        object.__setattr__(self, "available_at", as_et(self.available_at, "Level.available_at"))


@dataclass(frozen=True)
class Zone:
    """Support or resistance cluster, valid from ``available_at`` until the next recompute.

    ``valid_from_ts`` equals ``available_at`` (§3). ``side`` is relative to
    price at ``as_of_ts``. ``engine_cfg`` is the config hash.
    """

    symbol: str
    low: float
    high: float
    side: Side
    score: float
    components: Mapping[str, float]
    kinds: tuple[str, ...]
    as_of_ts: datetime
    valid_from_ts: datetime
    available_at: datetime
    engine_cfg: str
    tf: EntryTF
    atr_d: float
    zone_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "low", float(self.low))
        object.__setattr__(self, "high", float(self.high))
        object.__setattr__(self, "score", float(self.score))
        object.__setattr__(self, "components", freeze_map(self.components))
        object.__setattr__(self, "kinds", tuple(self.kinds))
        object.__setattr__(self, "as_of_ts", as_et(self.as_of_ts, "Zone.as_of_ts"))
        object.__setattr__(self, "valid_from_ts", as_et(self.valid_from_ts, "Zone.valid_from_ts"))
        object.__setattr__(self, "available_at", as_et(self.available_at, "Zone.available_at"))
        object.__setattr__(self, "atr_d", float(self.atr_d))
        if self.low > self.high:
            raise ValueError("Zone.low must be <= Zone.high")
        if self.tf not in ("5m", "15m"):
            raise ValueError("Zone.tf must be 5m or 15m")
        if not math.isfinite(self.atr_d) or self.atr_d <= 0.0:
            raise ValueError("Zone.atr_d must be finite and > 0")
        if self.valid_from_ts != self.available_at:
            raise ValueError("Zone.valid_from_ts must equal available_at")
        if self.as_of_ts > self.available_at:
            raise ValueError("Zone.as_of_ts must be <= available_at")
        expected = zone_id_for(self.symbol, self.side, self.low, self.high, self.engine_cfg, self.tf)
        if self.zone_id == "":
            object.__setattr__(self, "zone_id", expected)
        elif self.zone_id != expected:
            raise ValueError("Zone.zone_id does not match symbol, side, tf, midpoint, and engine_cfg")


def _extreme_from_pivots(
    kind: str,
    pivots: Sequence[tuple[datetime, float]],
) -> tuple[datetime, float]:
    """Pattern low (W, IHS) or pattern high (M, HS). Ties take the earliest pivot."""
    if not pivots:
        raise ValueError("Formation.extreme is derived from pivots")
    if kind in ("W", "IHS"):
        price = min(item[1] for item in pivots)
    elif kind in ("M", "HS"):
        price = max(item[1] for item in pivots)
    else:
        raise ValueError("Formation.kind is not W, IHS, M, or HS")
    ts = min(item[0] for item in pivots if item[1] == price)
    return ts, float(price)


def formation_id_for(
    kind: str,
    symbol: str,
    tf: str,
    confirmed_ts: datetime,
    pivots: Sequence[tuple[datetime, float]],
) -> str:
    """Identity of a confirmed pattern.

    The hash covers kind, symbol, timeframe, confirmation time, and the
    pivot timestamps and prices. It does not include the formation grid's
    ``pivot_tol_atr``: that tolerance is how the pattern was selected, not
    which pattern it is.
    """
    parts = [kind, symbol, tf, confirmed_ts.isoformat()]
    for ts, price in pivots:
        parts.append(f"{ts.isoformat()}@{price:.6f}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Formation:
    """W, inverse head-and-shoulders, M, or head-and-shoulders.

    ``neckline`` is ``(price at break_ts, slope per bar)``. ``break_ts`` is
    required by §3; ``retest_ts`` is None until a retest prints.
    """

    kind: FormationKind
    symbol: str
    tf: str
    pivots: tuple[tuple[datetime, float], ...]
    neckline: tuple[float, float]
    invalidation: float
    break_ts: datetime
    retest_ts: datetime | None
    confirmed_ts: datetime
    as_of_ts: datetime
    available_at: datetime
    zone_id: str | None
    formation_id: str = ""
    extreme: tuple[datetime, float] | None = None

    def __post_init__(self) -> None:
        if self.tf not in _ENTRY_WIDTH:
            raise ValueError("Formation.tf must be 5m or 15m")
        pivots = tuple((as_et(ts, "Formation.pivots.ts"), float(price)) for ts, price in self.pivots)
        object.__setattr__(self, "pivots", pivots)
        object.__setattr__(self, "neckline", (float(self.neckline[0]), float(self.neckline[1])))
        object.__setattr__(self, "invalidation", float(self.invalidation))
        object.__setattr__(self, "break_ts", as_et(self.break_ts, "Formation.break_ts"))
        retest = None if self.retest_ts is None else as_et(self.retest_ts, "Formation.retest_ts")
        object.__setattr__(self, "retest_ts", retest)
        confirmed = as_et(self.confirmed_ts, "Formation.confirmed_ts")
        object.__setattr__(self, "confirmed_ts", confirmed)
        object.__setattr__(self, "as_of_ts", as_et(self.as_of_ts, "Formation.as_of_ts"))
        object.__setattr__(self, "available_at", as_et(self.available_at, "Formation.available_at"))
        derived = _extreme_from_pivots(self.kind, pivots)
        if self.extreme is not None:
            given_ts = as_et(self.extreme[0], "Formation.extreme.ts")
            given_px = float(self.extreme[1])
            if given_ts != derived[0] or given_px != derived[1]:
                raise ValueError("Formation.extreme is derived from pivots")
        object.__setattr__(self, "extreme", derived)
        lag = _grids.PIVOT_N[self.tf] * _ENTRY_WIDTH[self.tf]
        last_pivot = max(ts for ts, _ in pivots)
        if confirmed < last_pivot + lag:
            raise ValueError("Formation.confirmed_ts is before the last pivot is knowable")
        if not (confirmed <= self.break_ts <= self.available_at):
            raise ValueError("Formation requires confirmed_ts <= break_ts <= available_at")
        if retest is not None and retest <= self.break_ts:
            raise ValueError("Formation.retest_ts must be after break_ts")
        if derived[0] > confirmed or derived[0] not in {ts for ts, _ in pivots}:
            raise ValueError("Formation.extreme must be a pivot at or before confirmation")
        if self.formation_id == "":
            object.__setattr__(
                self,
                "formation_id",
                formation_id_for(self.kind, self.symbol, self.tf, confirmed, pivots),
            )


@dataclass(frozen=True)
class Signal:
    """One armed entry.

    ``trigger``, ``stop``, and ``targets`` are adjusted prices.
    ``as_traded = adjusted * Bar.adj_factor``, where ``Bar.adj_factor`` is
    raw/adjusted. ``targets`` keys are exactly ``1R``, ``2R``, and ``zone``.

    ``confluence`` is how many of the three optional conditions held
    (oscillator, MACD, RVOL), from 0 to 3. It equals the count of those
    flags in ``components``. It is not a score and it is not ``k_confirm``.
    Ties stay on zone score, then symbol.
    """

    symbol: str
    tf: EntryTF
    direction: Direction
    test: TestName
    zone: Zone
    formation: Formation | None
    trigger: float
    stop: float
    targets: Mapping[str, float]
    expires_at: datetime
    components: Mapping[str, float]
    as_of_ts: datetime
    available_at: datetime
    variant_id: str
    confluence: int = 0

    def __post_init__(self) -> None:
        if self.direction not in (1, -1):
            raise ValueError("Signal.direction must be 1 or -1")
        if self.tf not in ("5m", "15m"):
            raise ValueError("Signal.tf must be 5m or 15m")
        if self.test not in ("A", "B", "F_W", "F_IHS", "F_M", "F_HS"):
            raise ValueError("Signal.test is not a known test name")
        if self.confluence not in (0, 1, 2, 3):
            raise ValueError("Signal.confluence must be 0, 1, 2, or 3")
        if not math.isfinite(float(self.zone.atr_d)) or float(self.zone.atr_d) <= 0.0:
            raise ValueError("Signal.zone.atr_d must be finite and > 0")
        object.__setattr__(self, "trigger", float(self.trigger))
        object.__setattr__(self, "stop", float(self.stop))
        object.__setattr__(self, "targets", freeze_map(self.targets))
        object.__setattr__(self, "components", freeze_map(self.components))
        if set(self.targets) != _TARGET_KEYS:
            raise ValueError("Signal.targets keys must be exactly 1R, 2R, and zone")
        flag_count = sum(1 for name in _OPTIONAL_FLAGS if float(self.components.get(name, 0.0)) != 0.0)
        if flag_count != self.confluence:
            raise ValueError("Signal.confluence must equal the optional-condition count")
        object.__setattr__(self, "expires_at", as_et(self.expires_at, "Signal.expires_at"))
        object.__setattr__(self, "as_of_ts", as_et(self.as_of_ts, "Signal.as_of_ts"))
        object.__setattr__(self, "available_at", as_et(self.available_at, "Signal.available_at"))


@dataclass(frozen=True)
class Fill:
    symbol: str
    ts: datetime
    price: float
    qty: float
    side: int
    reason: str
    cost: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", as_et(self.ts, "Fill.ts"))
        object.__setattr__(self, "price", float(self.price))
        object.__setattr__(self, "qty", float(self.qty))
        object.__setattr__(self, "side", int(self.side))
        object.__setattr__(self, "cost", float(self.cost))


@dataclass(frozen=True)
class Trade:
    signal: Signal
    entry: Fill
    exit: Fill
    r: float
    pnl: float
    fold: int
    variant_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "r", float(self.r))
        object.__setattr__(self, "pnl", float(self.pnl))
        object.__setattr__(self, "fold", int(self.fold))


@dataclass
class BarSet:
    """Bars visible to the engine. Callers pass the tape they have.

    ``visible(as_of)`` is the only slice engine code may read: rows whose
    ``available_at`` is at or before ``as_of``.
    """

    _frame: pd.DataFrame

    def visible(self, as_of: datetime) -> pd.DataFrame:
        """Rows with ``available_at <= as_of``.

        ``available_at`` must be sorted ascending. The slice is a prefix
        from ``searchsorted``.
        """
        as_of = as_et(as_of, "as_of")
        frame = self._frame
        if frame.empty:
            return frame.iloc[0:0]
        cutoff = pd.Timestamp(as_of)
        index = int(frame["available_at"].searchsorted(cutoff, side="right"))
        return frame.iloc[:index]


@dataclass(frozen=True)
class EngineCfg:
    """Engine constants. ``k_zones`` is the variant's K. Every other field is frozen in ``grids.py``."""

    k_zones: int = 5
    k_cluster: float = _grids.K_CLUSTER
    n_5m: int = _grids.PIVOT_N["5m"]
    n_15m: int = _grids.PIVOT_N["15m"]
    n_1h: int = _grids.PIVOT_N["1h"]
    n_1d: int = _grids.PIVOT_N["1d"]
    atr_length: int = _grids.ATR_LENGTH
    profile_sessions: int = _grids.PROFILE_SESSIONS
    profile_percentile: float = _grids.PROFILE_PERCENTILE
    profile_bin_atr: float = _grids.PROFILE_BIN_ATR
    zone_pad_atr: float = _grids.ZONE_PAD_ATR
    candidate_band_atr: float = _grids.CANDIDATE_BAND_ATR
    touch_sessions: int = _grids.TOUCH_SESSIONS
    recency_half_life_sessions: float = _grids.RECENCY_HALF_LIFE_SESSIONS
    score_touches: float = _grids.SCORE_WEIGHTS["touches"]
    score_rejections: float = _grids.SCORE_WEIGHTS["rejections"]
    score_recency: float = _grids.SCORE_WEIGHTS["recency"]
    score_volume: float = _grids.SCORE_WEIGHTS["volume"]
    opening_range_start_et: str = _grids.OPENING_RANGE_START_ET
    opening_range_end_et: str = _grids.OPENING_RANGE_END_ET
    warmup_date: str = _grids.WARMUP_DATE
    dev_start: str = _grids.DEV_START
    dev_end: str = _grids.DEV_END
    holdout_start: str = _grids.HOLDOUT_START
    holdout_end: str = _grids.HOLDOUT_END
    no_new_entries_after_et: str = _grids.NO_NEW_ENTRIES_AFTER_ET
    forced_exit_bar_open_et: str = _grids.FORCED_EXIT_BAR_OPEN_ET
    stop_buffer_atr: float = _grids.STOP_BUFFER_ATR
    stop_floor_atr: float = _grids.STOP_FLOOR_ATR
    arm_atr: float = _grids.ARM_ATR
    cancel_bars: int = _grids.CANCEL_BARS
    entry_offset: float = _grids.ENTRY_OFFSET
    risk_fraction: float = _grids.RISK_FRACTION
    notional_cap_position: float = _grids.NOTIONAL_CAP_POSITION
    notional_cap_total: float = _grids.NOTIONAL_CAP_TOTAL
    model_equity: int = _grids.MODEL_EQUITY
    max_entries_per_day: int = _grids.MAX_ENTRIES_PER_DAY
    max_concurrent: int = _grids.MAX_CONCURRENT
    max_per_symbol: int = _grids.MAX_PER_SYMBOL
    daily_loss_stop: float = _grids.DAILY_LOSS_STOP
    max_losses_day: int = _grids.PRIMARY_GUARDRAIL["daily_losses"]
    max_losses_week: int = _grids.PRIMARY_GUARDRAIL["weekly_losses"]
    rvol_sessions: int = _grids.RVOL_SESSIONS
    rsi_length: int = _grids.RSI_LENGTH
    stoch_k: int = _grids.STOCH_K
    stoch_d: int = _grids.STOCH_D
    stoch_smooth: int = _grids.STOCH_SMOOTH
    macd_fast: int = _grids.MACD_FAST
    macd_slow: int = _grids.MACD_SLOW
    macd_signal: int = _grids.MACD_SIGNAL
    indicator_warmup_bars: int = _grids.INDICATOR_WARMUP_BARS
    round_step_under_50: float = _grids.ROUND_STEP_UNDER_50
    round_step_under_250: float = _grids.ROUND_STEP_UNDER_250
    round_step_under_1000: float = _grids.ROUND_STEP_UNDER_1000
    round_step_else: float = _grids.ROUND_STEP_ELSE
    bootstrap_seed: int = _grids.BOOTSTRAP_SEED
    bootstrap_resamples: int = _grids.BOOTSTRAP_RESAMPLES
    touch_window_bars: int = _grids.TOUCH_WINDOW_BARS
    formation_pivot_gap_min: int = _grids.FORMATION_PIVOT_GAP_MIN
    formation_pivot_gap_max: int = _grids.FORMATION_PIVOT_GAP_MAX
    shoulder_atr: float = _grids.SHOULDER_ATR
    hvn_rejection_wick: float = _grids.HVN_REJECTION_WICK

    def __post_init__(self) -> None:
        if self.k_zones not in (3, 5):
            raise ValueError("K must be 3 or 5")
        for name, value in _grids.frozen_engine_values().items():
            if getattr(self, name) != value:
                raise ValueError(f"{name} is frozen in grids.py")


@dataclass(frozen=True)
class SignalCfg:
    """One equity-grid variant. ``k_confirm`` is the minimum optional-condition count."""

    oscillator: OscillatorName
    rvol_min: float
    entry_tf: EntryTF
    target: TargetName
    k_confirm: int
    variant_id: str
    test: TestName = "A"

    def __post_init__(self) -> None:
        if self.k_confirm not in (0, 1, 2, 3):
            raise ValueError("k_confirm must be 0, 1, 2, or 3")
        object.__setattr__(self, "rvol_min", float(self.rvol_min))


def nearest_zones_at(
    zones: Sequence[Zone],
    symbol: str,
    price: float,
    as_of: datetime,
) -> tuple[Zone | None, Zone | None]:
    """Nearest support below ``price`` and resistance above it, as of ``as_of``.

    Keeps zones for ``symbol`` with ``available_at <= as_of``, then only the
    latest recompute (``as_of_ts`` equal to the max that is still ``<= as_of``).
    A zone that contains ``price`` is excluded. Support is used only when the
    zone sits entirely below ``price``; resistance only when it sits entirely
    above. Ties break on ``zone_id``.
    """
    as_of = as_et(as_of, "as_of")
    eligible = [
        zone
        for zone in zones
        if zone.symbol == symbol and zone.available_at <= as_of and zone.as_of_ts <= as_of
    ]
    if not eligible:
        return None, None
    latest = max(zone.as_of_ts for zone in eligible)
    current = [zone for zone in eligible if zone.as_of_ts == latest]
    below: Zone | None = None
    above: Zone | None = None
    for zone in current:
        if zone.low <= price <= zone.high:
            continue
        if zone.side == "support" and zone.high < price:
            if below is None or zone.high > below.high or (zone.high == below.high and zone.zone_id < below.zone_id):
                below = zone
        elif zone.side == "resistance" and zone.low > price:
            if above is None or zone.low < above.low or (zone.low == above.low and zone.zone_id < above.zone_id):
                above = zone
    return below, above
