"""Shared dataclasses from spec §3.

Field names and meanings match ``docs/intraday-sr/SPEC.md`` (v1.2).
Harness config classes (``RiskCfg``, ``CostCfg``, ``Fold``) are Developer 1's
and are not defined here.

S0 additions, pending CP0, are the fields and the helper marked below.
Each added field has a default so a §3 constructor still works.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Literal, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

TZ = "America/New_York"
ET = ZoneInfo(TZ)

Side = Literal["support", "resistance"]
BarTF = Literal["5m", "15m", "1h", "1d"]
EntryTF = Literal["5m", "15m"]
Direction = Literal[1, -1]
TestName = Literal["A", "B", "F_W", "F_IHS", "F_M", "F_HS"]
FormationKind = Literal["W", "IHS", "M", "HS"]
TargetName = Literal["1R", "2R", "next_zone"]
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
) -> str:
    """Stable id: sha256 of symbol, side, midpoint rounded half-up to 1e-4, engine_cfg.

    S0 addition, pending CP0.
    """
    mid = (float(low) + float(high)) / 2.0
    rounded = round_half_up(mid, 4)
    payload = f"{symbol}|{side}|{rounded:.4f}|{engine_cfg}"
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
    # S0 additions, pending CP0.
    zone_id: str = ""
    tf: str = ""
    atr_d: float = float("nan")

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
        if self.zone_id == "":
            object.__setattr__(
                self,
                "zone_id",
                zone_id_for(self.symbol, self.side, self.low, self.high, self.engine_cfg),
            )


def _extreme_from_pivots(
    kind: str,
    pivots: Sequence[tuple[datetime, float]],
    fallback_ts: datetime,
) -> tuple[datetime, float]:
    if not pivots:
        return fallback_ts, float("nan")
    # W / IHS stop reference is the lowest trough. M / HS is the highest peak.
    if kind in ("W", "IHS"):
        ts, price = min(pivots, key=lambda item: (item[1], item[0]))
    else:
        ts, price = max(pivots, key=lambda item: (item[1], item[0]))
    return ts, float(price)


def formation_id_for(
    kind: str,
    symbol: str,
    tf: str,
    confirmed_ts: datetime,
    pivots: Sequence[tuple[datetime, float]],
) -> str:
    """S0 addition, pending CP0. Identity of a confirmed pattern, not its later break."""
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
    # S0 additions, pending CP0.
    formation_id: str = ""
    extreme: tuple[datetime, float] | None = None
    measured_move: float = float("nan")

    def __post_init__(self) -> None:
        pivots = tuple((as_et(ts, "Formation.pivots.ts"), float(price)) for ts, price in self.pivots)
        object.__setattr__(self, "pivots", pivots)
        object.__setattr__(self, "neckline", (float(self.neckline[0]), float(self.neckline[1])))
        object.__setattr__(self, "invalidation", float(self.invalidation))
        object.__setattr__(self, "break_ts", as_et(self.break_ts, "Formation.break_ts"))
        if self.retest_ts is not None:
            object.__setattr__(self, "retest_ts", as_et(self.retest_ts, "Formation.retest_ts"))
        confirmed = as_et(self.confirmed_ts, "Formation.confirmed_ts")
        object.__setattr__(self, "confirmed_ts", confirmed)
        object.__setattr__(self, "as_of_ts", as_et(self.as_of_ts, "Formation.as_of_ts"))
        object.__setattr__(self, "available_at", as_et(self.available_at, "Formation.available_at"))
        extreme = self.extreme
        if extreme is None:
            extreme = _extreme_from_pivots(self.kind, pivots, confirmed)
        else:
            extreme = (as_et(extreme[0], "Formation.extreme.ts"), float(extreme[1]))
        object.__setattr__(self, "extreme", extreme)
        if self.formation_id == "":
            object.__setattr__(
                self,
                "formation_id",
                formation_id_for(self.kind, self.symbol, self.tf, confirmed, pivots),
            )
        measured = float(self.measured_move)
        if math.isnan(measured) and not math.isnan(extreme[1]):
            neck = float(self.neckline[0])
            # Pending CP0: projected from the neckline price at the break.
            if self.kind in ("W", "IHS"):
                measured = neck + (neck - extreme[1])
            else:
                measured = neck - (extreme[1] - neck)
        object.__setattr__(self, "measured_move", measured)


@dataclass(frozen=True)
class Signal:
    """One armed entry. ``confluence`` is an S0 addition, pending CP0.

    ``confluence`` is how many of the three optional conditions held
    (oscillator, MACD, RVOL), from 0 to 3. It is not the variant's
    ``k_confirm`` threshold. v1.1 A1: a variant requires at least
    ``k_confirm`` of those conditions; the hold and the re-confirmation
    stay mandatory.
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
        if self.confluence not in (0, 1, 2, 3):
            raise ValueError("Signal.confluence must be 0, 1, 2, or 3")
        object.__setattr__(self, "trigger", float(self.trigger))
        object.__setattr__(self, "stop", float(self.stop))
        object.__setattr__(self, "targets", freeze_map(self.targets))
        object.__setattr__(self, "components", freeze_map(self.components))
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

    frame: pd.DataFrame

    def visible(self, as_of: datetime) -> pd.DataFrame:
        as_of = as_et(as_of, "as_of")
        frame = self.frame
        if frame.empty:
            return frame.copy()
        return frame.loc[frame["available_at"] <= as_of].copy()


@dataclass(frozen=True)
class EngineCfg:
    """Engine constants implied by §4 and §5. ``k_cluster`` is fixed, not a grid axis.

    ``k_zones`` is the variant's K. Everything else is frozen.
    """

    k_zones: int = 5
    k_cluster: float = 0.25
    n_5m: int = 3
    n_15m: int = 3
    n_1h: int = 2
    n_1d: int = 2
    atr_length: int = 14
    profile_sessions: int = 5
    profile_percentile: float = 0.70
    profile_bin_atr: float = 0.05
    zone_pad_atr: float = 0.05
    candidate_band_atr: float = 2.0
    touch_sessions: int = 20
    recency_half_life_sessions: float = 5.0
    score_touches: float = 0.30
    score_rejections: float = 0.30
    score_recency: float = 0.20
    score_volume: float = 0.20

    def __post_init__(self) -> None:
        if self.k_cluster != 0.25:
            raise ValueError("k_cluster is fixed at 0.25 ATR_d (spec v1.1)")
        if self.k_zones not in (3, 5):
            raise ValueError("K must be 3 or 5")


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


def nearest_zones(zones: Sequence[Zone], price: float) -> tuple[Zone | None, Zone | None]:
    """Nearest published zone strictly below and above ``price``.

    S0 addition, pending CP0. Midpoint is ``(low + high) / 2``. A zone that
    contains ``price`` is not returned as either target. Ties break on
    ``zone_id`` so the choice is stable.
    """
    below: Zone | None = None
    above: Zone | None = None
    below_mid = float("-inf")
    above_mid = float("inf")
    for zone in zones:
        mid = (zone.low + zone.high) / 2.0
        if mid < price and (below is None or mid > below_mid or (mid == below_mid and zone.zone_id < below.zone_id)):
            below, below_mid = zone, mid
        elif mid > price and (above is None or mid < above_mid or (mid == above_mid and zone.zone_id < above.zone_id)):
            above, above_mid = zone, mid
    return below, above
