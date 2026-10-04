"""Level clocks (spec §4.3–4.8 and §5)."""

from __future__ import annotations

from datetime import date, datetime, time

import numpy as np

from research.intraday_sr.engine.levels import levels_at
from research.intraday_sr.engine.signals import _adjusted_offset
from research.intraday_sr.tests.fixtures.synthetic import _concat, _frame_from_closes, _volumes, _weekdays
from research.intraday_sr.types import ET, BarSet, EngineCfg


def _bars(sessions: int, closes_for) -> tuple:
    days = _weekdays(date(2024, 6, 3), sessions)
    frames = []
    for index, day in enumerate(days):
        closes = closes_for(index)
        frames.append(
            _frame_from_closes(
                day,
                closes,
                symbol="SPY",
                volumes=_volumes(len(closes), index),
                extra_high_at=tuple(range(len(closes))) if index == 2 and sessions >= 5 else (),
            )
        )
    frame = _concat(frames)
    return days, frame


def _flat(index: int) -> np.ndarray:
    closes = np.full(78, 100.0)
    if index % 2 == 0:
        closes[30] = 100.15
        closes[50] = 99.85
    return closes


def _kinds_at(frame, stamp: datetime) -> set[str]:
    levels = levels_at(BarSet(frame), stamp, EngineCfg())
    return {level.kind for level in levels}


def _prices(frame, stamp: datetime, kind: str) -> list[float]:
    levels = levels_at(BarSet(frame), stamp, EngineCfg())
    return sorted(level.price for level in levels if level.kind == kind)


def test_prior_day_opens_at_0930_and_the_opening_range_at_1000():
    days, frame = _bars(2, lambda index: np.full(78, 100.0 + index))
    day1 = frame[frame["session"] == days[0]]
    day2 = frame[frame["session"] == days[1]]
    at_prior_close = day1["available_at"].iloc[-1].to_pydatetime()
    assert "pdh" not in _kinds_at(frame, at_prior_close)
    first_close = day2["available_at"].iloc[0].to_pydatetime()
    assert first_close.astimezone(ET).time() == time(9, 35)
    assert {"pdh", "pdl", "pdc"} <= _kinds_at(frame, first_close)
    assert _prices(frame, first_close, "pdh") == [float(day1["high"].max())]
    assert _prices(frame, first_close, "pdc") == [float(day1["close"].iloc[-1])]
    assert "orh" not in _kinds_at(frame, first_close)
    at_ten = day2["available_at"].iloc[5].to_pydatetime()
    assert at_ten.astimezone(ET).time() == time(10, 0)
    window = day2[day2["ts"] < at_ten]
    assert _prices(frame, at_ten, "orh") == [float(window["high"].max())]
    assert _prices(frame, at_ten, "orl") == [float(window["low"].min())]
    # Five minutes earlier the 10:00 bar has not closed.
    before = day2["available_at"].iloc[4].to_pydatetime()
    assert "orh" not in _kinds_at(frame, before)


def test_pivots_wait_for_the_right_hand_bars():
    days, frame = _bars(6, _flat)
    # 5m peak at bar 30 of day 0. N=3, so it is knowable at bar 33.
    day0 = frame[frame["session"] == days[0]].reset_index(drop=True)
    peak = float(day0["high"].iloc[30])
    hidden = day0["available_at"].iloc[32].to_pydatetime()
    shown = day0["available_at"].iloc[33].to_pydatetime()
    assert peak not in _prices(frame, hidden, "pivot_5m")
    assert peak in _prices(frame, shown, "pivot_5m")

    # Day index 2 is the strict daily high (every bar's high is lifted).
    # N_1d = 2, so that daily pivot is knowable at the close of day index 4.
    spike = frame[frame["session"] == days[2]]
    daily_high = float(spike["high"].max())
    before_daily = frame[frame["session"] == days[3]]["available_at"].iloc[-1].to_pydatetime()
    at_daily = frame[frame["session"] == days[4]]["available_at"].iloc[-1].to_pydatetime()
    assert daily_high not in _prices(frame, before_daily, "pivot_1d")
    assert daily_high in _prices(frame, at_daily, "pivot_1d")


def test_vwap_is_the_session_running_sum():
    _days, frame = _bars(1, lambda _index: np.linspace(100.0, 101.0, 78))
    third = frame["available_at"].iloc[2].to_pydatetime()
    head = frame.iloc[:3]
    expected = float((head["vwap"] * head["volume"]).sum() / head["volume"].sum())
    assert _prices(frame, third, "vwap") == [expected]


def test_candidate_kinds_once_atr_exists():
    def closes_for(index: int) -> np.ndarray:
        t = np.arange(78)
        return 100.0 + 0.02 * index + 0.08 * np.sin(t / 3.0)

    _days, frame = _bars(20, closes_for)
    as_of = frame["available_at"].iloc[-1].to_pydatetime()
    kinds = _kinds_at(frame, as_of)
    expected = {
        "pivot_5m",
        "pivot_15m",
        "pivot_1h",
        "pdh",
        "pdl",
        "pdc",
        "orh",
        "orl",
        "hvn",
        "vwap",
        "round",
    }
    assert expected <= kinds


def test_entry_tick_is_scaled_into_adjusted_prices():
    cfg = EngineCfg()
    assert _adjusted_offset(cfg, 1.0) == 0.01
    assert _adjusted_offset(cfg, 4.0) == 0.01 / 4.0
    assert _adjusted_offset(cfg, float("nan")) == 0.01
