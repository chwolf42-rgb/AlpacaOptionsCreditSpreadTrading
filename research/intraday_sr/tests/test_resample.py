"""Resample counts and the partial last hour."""

from __future__ import annotations

import pytest

from research.intraday_sr.data.resample import resample
from research.intraday_sr.tests.fixtures import early_close_bars, trend_bars
from research.intraday_sr.types import ET


def test_resample_rejects_an_unknown_timeframe():
    with pytest.raises(ValueError):
        resample(trend_bars(), "30m")


def test_full_session_bucket_counts():
    day = trend_bars()
    day = day[day["session"] == day["session"].iloc[0]]
    out_15 = resample(day, "15m")
    out_1h = resample(day, "1h")
    out_1d = resample(day, "1d")
    assert len(out_15) == 26
    assert out_15["ts"].iloc[0].astimezone(ET).hour == 9
    assert out_15["ts"].iloc[0].astimezone(ET).minute == 30
    assert len(out_1h) == 7
    last_hour = out_1h.iloc[-1]
    assert last_hour["ts"].astimezone(ET).hour == 15
    assert last_hour["ts"].astimezone(ET).minute == 30
    assert bool(last_hour["partial"]) is True
    assert int(out_1h["partial"].sum()) == 1
    assert len(out_1d) == 1
    assert bool(out_1d["partial"].iloc[0]) is False


def test_early_close_is_shorter():
    frame = early_close_bars()
    early = frame[frame["session"] == frame["session"].iloc[-1]]
    out_15 = resample(early, "15m")
    out_1h = resample(early, "1h")
    out_1d = resample(early, "1d")
    assert len(out_15) == 14
    assert len(out_1h) == 4
    last_hour = out_1h.iloc[-1]
    assert last_hour["ts"].astimezone(ET).hour == 12
    assert last_hour["ts"].astimezone(ET).minute == 30
    assert bool(last_hour["partial"]) is True
    close = out_1d["available_at"].iloc[0].astimezone(ET)
    assert close.hour == 13 and close.minute == 0
    assert int(out_1d["n_bars"].iloc[0]) == 42


def test_daily_bar_survives_a_missing_last_print():
    day = trend_bars()
    day = day[day["session"] == day["session"].iloc[0]].iloc[:-1]
    daily = resample(day, "1d")
    intraday = resample(day, "15m")
    assert len(daily) == 1
    assert int(daily["n_bars"].iloc[0]) == 77
    assert int(intraday.attrs["dropped_buckets"]) >= 1
    assert len(intraday) < 26
