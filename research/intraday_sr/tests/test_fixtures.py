"""Synthetic fixtures: long paths, strict pivots, and both 2024 DST switches."""

from __future__ import annotations

from datetime import date, time, timedelta

import numpy as np

from research.intraday_sr.data.resample import resample
from research.intraday_sr.types import ET
from research.intraday_sr.tests.fixtures import (
    dst_fall_bars,
    dst_spring_bars,
    early_close_bars,
    gap_bars,
    ihs_bars,
    range_bars,
    thanksgiving_early_bars,
    trend_bars,
    w_bars,
)


def _sessions(frame):
    return list(dict.fromkeys(frame["session"].tolist()))


def test_each_fixture_is_rth_and_long_enough_for_lookahead_samples():
    for frame in (
        trend_bars(),
        range_bars(),
        w_bars(),
        ihs_bars(),
        gap_bars(),
        early_close_bars(),
    ):
        assert len(frame) >= 200
        assert frame["ts"].dt.tz is not None
        opens = frame["ts"].dt.tz_convert(ET)
        assert (opens.dt.time >= time(9, 30)).all()
        assert (opens.dt.time <= time(15, 55)).all()
        delta = frame["available_at"] - frame["ts"]
        assert (delta == pd_timedelta_five()).all()


def pd_timedelta_five():
    import pandas as pd

    return pd.Timedelta(minutes=5)


def test_early_close_session_ends_at_1300():
    frame = early_close_bars()
    early = frame[frame["session"] == date(2024, 7, 3)]
    assert len(early) == 42
    last_open = early["ts"].iloc[-1].tz_convert(ET)
    last_close = early["available_at"].iloc[-1].tz_convert(ET)
    assert last_open.hour == 12 and last_open.minute == 55
    assert last_close.hour == 13 and last_close.minute == 0
    full = frame[frame["session"] == date(2024, 7, 2)]
    assert len(full) == 78
    assert full["ts"].iloc[-1].tz_convert(ET).hour == 15
    assert full["ts"].iloc[-1].tz_convert(ET).minute == 55


def test_gap_opens_away_from_the_prior_close():
    frame = gap_bars()
    sessions = _sessions(frame)
    first = frame[frame["session"] == sessions[0]]
    second = frame[frame["session"] == sessions[1]]
    assert float(second["open"].iloc[0]) == float(first["close"].iloc[-1]) + 4.0


def _strict_lows(lows: np.ndarray, n: int = 3) -> list[int]:
    found = []
    for index in range(n, len(lows) - n):
        if lows[index] < lows[index - n : index].min() and lows[index] < lows[index + 1 : index + n + 1].min():
            found.append(index)
    return found


def test_long_fixtures_have_thirty_sessions_and_varying_volume():
    for frame in (trend_bars(), range_bars(), w_bars(), ihs_bars(), gap_bars(), early_close_bars()):
        assert frame["session"].nunique() >= 30
        assert frame["volume"].nunique() > 1


def test_w_and_ihs_strict_pivots():
    w = w_bars()
    day = w[w["session"] == w["session"].iloc[0]]
    lows = _strict_lows(day["low"].to_numpy(dtype=np.float64))
    assert 10 in lows
    assert 50 in lows
    ihs = ihs_bars()
    day = ihs[ihs["session"] == ihs["session"].iloc[0]]
    lows = _strict_lows(day["low"].to_numpy(dtype=np.float64))
    assert 12 in lows
    assert 32 in lows
    assert 52 in lows


def _opens_at_0930(frame) -> None:
    opens = frame["ts"].dt.tz_convert(ET)
    first = frame[frame["session"] == frame["session"].iloc[0]]
    assert first["ts"].iloc[0].astimezone(ET).hour == 9
    assert first["ts"].iloc[0].astimezone(ET).minute == 30
    assert (opens.dt.hour * 60 + opens.dt.minute >= 9 * 60 + 30).all()


def test_dst_weeks_and_the_november_early_close():
    spring = dst_spring_bars()
    fall = dst_fall_bars()
    early = thanksgiving_early_bars()
    _opens_at_0930(spring)
    _opens_at_0930(fall)
    _opens_at_0930(early)
    march_8 = spring[spring["session"] == date(2024, 3, 8)]
    march_11 = spring[spring["session"] == date(2024, 3, 11)]
    nov_1 = fall[fall["session"] == date(2024, 11, 1)]
    nov_4 = fall[fall["session"] == date(2024, 11, 4)]
    assert len(march_8) == 78 and len(march_11) == 78
    assert len(nov_1) == 78 and len(nov_4) == 78
    assert len(early) == 42
    assert march_8["ts"].iloc[0].utcoffset() == timedelta(hours=-5)
    assert march_11["ts"].iloc[0].utcoffset() == timedelta(hours=-4)
    assert nov_1["ts"].iloc[0].utcoffset() == timedelta(hours=-4)
    assert nov_4["ts"].iloc[0].utcoffset() == timedelta(hours=-5)
    assert early["ts"].iloc[0].utcoffset() == timedelta(hours=-5)
    assert early["available_at"].iloc[-1].astimezone(ET).hour == 13
    for frame in (spring, fall):
        bars = resample(frame, "15m")
        first = bars["ts"].iloc[0].astimezone(ET)
        second = bars["ts"].iloc[1].astimezone(ET)
        assert first.hour == 9 and first.minute == 30
        assert second.hour == 9 and second.minute == 45


def test_w_and_ihs_have_the_expected_extremes():
    w = w_bars()
    day = w[w["session"] == w["session"].iloc[0]]
    assert float(day["close"].iloc[10]) == pytest_approx(100.0)
    assert float(day["close"].iloc[30]) == pytest_approx(110.0)
    assert float(day["close"].iloc[50]) == pytest_approx(100.2)
    ihs = ihs_bars()
    day = ihs[ihs["session"] == ihs["session"].iloc[0]]
    assert float(day["close"].iloc[32]) < float(day["close"].iloc[12])
    assert float(day["close"].iloc[32]) < float(day["close"].iloc[52])


def pytest_approx(value):
    import pytest

    return pytest.approx(value)
