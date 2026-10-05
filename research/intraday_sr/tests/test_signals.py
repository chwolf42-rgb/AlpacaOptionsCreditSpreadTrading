"""Test A levels stay banded to the stamp that is being evaluated."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import importlib

import pandas as pd
import numpy as np

from research.intraday_sr.engine import levels_at, signals, signals_funnel
from research.intraday_sr.engine.levels import _levels_at_stamp, _pivot_tape
from research.intraday_sr.engine.signals import SignalFunnel, _build, _entry_prices, _step
from research.intraday_sr.engine.zone_cache import clear_zone_cache, level_cfg_token, zone_cfg_token
from research.intraday_sr.engine.zones import fast_zones
from research.intraday_sr.grids import TEST_A
from research.intraday_sr.tests.fixtures.synthetic import _concat, _frame_from_closes, _volumes, _weekdays, trend_bars
from research.intraday_sr.types import ET, BarSet, EngineCfg, SignalCfg, Zone


def _sig() -> SignalCfg:
    row = TEST_A[0]
    return SignalCfg(
        oscillator=row["oscillator"],
        rvol_min=row["rvol_min"],
        entry_tf=row["entry_tf"],
        target=row["target"],
        k_confirm=row["k_confirm"],
        variant_id=row["variant_id"],
        test="A",
    )


def _keys(levels) -> set[tuple[str, float]]:
    return {(level.kind, round(level.price, 5)) for level in levels}


def _march_to_june():
    """Price near 100 through 21 Mar 2024, then a drift that leaves those pivots behind."""
    days = _weekdays(date(2024, 2, 1), 100)
    frames = []
    clock = np.arange(78, dtype=np.float64)
    for index, day in enumerate(days):
        closes = 100.0 + np.sin(clock / 5.0)
        if day == date(2024, 3, 21):
            closes = closes.copy()
            closes[20] = 102.2
        elif day > date(2024, 3, 21):
            closes = closes + (day - date(2024, 3, 21)).days * 0.8
        frames.append(_frame_from_closes(day, closes, symbol="SPY", volumes=_volumes(78, index)))
    return _concat(frames)


def test_stamp_levels_match_a_truncated_levels_at():
    frame = trend_bars()
    cfg = EngineCfg()
    pivots, prices = _pivot_tape("TREND", frame, cfg)
    for offset in (16 * 78 + 36, 24 * 78 + 20, len(frame) - 1):
        stamp = frame["available_at"].iloc[offset].to_pydatetime()
        prefix = frame.iloc[: offset + 1]
        direct = levels_at(BarSet(prefix), stamp, cfg)
        stamped = _levels_at_stamp("TREND", prefix, pivots, prices, stamp, cfg)
        assert _keys(stamped) == _keys(direct)


def test_march_levels_stay_candidates_when_the_window_ends_in_june(monkeypatch):
    frame = _march_to_june()
    cfg = EngineCfg()
    march = frame[frame["session"] == date(2024, 3, 21)]
    stamp = march["available_at"].iloc[23].to_pydatetime()
    assert stamp.astimezone(ET).hour == 11 and stamp.astimezone(ET).minute == 30
    prefix = frame.loc[frame["available_at"] <= stamp].reset_index(drop=True)
    direct = levels_at(BarSet(prefix), stamp, cfg)
    march_pivots = [level for level in direct if level.kind == "pivot_5m" and level.price > 101.0]
    assert march_pivots
    pivot_price = max(level.price for level in march_pivots)

    june = frame["available_at"].iloc[-1].to_pydatetime()
    assert june.astimezone(ET).date() > date(2024, 3, 21) + timedelta(days=60)
    later = levels_at(BarSet(frame), june, cfg)
    assert all(abs(level.price - pivot_price) > 0.05 for level in later)

    seen: dict[str, set[tuple[str, float]]] = {}

    def spy(levels, **kwargs):
        when = kwargs["stamp"].astimezone(ET)
        if when.date() == date(2024, 3, 21) and when.hour == 11 and when.minute == 30:
            seen["keys"] = _keys(levels)
        return fast_zones(levels, **kwargs)

    signal_engine = importlib.import_module("research.intraday_sr.engine.signals")
    monkeypatch.setattr(signal_engine, "fast_zones", spy)
    start = frame["available_at"].iloc[0].to_pydatetime()
    list(signals(BarSet(frame), start, june, cfg, _sig()))
    assert seen["keys"] == _keys(direct)
    assert ("pivot_5m", round(pivot_price, 5)) in seen["keys"]


def test_zone_cfg_hash_includes_k_and_level_hash_does_not():
    assert zone_cfg_token(EngineCfg(k_zones=3)) != zone_cfg_token(EngineCfg(k_zones=5))
    assert level_cfg_token(EngineCfg(k_zones=3)) == level_cfg_token(EngineCfg(k_zones=5))


def test_warm_cache_reuses_levels_across_signal_axes(monkeypatch):
    clear_zone_cache()
    signal_engine = importlib.import_module("research.intraday_sr.engine.signals")
    calls = {"levels": 0, "zones": 0}
    real_levels = signal_engine._levels_at_stamp
    real_zones = signal_engine.fast_zones

    def count_levels(*args, **kwargs):
        calls["levels"] += 1
        return real_levels(*args, **kwargs)

    def count_zones(*args, **kwargs):
        calls["zones"] += 1
        return real_zones(*args, **kwargs)

    monkeypatch.setattr(signal_engine, "_levels_at_stamp", count_levels)
    monkeypatch.setattr(signal_engine, "fast_zones", count_zones)
    frame = trend_bars()
    bars = BarSet(frame)
    start = frame["available_at"].iloc[0].to_pydatetime()
    end = frame["available_at"].iloc[-1].to_pydatetime()
    cfg = EngineCfg(k_zones=3)
    primary = _sig()
    list(signals(bars, start, end, cfg, primary))
    levels_once = calls["levels"]
    zones_once = calls["zones"]
    assert levels_once > 0 and zones_once > 0
    other = SignalCfg(
        oscillator=primary.oscillator,
        rvol_min=primary.rvol_min,
        entry_tf=primary.entry_tf,
        target="2R" if primary.target != "2R" else "1R",
        k_confirm=3,
        variant_id="cache-probe",
        test="A",
    )
    list(signals(bars, start, end, cfg, other))
    assert calls["levels"] == levels_once
    assert calls["zones"] == zones_once
    list(signals(bars, start, end, EngineCfg(k_zones=5), primary))
    assert calls["levels"] == levels_once
    assert calls["zones"] > zones_once


def test_funnel_counts_cover_touch_hold_arm_and_emit():
    clear_zone_cache()
    frame = trend_bars()
    bars = BarSet(frame)
    start = frame["available_at"].iloc[0].to_pydatetime()
    end = frame["available_at"].iloc[-1].to_pydatetime()
    cfg = EngineCfg(k_zones=3)
    sig = _sig()
    funnel = signals_funnel(bars, start, end, cfg, sig)
    assert funnel.emits == len(list(signals(bars, start, end, cfg, sig)))
    assert funnel.emits >= 1
    assert funnel.touches >= funnel.holds >= funnel.arms >= funnel.k_confirm_pass
    # A zone can be absent on the touch bar and present on the arm bar, so
    # the forward tally is not a ceiling on emits. This variant is 1R.
    assert funnel.build_fail_no_ahead_zone == 0
    assert funnel.build_fail_zone_lt_1R == 0


def _arm_zone(**overrides) -> Zone:
    stamp = datetime(2024, 6, 3, 10, 0, tzinfo=ET)
    payload = dict(
        symbol="SPY",
        low=99.0,
        high=99.4,
        side="support",
        score=0.5,
        components={"touches": 0.2},
        kinds=("pivot_5m",),
        as_of_ts=stamp,
        valid_from_ts=stamp,
        available_at=stamp,
        engine_cfg="cfg-a",
        tf="5m",
        atr_d=2.0,
    )
    payload.update(overrides)
    return Zone(**payload)


def _arm_cfg(target: str) -> SignalCfg:
    return SignalCfg(
        oscillator="rsi14_30_70",
        rvol_min=1.5,
        entry_tf="5m",
        target=target,
        k_confirm=0,
        variant_id=f"A-K3-rsi14_30_70-rvol1.5-5m-{target}-k0",
        test="A",
    )


def _arm_step(zones: list[Zone], target: str) -> tuple[list, SignalFunnel]:
    """One support touch that holds on bar 0 and arms on bar 1."""
    stamp = datetime(2024, 6, 3, 10, 5, tzinfo=ET)
    opens = np.array([99.3, 99.7], dtype=np.float64)
    highs = np.array([100.0, 99.9], dtype=np.float64)
    lows = np.array([99.2, 99.55], dtype=np.float64)
    closes = np.array([99.5, 99.7], dtype=np.float64)
    zeros = np.zeros(2, dtype=np.float64)
    funnel = SignalFunnel()
    found = _step(
        index=1,
        zones=zones,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        available=[stamp, stamp],
        osc=zeros,
        hist=zeros,
        macd_line=zeros,
        macd_signal=zeros,
        volume_ratio=zeros,
        factors=np.ones(2, dtype=np.float64),
        oversold=30.0,
        overbought=70.0,
        cfg=EngineCfg(),
        sig=_arm_cfg(target),
        width=timedelta(minutes=5),
        armed_until={},
        funnel=funnel,
    )
    return found, funnel


def test_fixed_r_emits_without_an_opposite_zone_and_zone_target_does_not():
    support = _arm_zone()
    near = _arm_zone(side="resistance", low=100.2, high=100.5)
    # 1R and 2R are measured from the stop. A missing opposite zone is not a skip.
    for target in ("1R", "2R"):
        found, funnel = _arm_step([support], target)
        assert len(found) == 1
        assert set(found[0].targets) == {"1R", "2R"}
        assert funnel.build_fail_no_ahead_zone == 0
        assert funnel.build_fail_zone_lt_1R == 0
        with_near, near_funnel = _arm_step([support, near], target)
        assert len(with_near) == 1
        assert with_near[0].targets["zone"] == near.low
        assert near_funnel.build_fail_no_ahead_zone == 0
        assert near_funnel.build_fail_zone_lt_1R == 0

    missing, missing_funnel = _arm_step([support], "zone")
    assert missing == []
    assert missing_funnel.build_fail_no_ahead_zone == 1
    assert missing_funnel.build_fail_zone_lt_1R == 0

    close_zone, close_funnel = _arm_step([support, near], "zone")
    assert close_zone == []
    assert close_funnel.build_fail_no_ahead_zone == 0
    assert close_funnel.build_fail_zone_lt_1R == 1

    # Direct gate: room under 1R is a skip only for the zone target.
    stamp = datetime(2024, 6, 3, 10, 5, tzinfo=ET)
    highs = np.array([100.0, 99.9], dtype=np.float64)
    lows = np.array([99.2, 99.55], dtype=np.float64)
    closes = np.array([99.5, 99.7], dtype=np.float64)
    signal, reason = _build(
        support,
        0,
        1,
        highs,
        lows,
        closes,
        [stamp, stamp],
        np.ones(2),
        {"oscillator": 0.0, "macd": 0.0, "rvol": 0.0},
        [support, near],
        EngineCfg(),
        _arm_cfg("zone"),
        timedelta(minutes=5),
    )
    assert signal is None and reason == "zone_lt_1r"


def test_entry_prices_keep_the_unclamped_extremes():
    frame = pd.DataFrame(
        {"high": [100.0], "low": [100.0], "high_unclamped": [120.0], "low_unclamped": [80.0]}
    )
    priced = _entry_prices(frame)
    assert float(priced["high"].iloc[0]) == 120.0
    assert float(priced["low"].iloc[0]) == 80.0
