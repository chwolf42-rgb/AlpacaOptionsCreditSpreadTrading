"""Public value types for the intraday S/R research stack.

Every emitted object carries ``known_at``: the bar close at which that
version became knowable. A computation stamped at time T uses only bars
whose close is at or before T.

``docs/intraday-sr/LEVELS-INTERFACE.md`` is the contract. ``SPEC.md`` was
not in the tree when these names were chosen; they are meant to be easy
to rename.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


def freeze_meta(meta: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return an immutable copy of a metadata mapping."""
    if meta is None or len(meta) == 0:
        return MappingProxyType({})
    if isinstance(meta, MappingProxyType):
        return meta
    return MappingProxyType(dict(meta))


class LevelSource(str, Enum):
    """Where a candidate price came from."""

    PIVOT_HIGH = "pivot_high"
    PIVOT_LOW = "pivot_low"
    PRIOR_DAY_HIGH = "prior_day_high"
    PRIOR_DAY_LOW = "prior_day_low"
    PRIOR_DAY_CLOSE = "prior_day_close"
    PREMARKET_HIGH = "premarket_high"
    PREMARKET_LOW = "premarket_low"
    OVERNIGHT_HIGH = "overnight_high"
    OVERNIGHT_LOW = "overnight_low"
    OR_HIGH = "or_high"
    OR_LOW = "or_low"
    HVN = "hvn"
    VP_SHELF = "vp_shelf"
    VWAP = "vwap"
    ROUND_NUMBER = "round_number"


class ZoneSide(str, Enum):
    SUPPORT = "support"
    RESISTANCE = "resistance"
    BOTH = "both"


class FormationKind(str, Enum):
    DOUBLE_BOTTOM = "double_bottom"
    DOUBLE_TOP = "double_top"
    INVERSE_HS = "inverse_hs"
    HS = "hs"


class FormationSide(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class FormationEventKind(str, Enum):
    NECKLINE_BREAK = "neckline_break"
    NECKLINE_RETEST = "neckline_retest"
    INVALIDATED = "invalidated"


class BarTimeConvention(str, Enum):
    """How to read a cached timestamp column.

    ``START`` is Alpaca's convention: the stamp is the left edge of the bar.
    The loader adds the bar width and the index becomes the bar close.
    ``END`` means the stamp is already the bar close.
    """

    START = "start"
    END = "end"


@dataclass(frozen=True)
class LevelCandidate:
    """One price level that was knowable at ``known_at``.

    ``ref_time`` is the bar close the price refers to (the pivot bar, the
    bar that printed a session high, the session close, or the bar that
    first made a round number relevant). ``known_at`` is the bar close at
    which the candidate may be used. Invariant: ``known_at >= ref_time``.

    A swing pivot's ``ref_time`` is the pivot bar. Its ``known_at`` is the
    close of the last right-hand confirming bar, which is later than
    ``ref_time`` whenever ``right >= 1``.

    ``provisional`` is only for a live opening range whose window has not
    ended. The final opening-range candidate is a second object with
    ``provisional=False``.
    """

    symbol: str
    price: float
    source: LevelSource
    timeframe: str
    ref_time: datetime
    known_at: datetime
    meta: Mapping[str, Any] = field(default_factory=dict)
    provisional: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "meta", freeze_meta(self.meta))
        object.__setattr__(self, "price", float(self.price))


@dataclass(frozen=True)
class ScoreComponents:
    """Weighted pieces of a zone score. ``total`` is their sum.

    See ``ScoreWeights`` in ``config.py`` for the formula. The harness can
    retune weights without re-deriving touches or rejections.
    """

    touch: float
    rejection: float
    recency: float
    volume: float
    confluence: float
    total: float

    def as_dict(self) -> dict[str, float]:
        return {
            "touch": self.touch,
            "rejection": self.rejection,
            "recency": self.recency,
            "volume": self.volume,
            "confluence": self.confluence,
            "total": self.total,
        }


@dataclass(frozen=True)
class SideFlip:
    """One entry in a zone's side history. ``at`` is a bar close."""

    side: ZoneSide
    at: datetime


@dataclass(frozen=True)
class Zone:
    """A cluster of candidates, versioned by ``known_at``.

    ``id`` is stable across versions as the cluster evolves. ``born_at`` is
    the close of the bar that first published this id. ``known_at`` is the
    close of the bar that produced this version. Later touches, score
    changes, and role flips publish a new version with the same id and a
    later ``known_at``.

    ``last_touch`` is the same timestamp as ``last_touch_at`` (the original
    field name, kept for the first interface cut).

    ``broken_at`` is the first close through the zone against its
    then-current side. That break flips support to resistance and resistance
    to support. ``flipped_at`` is the most recent flip. ``side_history``
    starts with the side at ``born_at`` and appends each change.
    ``invalidated_at`` is set when a close finishes more than
    ``invalidate_atr`` ATRs beyond the zone; later ``levels_asof`` calls
    omit the zone.
    """

    id: str
    symbol: str
    low: float
    high: float
    mid: float
    side: ZoneSide
    score: float
    components: ScoreComponents
    touches: int
    rejections: int
    last_touch_at: datetime | None
    volume_at_level: float
    sources: tuple[LevelSource, ...]
    known_at: datetime
    timeframe: str
    atr_at_known: float
    broken_at: datetime | None
    invalidated_at: datetime | None
    flipped_at: datetime | None
    side_history: tuple[SideFlip, ...]
    born_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "low", float(self.low))
        object.__setattr__(self, "high", float(self.high))
        object.__setattr__(self, "mid", float(self.mid))
        object.__setattr__(self, "score", float(self.score))
        object.__setattr__(self, "volume_at_level", float(self.volume_at_level))
        object.__setattr__(self, "atr_at_known", float(self.atr_at_known))
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "side_history", tuple(self.side_history))

    @property
    def last_touch(self) -> datetime | None:
        """Alias of ``last_touch_at``."""
        return self.last_touch_at


@dataclass(frozen=True)
class NearestZones:
    """Zones whose mids sit strictly below and above a price.

    A zone that contains the price is neither target. ``below`` is the
    greatest mid still under the price. ``above`` is the least mid still
    over the price.
    """

    below: Zone | None
    above: Zone | None


@dataclass(frozen=True)
class FormationPoint:
    """A confirmed swing used by a formation.

    ``bar_time`` is the pivot bar's close (``ref_time``). ``known_at`` is
    the close of that pivot's right-hand confirmation bar.
    """

    price: float
    bar_time: datetime
    known_at: datetime
    kind: str  # "high" or "low"

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", float(self.price))
        if self.kind not in ("high", "low"):
            raise ValueError("FormationPoint.kind must be 'high' or 'low'")


@dataclass(frozen=True)
class Neckline:
    """A horizontal or sloped neckline.

    ``price_at(t) = anchor_price + slope_per_second * (t - anchor_time)``.
    A horizontal neckline has ``slope_per_second == 0``. Slope is price
    units per SI second, not per bar, so 5m and 15m share the same meaning.
    """

    anchor_price: float
    anchor_time: datetime
    slope_per_second: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "anchor_price", float(self.anchor_price))
        object.__setattr__(self, "slope_per_second", float(self.slope_per_second))

    def price_at(self, t: datetime) -> float:
        delta = (t - self.anchor_time).total_seconds()
        return self.anchor_price + self.slope_per_second * delta


@dataclass(frozen=True)
class Formation:
    """A completed reversal structure built only from confirmed pivots.

    ``known_at`` is the confirming close of the last pivot in the pattern
    (the second trough, the second peak, or the right shoulder). Points
    earlier in the pattern were already confirmed by then.

    ``extreme`` is the head, the lower double-bottom trough, or the higher
    double-top peak. It is the stop reference. ``measured_move_target`` is
    the classic measured move, fixed at ``known_at`` from the neckline and
    the extreme. It does not wait for the break.

    ``zone_ref`` is the id of the nearest zone at ``known_at``, or None.
    """

    id: str
    symbol: str
    kind: FormationKind
    side: FormationSide
    timeframe: str
    points: tuple[FormationPoint, ...]
    neckline: Neckline
    zone_ref: str | None
    known_at: datetime
    extreme: FormationPoint
    measured_move_target: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", tuple(self.points))
        object.__setattr__(self, "measured_move_target", float(self.measured_move_target))

    @property
    def slope(self) -> float:
        return self.neckline.slope_per_second

    @property
    def anchor_price(self) -> float:
        return self.neckline.anchor_price

    @property
    def anchor_time(self) -> datetime:
        return self.neckline.anchor_time

    def neckline_price_at(self, t: datetime) -> float:
        """Neckline price at bar close ``t``. Uses only the stored line."""
        return self.neckline.price_at(t)


@dataclass(frozen=True)
class FormationEvent:
    """A neckline break, retest, or invalidation.

    ``bar_time`` is the event bar's close. ``known_at`` equals ``bar_time``:
    the event is knowable when that bar closes. ``price`` is that bar's
    close. ``level_crossed`` is ``neckline_price_at(bar_time)``.

    On ``neckline_retest``, ``retest_extreme`` is the retest bar's low for
    a bullish formation and the retest bar's high for a bearish one. It is
    None for breaks and invalidations.
    """

    kind: FormationEventKind
    formation_id: str
    price: float
    bar_time: datetime
    known_at: datetime
    level_crossed: float
    retest_extreme: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", float(self.price))
        object.__setattr__(self, "level_crossed", float(self.level_crossed))
        if self.retest_extreme is not None:
            object.__setattr__(self, "retest_extreme", float(self.retest_extreme))


@dataclass(frozen=True)
class LevelStudy:
    """Everything a symbol run emitted, in deterministic order.

    ``zones`` contains every published version, not only the latest. Filter
    with ``asof`` before comparing a truncated run to a full run.
    """

    symbol: str
    candidates: tuple[LevelCandidate, ...]
    zones: tuple[Zone, ...]
    formations: tuple[Formation, ...]
    events: tuple[FormationEvent, ...]

    def asof(self, t: datetime) -> "LevelStudy":
        """Objects whose ``known_at`` is at or before ``t``."""
        return LevelStudy(
            symbol=self.symbol,
            candidates=tuple(c for c in self.candidates if c.known_at <= t),
            zones=tuple(z for z in self.zones if z.known_at <= t),
            formations=tuple(f for f in self.formations if f.known_at <= t),
            events=tuple(e for e in self.events if e.known_at <= t),
        )
