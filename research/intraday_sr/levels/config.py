"""Research configuration. Not read by the live trading bot.

The Alpaca request budget lives here so it cannot be forgotten. This
package does not open a socket to Alpaca.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = _PACKAGE_ROOT / "config" / "research.yaml"


@dataclass(frozen=True)
class DataBudget:
    """Rolling per-minute cap for a future Alpaca bar pull.

    Peak: weekdays from ``peak_start_ct`` inclusive to ``peak_end_ct``
    exclusive, on the ``peak_tz`` clock. Everything else, including
    weekends and the instant ``peak_end_ct``, uses the off-hours cap.

    NYSE holidays that fall on a weekday are still treated as peak if
    they sit inside the clock window. This package does not consult the
    trading calendar.
    """

    peak_requests_per_min: int = 30
    offhours_requests_per_min: int = 60
    peak_start_ct: time = time(8, 15)
    peak_end_ct: time = time(15, 15)
    peak_tz: str = "America/Chicago"

    def __post_init__(self) -> None:
        if self.peak_requests_per_min < 1 or self.offhours_requests_per_min < 1:
            raise ValueError("request caps must be >= 1")
        if self.peak_requests_per_min > self.offhours_requests_per_min:
            raise ValueError("peak cap must not exceed the off-hours cap")

    def limit_per_min(self, now: datetime) -> int:
        """Cap in force at ``now``. Naive timestamps are treated as UTC."""
        tz = ZoneInfo(self.peak_tz)
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo("UTC"))
        local = now.astimezone(tz)
        if local.weekday() >= 5:
            return self.offhours_requests_per_min
        clock = local.time()
        if self.peak_start_ct <= clock < self.peak_end_ct:
            return self.peak_requests_per_min
        return self.offhours_requests_per_min


@dataclass(frozen=True)
class ScoreWeights:
    """Zone score weights. Components exposed on each ``Zone`` use these.

    ``touch = touch * touches``
    ``rejection = rejection * rejections``
    ``recency = recency * exp(-bars_since_last_touch / half_life_bars)``
    ``volume = volume * log1p(volume_at_level / volume_unit)``
    ``confluence = confluence * max(0, n_distinct_sources - 1)``
    ``total`` is the sum. ``bars_since_last_touch`` is counted on the zone
    timeframe. A zone that has never been touched uses bars since ``born_at``.
    """

    touch: float = 1.0
    rejection: float = 1.5
    recency: float = 1.0
    volume: float = 0.25
    confluence: float = 1.0
    half_life_bars: float = 78.0
    volume_unit: float = 100_000.0

    def __post_init__(self) -> None:
        if self.half_life_bars <= 0:
            raise ValueError("half_life_bars must be positive")
        if self.volume_unit <= 0:
            raise ValueError("volume_unit must be positive")


@dataclass(frozen=True)
class FormationConfig:
    """Tolerances for W / M / head-and-shoulders on 5m and 15m.

    Equality of troughs or peaks is ``atr_mult * ATR(14)`` on that
    formation's timeframe, computed at the pattern's ``known_at``.
    ``min_bars_between`` is the minimum spacing between adjacent points,
    in bars of that timeframe. ``max_bars_span`` bounds the whole pattern.
    """

    atr_mult: float = 0.5
    min_bars_between: int = 3
    max_bars_span: int = 80
    min_prominence_atr: float = 0.3
    min_head_prominence_atr: float = 0.25
    retest_bars: int = 10
    retest_tolerance_atr: float = 0.15
    timeframes: tuple[str, ...] = ("5m", "15m")

    def __post_init__(self) -> None:
        if self.min_bars_between < 1:
            raise ValueError("min_bars_between must be >= 1")
        if self.max_bars_span < self.min_bars_between:
            raise ValueError("max_bars_span must cover min_bars_between")
        if self.retest_bars < 1:
            raise ValueError("retest_bars must be >= 1")


@dataclass(frozen=True)
class LevelConfig:
    """Knobs for candidates, zones, and formations.

    ``left`` / ``right`` are the swing-pivot wing sizes. A pivot is not
    emitted until the ``right`` confirming bars have closed.
    ``or_minutes`` is the opening-range length (15 or 30 are the expected
    settings). ``cluster_atr`` is the single-linkage width in ATR units on
    ``zone_timeframe``.
    """

    left: int = 3
    right: int = 3
    pivot_timeframes: tuple[str, ...] = ("5m", "15m", "1h", "1d")
    or_minutes: int = 15
    zone_timeframe: str = "5m"
    atr_length: int = 14
    cluster_atr: float = 0.25
    min_score: float = 3.0
    top_n: int = 8
    max_return: int = 32
    profile_sessions: int = 20
    profile_percentile: float = 0.70
    profile_bin_size: float | None = None
    round_steps: tuple[float, ...] = (1.0, 5.0, 10.0)
    round_band_atr: float = 1.0
    include_prior_vwap: bool = True
    invalidate_atr: float = 1.0
    candidate_lookback_sessions: int = 20
    score: ScoreWeights = field(default_factory=ScoreWeights)
    formation: FormationConfig = field(default_factory=FormationConfig)
    budget: DataBudget = field(default_factory=DataBudget)
    universe: tuple[str, ...] = ()
    session_tz: str = "America/New_York"
    # Alpaca 16:00-labeled print is the closing auction, not a 5m bar.
    treat_1600_as_settlement: bool = True

    def __post_init__(self) -> None:
        if self.left < 1 or self.right < 1:
            raise ValueError("pivot wings must be >= 1 so confirmation is real")
        if self.or_minutes < 1:
            raise ValueError("or_minutes must be >= 1")
        if self.atr_length < 1:
            raise ValueError("atr_length must be >= 1")
        if self.cluster_atr <= 0:
            raise ValueError("cluster_atr must be positive")
        if not 0 < self.profile_percentile < 1:
            raise ValueError("profile_percentile must be in (0, 1)")
        if self.top_n < 1 or self.max_return < 1:
            raise ValueError("top_n and max_return must be >= 1")
        if not self.universe:
            object.__setattr__(self, "universe", load_universe())

    @classmethod
    def from_yaml(cls, path: str | Path | None = None) -> "LevelConfig":
        """Load universe and data budget. Engine knobs stay at dataclass defaults
        unless the yaml contains an ``engine`` mapping with the same field names.
        """
        raw = _read_yaml(path)
        budget_raw = raw.get("data_budget") or {}
        budget = DataBudget(
            peak_requests_per_min=int(budget_raw.get("peak_requests_per_min", 30)),
            offhours_requests_per_min=int(budget_raw.get("offhours_requests_per_min", 60)),
            peak_start_ct=_parse_hhmm(str(budget_raw.get("peak_start_ct", "08:15"))),
            peak_end_ct=_parse_hhmm(str(budget_raw.get("peak_end_ct", "15:15"))),
            peak_tz=str(budget_raw.get("peak_tz", "America/Chicago")),
        )
        universe = tuple(str(s) for s in raw.get("universe") or [])
        engine_raw = dict(raw.get("engine") or {})
        if "score" in engine_raw and isinstance(engine_raw["score"], dict):
            engine_raw["score"] = ScoreWeights(**engine_raw["score"])
        if "formation" in engine_raw and isinstance(engine_raw["formation"], dict):
            form = dict(engine_raw["formation"])
            if "timeframes" in form:
                form["timeframes"] = tuple(form["timeframes"])
            engine_raw["formation"] = FormationConfig(**form)
        if "round_steps" in engine_raw:
            engine_raw["round_steps"] = tuple(float(x) for x in engine_raw["round_steps"])
        if "pivot_timeframes" in engine_raw:
            engine_raw["pivot_timeframes"] = tuple(engine_raw["pivot_timeframes"])
        return cls(budget=budget, universe=universe, **engine_raw)


def load_universe(path: str | Path | None = None) -> tuple[str, ...]:
    """Symbol list from the research yaml. Trading may edit that file."""
    raw = _read_yaml(path)
    names = tuple(str(s).strip().upper() for s in raw.get("universe") or () if str(s).strip())
    if not names:
        raise ValueError("research universe is empty")
    return names


def load_budget(path: str | Path | None = None) -> DataBudget:
    return LevelConfig.from_yaml(path).budget


def _read_yaml(path: str | Path | None) -> dict:
    target = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with target.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{target} must be a mapping")
    return loaded


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))
