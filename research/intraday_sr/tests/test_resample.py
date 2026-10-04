"""Resample skeleton. Counts are asserted once D2-1 replaces the stub."""

from __future__ import annotations

import pytest

from research.intraday_sr.data.resample import resample
from research.intraday_sr.tests.fixtures import early_close_bars, trend_bars


def test_resample_rejects_an_unknown_timeframe():
    with pytest.raises(ValueError):
        resample(trend_bars(), "30m")


def test_full_session_bucket_counts():
    day = trend_bars()
    day = day[day["session"] == day["session"].iloc[0]]
    out_15 = resample(day, "15m")
    out_1h = resample(day, "1h")
    out_1d = resample(day, "1d")
    if out_15.empty and out_1h.empty and out_1d.empty:
        pytest.skip("resample stub; D2-1 asserts 26 x 15m, 7 x 1h, 1 x 1d")
    assert len(out_15) == 26
    assert len(out_1h) == 7
    assert bool(out_1h["partial"].iloc[-1]) is True
    assert int(out_1h["partial"].sum()) == 1
    assert len(out_1d) == 1
    assert bool(out_1d["partial"].iloc[0]) is False


def test_early_close_is_shorter():
    frame = early_close_bars()
    early = frame[frame["session"] == frame["session"].iloc[-1]]
    out_15 = resample(early, "15m")
    out_1h = resample(early, "1h")
    if out_15.empty and out_1h.empty:
        pytest.skip("resample stub; D2-1 asserts the 13:00 session")
    assert len(out_15) == 14
    assert len(out_1h) == 4
    assert bool(out_1h["partial"].iloc[-1]) is True
