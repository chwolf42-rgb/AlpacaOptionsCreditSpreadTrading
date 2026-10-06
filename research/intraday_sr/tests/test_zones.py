"""SPEC v1.3.3 zone locks: lookback, max width, and straddle split."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from research.intraday_sr.data.badprint import prices_as_of
from research.intraday_sr.engine.levels import _atr_from_segments, _segments, levels_at
from research.intraday_sr.engine.tape import floor_15m, session_day
from research.intraday_sr.engine.zones import (
    _pad_zone,
    _split_max_width,
    fast_zones,
    zones_at,
)
from research.intraday_sr.grids import GRID_SHA256, MAX_ZONE_WIDTH_ATR, MIN_CLEARANCE_ATR
from research.intraday_sr.tests.fixtures import trend_bars
from research.intraday_sr.types import ET, BarSet, EngineCfg, Level


def _level(kind: str, price: float, when: datetime) -> Level:
    return Level(symbol="SPY", kind=kind, price=price, weight=1.0, as_of_ts=when, available_at=when)


def _bars(n: int = 6) -> dict:
    return {
        "lows": np.full(n, 50.0),
        "highs": np.full(n, 50.2),
        "opens": np.full(n, 50.1),
        "closes": np.full(n, 50.1),
        "volume": np.ones(n),
        "ages": np.zeros(n),
    }


def _zones(levels: list[Level], *, last_close: float, atr: float, cfg: EngineCfg, **extra) -> list:
    stamp = datetime(2024, 6, 28, 11, 0, tzinfo=ET)
    return fast_zones(
        levels,
        symbol="SPY",
        last_close=last_close,
        atr=atr,
        stamp=stamp,
        cfg=cfg,
        **_bars(),
        **extra,
    )


def _supports(zones) -> list:
    return [zone for zone in zones if zone.side == "support"]


def _resistances(zones) -> list:
    return [zone for zone in zones if zone.side == "resistance"]


def test_max_zone_width_is_fixed_and_the_grid_hash_is_unchanged():
    assert MAX_ZONE_WIDTH_ATR == 1.0
    assert MIN_CLEARANCE_ATR == 0.10
    assert EngineCfg.max_zone_width_atr == 1.0
    assert EngineCfg.min_clearance_atr == 0.10
    assert GRID_SHA256 == "2ef95123c015d70a1751f559f21fa1249d0b08aea272319e232f2facb120aa22"


def test_wide_cluster_splits_at_the_largest_gap_and_recurses():
    """A chained cluster wider than 1 ATR splits at its largest gap, then again."""
    left = np.array([0.0, 0.25, 0.5, 0.75, 1.0, 1.25])
    right = np.array([1.75, 2.0, 2.25, 2.5, 2.75, 3.0])
    prices = np.concatenate([left, right])
    parts = _split_max_width(list(range(len(prices))), prices, 1.0)
    spans = [float(prices[part].max() - prices[part].min()) for part in parts]
    assert spans == [0.0, 1.0, 0.0, 1.0]
    assert float(prices[parts[0]][0]) == 0.0
    assert float(prices[parts[1]][0]) == 0.25
    assert float(prices[parts[2]][0]) == 1.75
    assert float(prices[parts[3]][0]) == 2.0

    tied = np.array([0.0, 1.0, 2.0])
    tied_parts = _split_max_width(list(range(3)), tied, 1.5)
    assert [float(tied[index]) for index in tied_parts[0]] == [0.0]
    assert [float(tied[index]) for index in tied_parts[1]] == [1.0, 2.0]

    # The same chain, run through fast_zones, is more than one zone and none
    # is wider than 1 ATR before the 0.05 pad (pad can add that and no more
    # when the raw span is already at the cap).
    stamp = datetime(2024, 6, 28, 11, 0, tzinfo=ET)
    levels = [_level("hvn", 8.0 + 0.2 * i, stamp) for i in range(8)]
    zones = _zones(levels, last_close=10.0, atr=1.0, cfg=EngineCfg(k_zones=5))
    assert len(zones) >= 2
    assert all(zone.high - zone.low <= 1.0 + 1e-9 for zone in zones)
    assert all(zone.high < 10.0 for zone in zones)


def test_straddle_keeps_support_and_resistance():
    stamp = datetime(2024, 6, 28, 11, 0, tzinfo=ET)
    levels = [
        _level("hvn", 99.0, stamp),
        _level("vwap", 100.0, stamp),
        _level("round", 101.0, stamp),
    ]
    zones = _zones(levels, last_close=100.0, atr=4.0, cfg=EngineCfg(k_zones=5))
    assert len(_supports(zones)) == 1
    assert len(_resistances(zones)) == 1
    support = _supports(zones)[0]
    resistance = _resistances(zones)[0]
    assert support.high < 100.0
    assert resistance.low >= 100.0
    assert len(zones) == 2


def test_padding_does_not_cross_last_close():
    """The price-side edge stays put when a centered pad would cross last close."""
    low, high = _pad_zone(99.99, 99.99, 100.0, 0.5)
    assert high < 100.0
    assert high == 99.99
    assert high - low >= 0.5 - 1e-9
    low, high = _pad_zone(100.0, 100.0, 100.0, 0.5)
    assert low >= 100.0
    assert low == 100.0
    assert high - low >= 0.5 - 1e-9

    # A member outside the clearance band still pads without crossing.
    stamp = datetime(2024, 6, 28, 11, 0, tzinfo=ET)
    support = _zones([_level("hvn", 98.5, stamp)], last_close=100.0, atr=10.0, cfg=EngineCfg(k_zones=3))
    assert len(support) == 1
    assert support[0].high < 100.0
    assert 100.0 - support[0].high >= 1.0 - 1e-9


def test_old_pivot_is_excluded_and_a_recent_one_is_kept():
    today = date(2024, 6, 28)
    origin = today - timedelta(days=28)
    old_when = datetime(2024, 5, 1, 10, 0, tzinfo=ET)
    now = datetime(2024, 6, 28, 11, 0, tzinfo=ET)
    assert session_day(old_when) < origin
    levels = [
        _level("pivot_5m", 80.0, old_when),
        _level("pdh", 85.0, old_when),
        _level("pivot_15m", 97.0, now),
    ]
    zones = _zones(
        levels,
        last_close=100.0,
        atr=20.0,
        cfg=EngineCfg(k_zones=5),
        pivot_not_before=origin,
    )
    mids = [0.5 * (zone.low + zone.high) for zone in zones]
    assert all(abs(mid - 80.0) > 1.0 for mid in mids)
    assert any(abs(mid - 85.0) < 1.0 for mid in mids)
    assert any(abs(mid - 97.0) < 1.0 for mid in mids)
    assert any("pdh" in zone.kinds for zone in zones)
    assert any("pivot_15m" in zone.kinds for zone in zones)
    assert all("pivot_5m" not in zone.kinds for zone in zones)


def test_clearance_trims_near_members_and_drops_a_zone_left_too_close():
    """Members inside 0.10 ATR are removed. A pad that leaves the edge closer is dropped."""
    stamp = datetime(2024, 6, 28, 11, 0, tzinfo=ET)
    atr = 10.0
    inside = _zones([_level("hvn", 99.5, stamp)], last_close=100.0, atr=atr, cfg=EngineCfg(k_zones=3))
    assert inside == []

    # Point at 99 survives the member trim (distance 1.0 ATR) and the centered
    # pad then pulls the edge to 0.75 ATR, which is inside the band.
    too_close = _zones([_level("hvn", 99.0, stamp)], last_close=100.0, atr=atr, cfg=EngineCfg(k_zones=3))
    assert too_close == []

    kept = _zones([_level("hvn", 98.5, stamp)], last_close=100.0, atr=atr, cfg=EngineCfg(k_zones=3))
    assert len(kept) == 1
    assert kept[0].side == "support"
    assert 100.0 - kept[0].high >= 0.10 * atr - 1e-9

    # Far enough that the centered pad still leaves a 0.10 ATR gap.
    on_edge = _zones([_level("round", 101.5, stamp)], last_close=100.0, atr=atr, cfg=EngineCfg(k_zones=3))
    assert len(on_edge) == 1
    assert on_edge[0].side == "resistance"
    assert on_edge[0].low - 100.0 >= 0.10 * atr - 1e-9
    inside_resistance = _zones(
        [_level("round", 100.5, stamp)], last_close=100.0, atr=atr, cfg=EngineCfg(k_zones=3)
    )
    assert inside_resistance == []


def test_k3_and_k5_differ_when_a_side_has_more_than_five_zones():
    stamp = datetime(2024, 6, 28, 11, 0, tzinfo=ET)
    prices = [96.5 + 0.6 * i for i in range(6)]
    levels = [_level("hvn", price, stamp) for price in prices]
    k3 = _supports(_zones(levels, last_close=100.0, atr=2.0, cfg=EngineCfg(k_zones=3)))
    k5 = _supports(_zones(levels, last_close=100.0, atr=2.0, cfg=EngineCfg(k_zones=5)))
    assert len(k5) > 5 or len(k3) == 3 and len(k5) == 5
    assert len(k3) == 3
    assert len(k5) == 5
    assert {zone.zone_id for zone in k3} != {zone.zone_id for zone in k5}


def test_fast_zones_match_zones_at_across_fixture_stamps():
    frame = trend_bars()
    bars = BarSet(frame)
    cfg = EngineCfg(k_zones=3)
    stamps: list[datetime] = []
    seen: set[datetime] = set()
    for value, session in zip(frame["available_at"], frame["session"]):
        stamp = value.to_pydatetime() if isinstance(value, pd.Timestamp) else value
        recompute = floor_15m(stamp, session_day(session))
        if recompute is None or recompute in seen:
            continue
        seen.add(recompute)
        stamps.append(recompute)
    sample = stamps[:: max(1, len(stamps) // 12)]
    assert len(sample) >= 8
    for stamp in sample:
        reference = zones_at(bars, stamp, cfg)
        visible = prices_as_of(bars.visible(stamp), stamp)
        prefix = visible.loc[visible["available_at"] <= stamp].reset_index(drop=True)
        if prefix.empty:
            assert reference == []
            continue
        group = prefix.sort_values("ts")
        current = session_day(group["session"].iloc[-1])
        sessions = [session_day(value) for value in group["session"]]
        unique_days = sorted(set(sessions))
        prior = [day for day in unique_days if day < current][-int(cfg.touch_sessions) :]
        allowed = set(prior)
        allowed.add(current)
        mask = np.array([day in allowed for day in sessions])
        window = group.loc[mask]
        day_index = {day: index for index, day in enumerate(unique_days)}
        ages = np.array(
            [day_index[current] - day_index[session_day(day)] for day in window["session"]],
            dtype=np.float64,
        )
        atr = _atr_from_segments(_segments(group), stamp, int(cfg.atr_length))
        own = levels_at(BarSet(prefix), stamp, cfg)
        direct = []
        if atr is not None and not window.empty:
            direct = fast_zones(
                own,
                symbol="TREND",
                lows=window["low"].to_numpy(dtype=np.float64),
                highs=window["high"].to_numpy(dtype=np.float64),
                opens=window["open"].to_numpy(dtype=np.float64),
                closes=window["close"].to_numpy(dtype=np.float64),
                volume=window["volume"].to_numpy(dtype=np.float64),
                ages=ages,
                last_close=float(window["close"].iloc[-1]),
                atr=float(atr),
                stamp=stamp,
                cfg=cfg,
                pivot_not_before=prior[0] if prior else current,
            )

        def key(zone):
            return (zone.side, zone.low, zone.high, zone.score, zone.kinds, zone.zone_id)

        assert sorted(direct, key=key) == sorted(reference, key=key)
        last_close = float(window["close"].iloc[-1]) if not window.empty else None
        for zone in reference:
            gap = (last_close - zone.high) if zone.side == "support" else (zone.low - last_close)
            assert gap >= MIN_CLEARANCE_ATR * zone.atr_d - 1e-9
