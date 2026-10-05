"""Test A levels stay banded to the stamp that is being evaluated."""

from __future__ import annotations

from datetime import date, timedelta
import importlib

import numpy as np

from research.intraday_sr.engine import levels_at, signals
from research.intraday_sr.engine.levels import _levels_at_stamp, _pivot_tape
from research.intraday_sr.engine.zones import fast_zones
from research.intraday_sr.grids import TEST_A
from research.intraday_sr.tests.fixtures.synthetic import _concat, _frame_from_closes, _volumes, _weekdays, trend_bars
from research.intraday_sr.types import ET, BarSet, EngineCfg, SignalCfg


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
