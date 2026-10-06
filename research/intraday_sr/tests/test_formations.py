"""SPEC v1.3.5 formation detector and Test B / F_* signals.

spec_doc v1.3.5 (8504fd7c19136141a32746234b63dd084106369d).
engine_spec v1.3.5.
"""

from __future__ import annotations

import hashlib
import time as time_mod
from dataclasses import asdict
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from research.intraday_sr.engine import FormationStats, formations_at, signals, signals_funnel
from research.intraday_sr.engine.signals import _formation_signal
from research.intraday_sr.engine.version import ENGINE_SPEC
from research.intraday_sr.grids import FORMATIONS, GRID_SHA256, N_TRIALS, PIVOT_N, TEST_A
from research.intraday_sr.tests.fixtures import FIXTURES, ihs_bars, w_bars
from research.intraday_sr.tests.fixtures.synthetic import (
    _FULL,
    _concat,
    _frame_from_closes,
    _volumes,
    _weekdays,
)
from research.intraday_sr.tests.test_lookahead import _DRAWS, _SEED, _sample_times, _same
from research.intraday_sr.types import ET, BarSet, EngineCfg, Formation, SignalCfg, Zone

# Test A on every fixture, TEST_A[0], K=3. Same digest at cf7a6e4.
_TEST_A_SIGNAL_SHA256 = "ca34bfe2dfb807b7eb3386957ac44b419eddb488b38336f01f113d299d7dde28"
_DAY = date(2024, 7, 1)


def _cfg() -> EngineCfg:
    return EngineCfg(k_zones=3)


def _f_sig(test: str = "F_W", target: str = "1R", tol: float = 0.25) -> SignalCfg:
    return SignalCfg(
        oscillator="rsi14_30_70",
        rvol_min=1.5,
        entry_tf="5m",
        target=target,
        k_confirm=0,
        variant_id=f"{test}-5m-{target}-tol{tol:.2f}",
        test=test,
        pivot_tol_atr=tol,
    )


def _b_sig(**overrides) -> SignalCfg:
    payload = dict(
        oscillator="rsi14_30_70",
        rvol_min=1.5,
        entry_tf="5m",
        target="1R",
        k_confirm=0,
        variant_id="B-5m-1R-k0",
        test="B",
        pivot_tol_atr=0.25,
    )
    payload.update(overrides)
    return SignalCfg(**payload)


def _end(frame: pd.DataFrame) -> datetime:
    return frame["available_at"].iloc[-1].to_pydatetime()


def _start(frame: pd.DataFrame) -> datetime:
    return frame["available_at"].iloc[0].to_pydatetime()


def _on_day(forms, day: date, tf: str, kind: str):
    return [
        item
        for item in forms
        if item.tf == tf and item.kind == kind and item.pivots[-1][0].date() == day
    ]


def _clock(ts: datetime) -> str:
    return ts.astimezone(ET).strftime("%H:%M")


def _quiet_days(count: int = 20) -> list[date]:
    return _weekdays(date(2024, 6, 3), count)


def _stack(symbol: str, closes_for_day, *, pattern_from: int, extra_for_pattern: tuple[int, ...]) -> pd.DataFrame:
    days = _quiet_days()
    base = 100.0 + 0.4 * np.sin(np.arange(_FULL) / 4.0)
    frames = []
    for index, day in enumerate(days):
        if index >= pattern_from:
            closes = closes_for_day(index)
            extra = extra_for_pattern
        else:
            closes = base + (index % 3) * 0.1
            extra = ()
        frames.append(
            _frame_from_closes(
                day,
                closes,
                symbol=symbol,
                wick=0.02,
                extra_low_at=extra,
                volumes=_volumes(_FULL, index),
            )
        )
    return _concat(frames)


def _early_w_closes() -> np.ndarray:
    closes = np.full(_FULL, 106.0)
    closes[0:11] = np.linspace(108.0, 100.0, 11)
    closes[11:21] = np.linspace(101.5, 109.0, 10)
    closes[21] = 105.0
    closes[22] = 100.12
    closes[23] = 104.0
    closes[24:33] = np.linspace(106.0, 114.0, 9)
    closes[33] = 110.5
    closes[34] = 109.2
    closes[35] = 108.4
    closes[36] = 110.0
    closes[37:] = np.linspace(111.0, 113.0, _FULL - 37)
    return closes


def _early_w() -> pd.DataFrame:
    closes = _early_w_closes()
    return _stack("EARLY", lambda _index: closes, pattern_from=16, extra_for_pattern=(10, 22))


def _negate(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for column in ("open", "close", "vwap"):
        out[column] = -out[column].astype(np.float64)
    high = out["high"].astype(np.float64).to_numpy(copy=True)
    low = out["low"].astype(np.float64).to_numpy(copy=True)
    out["high"] = -low
    out["low"] = -high
    return out


def _stamp_key(formed: Formation) -> tuple:
    return (
        tuple(item[0] for item in formed.pivots),
        formed.confirmed_ts,
        formed.break_ts,
        formed.available_at,
        formed.retest_ts,
    )


def test_classvars_stay_out_of_the_grid_hash():
    assert ENGINE_SPEC == "v1.3.5"
    assert GRID_SHA256 == "2ef95123c015d70a1751f559f21fa1249d0b08aea272319e232f2facb120aa22"
    assert N_TRIALS == 450
    assert len(FORMATIONS) == 48
    dumped = asdict(EngineCfg())
    assert "formation_pivot_tol" not in dumped
    assert "formation_pivot_tol_grid" not in dumped
    assert EngineCfg.formation_pivot_tol == 0.25
    assert EngineCfg.formation_pivot_tol_grid == (0.15, 0.25)
    assert EngineCfg.formation_break_bars == 60
    assert EngineCfg.formation_head_margin_atr == 0.10
    assert EngineCfg.formation_retest_bars == 6
    assert EngineCfg.formation_retest_tol_atr == 0.10


def test_w_and_ihs_emit_at_the_fixture_timestamps():
    expected = {
        ("W", "5m"): (("10:20", "13:40"), "14:00", "15:40", "15:45", 0.0),
        ("W", "15m"): (("10:15", "13:30"), "14:30", "15:45", "16:00", 0.0),
        ("IHS", "5m"): (("10:30", "12:10", "13:50"), "14:10", "15:05", "15:10", None),
        ("IHS", "15m"): (("10:30", "12:00", "13:45"), "14:45", "15:15", "15:30", None),
    }
    tapes = {"W": w_bars(), "IHS": ihs_bars()}
    for (kind, tf), (pivots, confirmed, break_at, retest, slope) in expected.items():
        forms = formations_at(BarSet(tapes[kind]), _end(tapes[kind]), _cfg(), tf=tf, kind=kind)
        day = _on_day(forms, _DAY, tf, kind)
        assert len(day) == 1
        formed = day[0]
        assert tuple(_clock(ts) for ts, _price in formed.pivots) == pivots
        assert _clock(formed.confirmed_ts) == confirmed
        assert _clock(formed.break_ts) == break_at
        assert _clock(formed.available_at) == break_at
        assert formed.retest_ts is not None and _clock(formed.retest_ts) == retest
        assert formed.zone_id is None
        if slope is not None:
            assert formed.neckline[1] == 0.0
        else:
            assert formed.neckline[1] != 0.0


def test_mirror_maps_w_to_m_and_ihs_to_hs():
    pairs = ((w_bars(), "W", "M"), (ihs_bars(), "IHS", "HS"))
    for frame, long_kind, short_kind in pairs:
        long = formations_at(BarSet(frame), _end(frame), _cfg(), tf="5m", kind=long_kind)
        short = formations_at(BarSet(_negate(frame)), _end(frame), _cfg(), tf="5m", kind=short_kind)
        long_keys = {_stamp_key(item) for item in long}
        short_keys = {_stamp_key(item) for item in short}
        assert long_keys == short_keys
        assert long_keys
        by_key = {_stamp_key(item): item for item in short}
        for item in long:
            other = by_key[_stamp_key(item)]
            for (_ts, price), (_other_ts, other_price) in zip(item.pivots, other.pivots):
                assert np.float32(other_price) == np.float32(-price)
            assert np.float32(other.neckline[0]) == np.float32(-item.neckline[0])
            assert np.float32(other.neckline[1]) == np.float32(-item.neckline[1])


def test_confirmation_is_at_or_before_the_break():
    for builder in (w_bars, ihs_bars):
        frame = builder()
        forms = formations_at(BarSet(frame), _end(frame), _cfg())
        assert forms
        for formed in forms:
            width = timedelta(minutes=5 if formed.tf == "5m" else 15)
            last = formed.pivots[-1][0]
            assert formed.confirmed_ts >= last + PIVOT_N[formed.tf] * width
            assert formed.confirmed_ts <= formed.break_ts <= formed.available_at
            if formed.retest_ts is not None:
                assert formed.retest_ts > formed.break_ts


def test_retest_stays_hidden_until_that_bar_closes():
    frame = w_bars()
    forms = formations_at(BarSet(frame), _end(frame), _cfg(), tf="5m", kind="W")
    formed = _on_day(forms, _DAY, "5m", "W")[0]
    at_break = formations_at(BarSet(frame), formed.break_ts, _cfg(), tf="5m", kind="W")
    early = _on_day(at_break, _DAY, "5m", "W")[0]
    assert early.break_ts == formed.break_ts
    assert early.retest_ts is None
    at_retest = formations_at(BarSet(frame), formed.retest_ts, _cfg(), tf="5m", kind="W")
    later = _on_day(at_retest, _DAY, "5m", "W")[0]
    assert later.retest_ts == formed.retest_ts


def test_invalidation_before_the_break_emits_nothing():
    closes = _early_w_closes()
    closes[24:] = 90.0
    frame = _stack("INV", lambda _index: closes, pattern_from=19, extra_for_pattern=(10, 22))
    forms = formations_at(BarSet(frame), _end(frame), _cfg(), tf="5m", kind="W")
    last = frame["session"].iloc[-1]
    assert not any(_clock(item.pivots[-1][0]) == "11:20" and item.pivots[-1][0].date() == last for item in forms)


def test_a_break_after_60_bars_expires():
    closes = np.full(_FULL, 101.5)
    closes[0:5] = np.linspace(104.0, 100.0, 5)
    closes[5:10] = np.linspace(101.0, 106.0, 5)
    closes[10:13] = np.array([104.0, 102.0, 100.05])
    closes[73:] = 108.0
    frame = _stack("EXP", lambda _index: closes, pattern_from=19, extra_for_pattern=(4, 12))
    forms = formations_at(BarSet(frame), _end(frame), _cfg(), tf="5m", kind="W")
    last = frame["session"].iloc[-1]
    # Second bottom is bar 12, open 10:30. A close through the neck at bar 73 is past +60.
    assert not any(item.pivots[-1][0].date() == last and _clock(item.pivots[-1][0]) == "10:30" for item in forms)


def test_dedupe_keeps_the_nearest_first_pivot():
    closes = np.full(_FULL, 106.0)
    closes[0:9] = np.linspace(108.0, 100.0, 9)
    closes[9:16] = np.linspace(102.0, 108.0, 7)
    closes[16:21] = np.linspace(106.0, 100.4, 5)
    closes[21:30] = np.linspace(102.0, 110.0, 9)
    closes[30:41] = np.linspace(108.0, 100.2, 11)
    closes[41:] = np.linspace(102.0, 116.0, _FULL - 41)
    frame = _stack("DED", lambda _index: closes, pattern_from=15, extra_for_pattern=(8, 20, 40))
    forms = formations_at(BarSet(frame), _end(frame), _cfg(), tf="5m", kind="W")
    last = frame["session"].iloc[-1]
    chosen = [
        item
        for item in forms
        if item.pivots[-1][0].date() == last and _clock(item.pivots[-1][0]) == "12:50"
    ]
    assert len(chosen) == 1
    assert _clock(chosen[0].pivots[0][0]) == "11:10"
    earlier = [
        item
        for item in forms
        if item.pivots[-1][0].date() == last and _clock(item.pivots[-1][0]) == "11:10"
    ]
    assert earlier
    far_price = earlier[0].pivots[0][1]
    near_price = chosen[0].pivots[0][1]
    last_price = chosen[0].pivots[-1][1]
    assert abs(last_price - far_price) <= abs(last_price - near_price) + 1e-6


def test_tighter_pivot_tol_drops_the_wide_pair():
    base = 100.0 + 0.15 * np.sin(np.arange(_FULL) / 3.0)
    closes = base.copy()
    closes[10] = 99.2
    closes[28] = 99.40
    closes[30:] = np.linspace(float(closes[29]), 102.5, _FULL - 30)
    days = _quiet_days()
    frames = []
    for index, day in enumerate(days):
        extra = (10, 28) if index == len(days) - 1 else ()
        path = closes if index == len(days) - 1 else base
        frames.append(
            _frame_from_closes(day, path, symbol="TOL", wick=0.01, extra_low_at=extra, volumes=_volumes(_FULL, index))
        )
    frame = _concat(frames)
    last = days[-1]
    wide = formations_at(BarSet(frame), _end(frame), _cfg(), 0.25, tf="5m", kind="W")
    tight = formations_at(BarSet(frame), _end(frame), _cfg(), 0.15, tf="5m", kind="W")
    assert _on_day(wide, last, "5m", "W")
    assert not _on_day(tight, last, "5m", "W")


def _formations_only(frame: pd.DataFrame, as_of: datetime):
    return formations_at(BarSet(frame), as_of, _cfg())


def test_formation_truncation_invariance():
    started = time_mod.perf_counter()
    rng = np.random.default_rng(_SEED)
    for builder in FIXTURES.values():
        frame = builder()
        for as_of in _sample_times(frame, rng):
            full = _formations_only(frame, as_of)
            truncated = frame.loc[frame["available_at"] <= as_of].reset_index(drop=True)
            assert _same(_formations_only(truncated, as_of), full)
    assert time_mod.perf_counter() - started < 60.0


def test_formation_poison_after_available_at():
    started = time_mod.perf_counter()
    rng = np.random.default_rng(_SEED + 1)
    for builder in FIXTURES.values():
        frame = builder()
        for as_of in _sample_times(frame, rng):
            expected = _formations_only(frame, as_of)
            poisoned = frame.copy()
            mask = poisoned["available_at"] > as_of
            n_poison = int(mask.sum())
            walk = rng.normal(0.0, 5.0, size=n_poison).astype(np.float32)
            for column in ("open", "high", "low", "close", "vwap"):
                poisoned.loc[mask, column] = walk
            poisoned.loc[mask, "volume"] = rng.integers(1, 50_000, size=n_poison).astype(np.float32)
            assert _same(_formations_only(poisoned, as_of), expected)
    assert time_mod.perf_counter() - started < 60.0


def test_formation_nan_poison_after_available_at():
    started = time_mod.perf_counter()
    rng = np.random.default_rng(_SEED + 2)
    for builder in FIXTURES.values():
        frame = builder()
        for as_of in _sample_times(frame, rng):
            expected = _formations_only(frame, as_of)
            poisoned = frame.copy()
            mask = poisoned["available_at"] > as_of
            for column in ("open", "high", "low", "close", "vwap"):
                poisoned.loc[mask, column] = np.nan
            assert _same(_formations_only(poisoned, as_of), expected)
    assert time_mod.perf_counter() - started < 60.0


def _hash_test_a(frame: pd.DataFrame, sig: SignalCfg, digest) -> int:
    found = list(signals(BarSet(frame), _start(frame), _end(frame), _cfg(), sig))
    for item in found:
        digest.update(item.symbol.encode())
        digest.update(item.tf.encode())
        digest.update(str(item.direction).encode())
        digest.update(item.test.encode())
        digest.update(np.float32(item.trigger).tobytes())
        digest.update(np.float32(item.stop).tobytes())
        digest.update(item.zone.zone_id.encode())
        digest.update(np.float32(item.zone.low).tobytes())
        digest.update(np.float32(item.zone.high).tobytes())
        digest.update(np.float32(item.zone.score).tobytes())
        digest.update(str(item.confluence).encode())
        digest.update(item.available_at.isoformat().encode())
        digest.update(item.as_of_ts.isoformat().encode())
        digest.update(item.expires_at.isoformat().encode())
        digest.update(item.variant_id.encode())
        for key in ("1R", "2R", "zone"):
            if key in item.targets:
                digest.update(key.encode())
                digest.update(np.float32(item.targets[key]).tobytes())
        for key in ("oscillator", "macd", "rvol"):
            digest.update(key.encode())
            digest.update(np.float32(item.components[key]).tobytes())
        digest.update(b"none" if item.formation is None else b"form")
    return len(found)


def test_test_a_signals_match_the_pre_formation_head():
    row = TEST_A[0]
    sig = SignalCfg(
        oscillator=row["oscillator"],
        rvol_min=row["rvol_min"],
        entry_tf=row["entry_tf"],
        target=row["target"],
        k_confirm=row["k_confirm"],
        variant_id=row["variant_id"],
        test="A",
    )
    digest = hashlib.sha256()
    counts = {name: _hash_test_a(builder(), sig, digest) for name, builder in FIXTURES.items()}
    assert counts == {
        "trend": 100,
        "range": 109,
        "W": 47,
        "IHS": 94,
        "gap": 0,
        "early_close": 19,
        "dst_spring": 0,
        "dst_fall": 0,
        "thanksgiving": 0,
    }
    assert digest.hexdigest() == _TEST_A_SIGNAL_SHA256


def test_f_signal_uses_the_retest_and_leaves_zone_id_empty():
    frame = _early_w()
    found = list(signals(BarSet(frame), _start(frame), _end(frame), _cfg(), _f_sig()))
    assert found
    last = frame["session"].iloc[-1]
    day = [item for item in found if item.available_at.date() == last]
    assert len(day) == 1
    item = day[0]
    assert item.formation is not None and item.formation.zone_id is None
    assert item.formation.retest_ts == item.available_at
    assert item.confluence == 0
    assert item.zone.kinds == ("formation",)
    assert item.test == "F_W"
    bar = frame[frame["available_at"] == item.available_at].iloc[0]
    assert item.trigger == float(bar["high"]) + 0.01
    atr = item.zone.atr_d
    extreme = item.formation.extreme[1]
    natural = extreme - 0.05 * atr
    floor = item.trigger - 0.10 * atr
    assert item.stop == min(natural, floor)
    assert item.expires_at == item.available_at + timedelta(minutes=30)
    assert "1R" in item.targets and "2R" in item.targets
    # The stock W retest is 15:45, after the 15:00 cutoff Test A already uses.
    stock = w_bars()
    funnel = signals_funnel(BarSet(stock), _start(stock), _end(stock), _cfg(), _f_sig())
    assert list(signals(BarSet(stock), _start(stock), _end(stock), _cfg(), _f_sig())) == []
    assert funnel.retest > 0 and funnel.emits == 0 and funnel.candidates > funnel.broken


def test_zone_target_skips_when_the_next_zone_is_inside_1r():
    frame = _early_w()
    funnel = signals_funnel(BarSet(frame), _start(frame), _end(frame), _cfg(), _f_sig(target="zone"))
    assert funnel.build_fail_zone_lt_1R > 0 or funnel.build_fail_no_ahead_zone > 0
    assert funnel.emits < funnel.retest


def test_b_last_low_is_the_touch_and_zone_id_is_set():
    frame = _early_w()
    found = list(signals(BarSet(frame), _start(frame), _end(frame), _cfg(), _b_sig()))
    item = next(row for row in found if row.formation is not None and row.formation.kind == "W")
    assert item.formation.zone_id == item.zone.zone_id
    assert item.zone.side == "support"
    touch = item.formation.pivots[-1][0]
    bar = frame[frame["ts"] == touch].iloc[0]
    assert float(bar["low"]) <= item.zone.high
    assert float(bar["close"]) >= item.zone.low
    atr = item.zone.atr_d
    anchor = min(item.zone.low, item.formation.extreme[1])
    natural = anchor - 0.05 * atr
    floor = item.trigger - 0.10 * atr
    assert item.stop == min(natural, floor)
    strict = list(signals(BarSet(frame), _start(frame), _end(frame), _cfg(), _b_sig(k_confirm=3)))
    assert len(strict) <= len(found)
    assert all(row.confluence >= 3 for row in strict)


def test_b_forces_pivot_tol_025():
    base = 100.0 + 0.15 * np.sin(np.arange(_FULL) / 3.0)
    closes = base.copy()
    closes[10] = 99.2
    closes[28] = 99.40
    closes[30:] = np.linspace(float(closes[29]), 102.5, _FULL - 30)
    days = _quiet_days()
    frames = []
    for index, day in enumerate(days):
        extra = (10, 28) if index == len(days) - 1 else ()
        path = closes if index == len(days) - 1 else base
        frames.append(
            _frame_from_closes(day, path, symbol="TOLB", wick=0.01, extra_low_at=extra, volumes=_volumes(_FULL, index))
        )
    frame = _concat(frames)
    wide = FormationStats()
    tight = FormationStats()
    formations_at(BarSet(frame), _end(frame), _cfg(), 0.25, tf="5m", stats=wide)
    formations_at(BarSet(frame), _end(frame), _cfg(), 0.15, tf="5m", stats=tight)
    assert wide.candidates > tight.candidates
    forced = signals_funnel(BarSet(frame), _start(frame), _end(frame), _cfg(), _b_sig(pivot_tol_atr=0.15))
    assert forced.candidates == wide.candidates


class _Prep:
    def __init__(self, high: float, low: float, stamp: datetime) -> None:
        self.highs = np.array([high], dtype=np.float64)
        self.lows = np.array([low], dtype=np.float64)
        self.factors = np.array([1.0], dtype=np.float64)
        self.available = [stamp]
        self.width = timedelta(minutes=5)


def _hand_formation(kind: str, prices: tuple[float, ...], stamp: datetime) -> Formation:
    step = timedelta(minutes=20)
    pivots = tuple((stamp + index * step, price) for index, price in enumerate(prices))
    confirmed = pivots[-1][0] + timedelta(minutes=20)
    return Formation(
        kind=kind,
        symbol="HAND",
        tf="5m",
        pivots=pivots,
        neckline=(110.0, 0.0),
        invalidation=min(prices) if kind in ("W", "IHS") else max(prices),
        break_ts=confirmed,
        retest_ts=confirmed + timedelta(minutes=5),
        confirmed_ts=confirmed,
        as_of_ts=confirmed,
        available_at=confirmed,
        zone_id=None,
    )


def _hand_zone(price: float, side: str, stamp: datetime) -> Zone:
    return Zone(
        symbol="HAND",
        low=price,
        high=price,
        side=side,
        score=0.0,
        components={},
        kinds=("formation",),
        as_of_ts=stamp,
        valid_from_ts=stamp,
        available_at=stamp,
        engine_cfg="K3|kc0.25",
        tf="5m",
        atr_d=2.0,
    )


def test_stop_floor_is_010_atr_from_the_trigger():
    stamp = datetime(2024, 7, 1, 10, 0, tzinfo=ET)
    formed = _hand_formation("W", (9.95, 9.96), stamp)
    zone = _hand_zone(9.95, "support", formed.retest_ts)
    sig = _f_sig()
    built, reason = _formation_signal(
        formed,
        zone,
        1,
        0,
        2.0,
        _Prep(10.0, 9.5, formed.retest_ts),
        EngineCfg(),
        sig,
        {"oscillator": 0.0, "macd": 0.0, "rvol": 0.0},
        [],
        pattern_and_zone=False,
    )
    assert reason is None and built is not None
    # Natural stop is 0.16 below the trigger; the 0.10·ATR floor widens it to 0.20.
    assert built.trigger == 10.01
    assert built.stop == 10.01 - 0.20
    short = _hand_formation("M", (10.04, 10.05), stamp)
    short_zone = _hand_zone(10.05, "resistance", short.retest_ts)
    built_s, reason_s = _formation_signal(
        short,
        short_zone,
        -1,
        0,
        2.0,
        _Prep(10.5, 10.0, short.retest_ts),
        EngineCfg(),
        _f_sig("F_M"),
        {"oscillator": 0.0, "macd": 0.0, "rvol": 0.0},
        [],
        pattern_and_zone=False,
    )
    assert reason_s is None and built_s is not None
    assert built_s.trigger == 9.99
    assert built_s.stop == 9.99 + 0.20
