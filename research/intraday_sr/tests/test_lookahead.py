"""Lookahead gate (spec §4.12).

(a) truncation invariance
(b) poison bars after T, including a NaN poison
(d) higher-timeframe values come only from constituent 5m bars and stay
    hidden until that bucket closes

(c) the harness guard, is owned by Developer 1 on
``research/intraday-sr-harness``. It is not in this file.

Floats compare as float32, with NaN equal to NaN.
"""

from __future__ import annotations

from dataclasses import is_dataclass
from datetime import date, datetime
from typing import Mapping

import numpy as np
import pandas as pd

from research.intraday_sr.data.resample import resample
from research.intraday_sr.engine import formations_at, levels_at, signals, zones_at
from research.intraday_sr.tests.test_badprint import _bad_print_tape
from research.intraday_sr.grids import TEST_A
from research.intraday_sr.tests.fixtures import FIXTURES, early_close_bars, trend_bars
from research.intraday_sr.types import ET, BarSet, EngineCfg, SignalCfg

_SEED = 20261004
_DRAWS = 200


def _cfg() -> EngineCfg:
    return EngineCfg(k_zones=3)


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


def _f32_same(left, right) -> bool:
    a = np.float32(left)
    b = np.float32(right)
    if np.isnan(a) and np.isnan(b):
        return True
    return a.tobytes() == b.tobytes()


def _same(left, right) -> bool:
    if isinstance(left, (float, np.floating)) or isinstance(right, (float, np.floating)):
        return _f32_same(left, right)
    if is_dataclass(left) and type(left) is type(right):
        return all(_same(getattr(left, name), getattr(right, name)) for name in left.__dataclass_fields__)
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(_same(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) and type(left) is type(right):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def _output(frame: pd.DataFrame, as_of: datetime):
    bars = BarSet(frame)
    start = frame["available_at"].iloc[0]
    return (
        levels_at(bars, as_of, _cfg()),
        zones_at(bars, as_of, _cfg()),
        formations_at(bars, as_of, _cfg()),
        list(signals(bars, start, as_of, _cfg(), _sig())),
    )


def _sample_times(frame: pd.DataFrame, rng: np.random.Generator) -> list[datetime]:
    stamps = list(frame["available_at"].drop_duplicates())
    choose = min(_DRAWS, len(stamps))
    idx = rng.choice(len(stamps), size=choose, replace=False)
    return [stamps[int(i)] for i in idx]


def test_truncation_invariance_a():
    rng = np.random.default_rng(_SEED)
    for builder in FIXTURES.values():
        frame = builder()
        for as_of in _sample_times(frame, rng):
            full = _output(frame, as_of)
            truncated = frame.loc[frame["available_at"] <= as_of].reset_index(drop=True)
            assert _same(_output(truncated, as_of), full)


def test_poison_after_t_b():
    rng = np.random.default_rng(_SEED + 1)
    for builder in FIXTURES.values():
        frame = builder()
        for as_of in _sample_times(frame, rng):
            expected = _output(frame, as_of)
            poisoned = frame.copy()
            mask = poisoned["available_at"] > as_of
            n_poison = int(mask.sum())
            walk = rng.normal(0.0, 5.0, size=n_poison).astype(np.float32)
            for column in ("open", "high", "low", "close", "vwap"):
                poisoned.loc[mask, column] = walk
            poisoned.loc[mask, "volume"] = rng.integers(1, 50_000, size=n_poison).astype(np.float32)
            assert _same(_output(poisoned, as_of), expected)


def test_nan_poison_after_t_b():
    rng = np.random.default_rng(_SEED + 2)
    for builder in FIXTURES.values():
        frame = builder()
        for as_of in _sample_times(frame, rng):
            expected = _output(frame, as_of)
            poisoned = frame.copy()
            mask = poisoned["available_at"] > as_of
            for column in ("open", "high", "low", "close", "vwap"):
                poisoned.loc[mask, column] = np.nan
            assert _same(_output(poisoned, as_of), expected)


def test_truncation_invariance_with_a_bad_print():
    """A clamp visible only at t+2 must not leak into an earlier as-of."""
    frame, _peak, _early, _late = _bad_print_tape()
    rng = np.random.default_rng(_SEED)
    for as_of in _sample_times(frame, rng):
        full = _output(frame, as_of)
        truncated = frame.loc[frame["available_at"] <= as_of].reset_index(drop=True)
        assert _same(_output(truncated, as_of), full)


def test_gate_is_non_empty_on_the_long_trend():
    frame = trend_bars()
    assert frame["session"].nunique() >= 30
    assert frame["volume"].nunique() > 1
    as_of = frame["available_at"].iloc[-1].to_pydatetime()
    bars = BarSet(frame)
    assert len(levels_at(bars, as_of, _cfg())) >= 1
    assert len(zones_at(bars, as_of, _cfg())) >= 1
    assert len(list(signals(bars, frame["available_at"].iloc[0].to_pydatetime(), as_of, _cfg(), _sig()))) >= 1


def test_htf_values_match_constituents_and_hide_mid_bucket():
    for builder in FIXTURES.values():
        frame = builder()
        for tf in ("15m", "1h", "1d"):
            out = resample(frame, tf)
            assert not out.empty
            htf = BarSet(out)
            for row in out.itertuples(index=False):
                same = frame[(frame["symbol"] == row.symbol) & (frame["session"] == row.session)]
                const = same[(same["ts"] >= row.ts) & (same["available_at"] <= row.available_at)].sort_values("ts")
                assert not const.empty
                assert const["available_at"].max() == row.available_at
                assert _f32_same(row.open, const["open"].iloc[0])
                assert _f32_same(row.high, const["high"].max())
                assert _f32_same(row.low, const["low"].min())
                assert _f32_same(row.close, const["close"].iloc[-1])
                assert abs(float(row.volume) - float(const["volume"].sum())) < 1e-4
                mid = (row.ts + pd.Timedelta(minutes=5)).to_pydatetime()
                visible = htf.visible(mid)
                if not visible.empty:
                    hit = (visible["symbol"] == row.symbol) & (visible["ts"] == row.ts)
                    assert not bool(hit.any())
    early = early_close_bars()
    early = early[early["session"] == date(2024, 7, 3)]
    daily = resample(early, "1d")
    close = daily["available_at"].iloc[0].astimezone(ET)
    assert close.hour == 13 and close.minute == 0
