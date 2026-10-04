"""Lookahead gate (spec §4.12).

(a) truncation invariance
(b) poison bars after T
(d) no higher-timeframe bar is visible before its last 5m close

(c) the harness guard, is owned by Developer 1 on
``research/intraday-sr-harness``. It is not in this file.

S0 runs (a), (b), and (d) against engine stubs. D2-1 fills resample.
D2-2 fills levels. The assertions stay the same.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from research.intraday_sr.data.resample import resample
from research.intraday_sr.engine import formations_at, levels_at, signals, zones_at
from research.intraday_sr.grids import TEST_A
from research.intraday_sr.tests.fixtures import FIXTURES
from research.intraday_sr.types import BarSet, EngineCfg, SignalCfg

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
            assert _output(truncated, as_of) == full


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
            assert _output(poisoned, as_of) == expected


def test_htf_not_visible_before_last_constituent_d():
    for builder in FIXTURES.values():
        frame = builder()
        for tf in ("15m", "1h", "1d"):
            out = resample(frame, tf)
            if out.empty:
                continue
            for row in out.itertuples(index=False):
                same = frame[(frame["symbol"] == row.symbol) & (frame["session"] == row.session)]
                const = same[(same["ts"] >= row.ts) & (same["available_at"] <= row.available_at)]
                assert not const.empty
                assert const["available_at"].max() == row.available_at
                spilled = same[(same["ts"] >= row.ts) & (same["available_at"] > row.available_at)]
                # A completed bucket does not depend on a bar that closes later.
                assert spilled.empty or spilled["ts"].min() >= row.available_at
