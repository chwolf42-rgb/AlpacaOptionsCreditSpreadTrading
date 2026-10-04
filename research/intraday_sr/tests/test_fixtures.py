"""Synthetic fixtures cover the six shapes S0 asked for."""

from __future__ import annotations

from datetime import date, time

from research.intraday_sr.types import ET
from research.intraday_sr.tests.fixtures import (
    early_close_bars,
    gap_bars,
    ihs_bars,
    range_bars,
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
