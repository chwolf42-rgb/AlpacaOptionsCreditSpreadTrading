"""Harness entry points for F/B signals, formations_in, and Test A stack_touch.

The harness loads these names (integ/test-bf-harness @ ddbc8a8). ``stack_touch``
is the Architect lock: ``(touch_ts, rc_ts)`` are entry-TF bar opens. SPEC §5
step 4 allows the touch bar itself to be the rejection close, so the gap is
0 to 3 entry-TF bars.
"""

from __future__ import annotations

import inspect
import math
from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from research.intraday_sr.engine import formation_signals, formations_at, formations_in, signals, stack_touch
from research.intraday_sr.engine import test_b_signals as run_test_b
from research.intraday_sr.engine.levels import timeframe_frame
from research.intraday_sr.engine.signals import _clear_touch_cache
from research.intraday_sr.grids import FORMATIONS, TEST_A, TEST_B
from research.intraday_sr.tests.fixtures import FIXTURES
from research.intraday_sr.tests.test_formations import _b_sig, _early_w, _end, _negate, _start
from research.intraday_sr.types import BarSet, EngineCfg, Signal, SignalCfg, as_et

_OPTIONAL = ("oscillator", "macd", "rvol")


def _test_a() -> SignalCfg:
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


def _cfg(k_zones: int = 3) -> EngineCfg:
    return EngineCfg(k_zones=k_zones)


def _width(tf: str) -> timedelta:
    return timedelta(minutes=15 if tf == "15m" else 5)


def _f_row(test: str = "F_W", target: str = "1R", tol: float = 0.25, tf: str = "5m") -> dict:
    vid = f"{test}-{tf}-{target}-tol{tol:.2f}"
    return next(row for row in FORMATIONS if row["variant_id"] == vid)


def _b_row() -> dict:
    return next(row for row in TEST_B if row["variant_id"] == "B-K3-rsi14_30_70-rvol1.5-5m-1R-k0")


def _direction(kind: str) -> int:
    if kind in ("W", "IHS"):
        return 1
    if kind in ("M", "HS"):
        return -1
    raise AssertionError(kind)


def _assert_fb(signal: Signal, variant: dict) -> None:
    form = signal.formation
    assert form is not None
    assert form.symbol == signal.symbol
    assert form.tf == signal.tf == variant["entry_tf"]
    assert form.available_at <= signal.available_at
    assert signal.direction == _direction(form.kind)
    assert signal.variant_id == variant["variant_id"]
    assert signal.test == variant["test"]
    assert math.isfinite(signal.trigger) and math.isfinite(signal.stop)
    assert signal.expires_at > signal.available_at
    assert signal.as_of_ts <= signal.available_at
    assert "1R" in signal.targets and "2R" in signal.targets
    assert math.isfinite(float(signal.zone.atr_d)) and float(signal.zone.atr_d) > 0.0
    if str(variant["test"]).startswith("F"):
        assert signal.confluence == 0
        assert all(float(signal.components[name]) == 0.0 for name in _OPTIONAL)
        assert form.zone_id is None
        assert form.kind == variant["kind"]
    else:
        assert variant["test"] == "B"
        assert form.kind in ("W", "IHS", "M", "HS")
        assert form.zone_id
        assert signal.confluence == sum(1 for name in _OPTIONAL if float(signal.components[name]) != 0.0)


def test_harness_signatures():
    assert list(inspect.signature(formation_signals).parameters) == ["bars", "start", "end", "cfg", "variant"]
    assert list(inspect.signature(run_test_b).parameters) == ["bars", "start", "end", "cfg", "sig"]
    assert list(inspect.signature(formations_in).parameters) == ["bars", "start", "end", "cfg", "pivot_tol"]
    assert list(inspect.signature(stack_touch).parameters) == ["bars", "signal", "cfg", "sig"]


def test_formation_signals_use_cfg_k_zones_and_match_the_contract():
    frame = _early_w()
    bars = BarSet(frame)
    variant = _f_row()
    locked = list(formation_signals(bars, _start(frame), _end(frame), EngineCfg(k_zones=5), variant))
    assert locked
    assert all(item.zone.engine_cfg.startswith("K5|") for item in locked)
    for item in locked:
        _assert_fb(item, variant)
    other = list(formation_signals(bars, _start(frame), _end(frame), EngineCfg(k_zones=3), variant))
    assert other
    assert all(item.zone.engine_cfg.startswith("K3|") for item in other)
    short = _negate(frame)
    mirror = _f_row("F_M")
    mirrored = list(formation_signals(BarSet(short), _start(short), _end(short), EngineCfg(k_zones=5), mirror))
    assert mirrored
    for item in mirrored:
        _assert_fb(item, mirror)
        assert item.direction == -1


def test_test_b_signals_match_signals_and_reject_other_tests():
    frame = _early_w()
    bars = BarSet(frame)
    variant = _b_row()
    sig = SignalCfg(
        oscillator=variant["oscillator"],
        rvol_min=variant["rvol_min"],
        entry_tf=variant["entry_tf"],
        target=variant["target"],
        k_confirm=variant["k_confirm"],
        variant_id=variant["variant_id"],
        test="B",
    )
    direct = list(signals(bars, _start(frame), _end(frame), _cfg(), sig))
    wrapped = list(run_test_b(bars, _start(frame), _end(frame), _cfg(), sig))
    assert wrapped == direct
    assert wrapped
    for item in wrapped:
        _assert_fb(item, variant)
    with pytest.raises(ValueError, match="test_b_signals"):
        run_test_b(bars, _start(frame), _end(frame), _cfg(), _b_sig(test="A"))


def test_formations_in_matches_the_as_of_break_object():
    frame = _early_w()
    bars = BarSet(frame)
    start, end = _start(frame), _end(frame)
    cfg = _cfg()
    got = list(formations_in(bars, start, end, cfg, 0.25))
    assert got
    assert got == sorted(got, key=lambda item: (item.available_at, item.formation_id))
    assert len({item.formation_id for item in got}) == len(got)
    scanned = [
        item
        for item in formations_at(bars, end, cfg, 0.25)
        if start <= item.available_at <= end
    ]
    # No delayed clamps: clearing retest_ts on the end scan is exact.
    expected = [item if item.retest_ts is None else replace(item, retest_ts=None) for item in scanned]
    assert got == sorted(expected, key=lambda item: (item.available_at, item.formation_id))
    sample = got[:: max(1, len(got) // 5)]
    for item in sample:
        at_break = [
            formed
            for formed in formations_at(bars, item.available_at, cfg, 0.25)
            if formed.formation_id == item.formation_id and formed.available_at == item.available_at
        ]
        assert len(at_break) == 1
        assert at_break[0].retest_ts is None
        assert item == at_break[0]
    both = {item.tf for item in got}
    assert both == {"5m", "15m"}


def test_formations_in_rescans_a_clamp_hidden_at_the_break():
    frame = _early_w().copy()
    frame["high_unclamped"] = frame["high"].to_numpy(copy=True)
    frame["low_unclamped"] = frame["low"].to_numpy(copy=True)
    frame["bad_print"] = False
    frame["bad_print_visible_at"] = frame["available_at"]
    # A clamp on a mid-tape bar, visible six bars later, with a different high.
    idx = len(frame) // 2
    frame.loc[idx, "bad_print"] = True
    frame.loc[idx, "high"] = float(frame["high"].iloc[idx]) + 1.5
    frame.loc[idx, "bad_print_visible_at"] = frame["available_at"].iloc[idx + 6]
    bars = BarSet(frame)
    start, end = _start(frame), _end(frame)
    cfg = _cfg()
    got = list(formations_in(bars, start, end, cfg, 0.25))
    for item in got:
        at_break = [
            formed
            for formed in formations_at(bars, item.available_at, cfg, 0.25)
            if formed.formation_id == item.formation_id and formed.available_at == item.available_at
        ]
        assert len(at_break) == 1
        assert at_break[0].retest_ts is None
        assert item == at_break[0]
    hidden = as_et(frame["available_at"].iloc[idx].to_pydatetime(), "available_at")
    visible_at = as_et(frame["bad_print_visible_at"].iloc[idx].to_pydatetime(), "bad_print_visible_at")
    end_scan = formations_at(bars, end, cfg, 0.25)
    for formed in end_scan:
        if not (hidden <= formed.available_at < visible_at):
            continue
        at_break = next(
            (
                item
                for item in formations_at(bars, formed.available_at, cfg, 0.25)
                if item.formation_id == formed.formation_id and item.available_at == formed.available_at
            ),
            None,
        )
        if at_break is None:
            assert all(item.formation_id != formed.formation_id for item in got)
        else:
            frozen = at_break if at_break.retest_ts is None else replace(at_break, retest_ts=None)
            assert frozen in got


def _entry_opens(frame, tf: str) -> dict:
    tape = frame if tf == "5m" else timeframe_frame(frame, tf)
    opens = [as_et(stamp.to_pydatetime(), "ts") for stamp in tape["ts"]]
    return {stamp: index for index, stamp in enumerate(opens)}


def _assert_touch(signal: Signal, touch, rc, opens: dict) -> None:
    # Count entry-TF bars, not wall-clock minutes. A touch on the last bar
    # of a session and an RC on the next session's open are one bar apart.
    gap = opens[rc] - opens[touch]
    assert 0 <= gap <= 3
    assert touch <= rc
    assert touch < signal.available_at
    assert rc < signal.available_at
    # ``ts`` is the bar open. Open + entry-TF width is that bar's close.
    # The arm bar is strictly after the RC, and Signal.available_at is the
    # arm close, so the RC close is strictly before available_at.
    assert rc + _width(signal.tf) < signal.available_at


def test_stack_touch_on_every_fixture_signal():
    sig = _test_a()
    cfg = _cfg()
    same_bar: list[tuple[str, Signal]] = []
    total = 0
    for name, builder in FIXTURES.items():
        frame = builder()
        bars = BarSet(frame)
        opens = _entry_opens(frame, sig.entry_tf)
        found = list(signals(bars, _start(frame), _end(frame), cfg, sig))
        for signal in found:
            touch, rc = stack_touch(bars, signal, cfg, sig)
            _assert_touch(signal, touch, rc, opens)
            total += 1
            if touch == rc:
                same_bar.append((name, signal))
    assert total == 369
    assert same_bar, "a fixture Test A signal whose touch bar is the RC"
    _clear_touch_cache()


def test_stack_touch_ignores_bars_after_as_of():
    sig = _test_a()
    cfg = _cfg()
    checked = 0
    for builder in FIXTURES.values():
        frame = builder()
        bars = BarSet(frame)
        found = list(signals(bars, _start(frame), _end(frame), cfg, sig))
        if not found:
            continue
        signal = found[len(found) // 2]
        _clear_touch_cache()
        expected = stack_touch(bars, signal, cfg, sig)
        truncated = frame.loc[frame["available_at"] <= signal.as_of_ts].reset_index(drop=True)
        _clear_touch_cache()
        assert stack_touch(BarSet(truncated), signal, cfg, sig) == expected
        poisoned = frame.copy()
        mask = poisoned["available_at"] > signal.as_of_ts
        n_poison = int(mask.sum())
        if n_poison:
            walk = np.linspace(-3.0, 3.0, n_poison, dtype=np.float32)
            for column in ("open", "high", "low", "close", "vwap"):
                poisoned.loc[mask, column] = walk
            poisoned.loc[mask, "volume"] = 1.0
            _clear_touch_cache()
            assert stack_touch(BarSet(poisoned), signal, cfg, sig) == expected
            nanned = frame.copy()
            for column in ("open", "high", "low", "close", "vwap"):
                nanned.loc[mask, column] = np.nan
            _clear_touch_cache()
            assert stack_touch(BarSet(nanned), signal, cfg, sig) == expected
        checked += 1
    assert checked >= 1


def test_stack_touch_raises_when_it_cannot_resolve():
    sig = _test_a()
    cfg = _cfg()
    frame = _early_w()
    bars = BarSet(frame)
    found = list(signals(bars, _start(frame), _end(frame), cfg, _test_a()))
    if not found:
        frame = FIXTURES["trend"]()
        bars = BarSet(frame)
        found = list(signals(bars, _start(frame), _end(frame), cfg, sig))
    assert found
    signal = found[0]
    with pytest.raises(ValueError, match="stack_touch"):
        stack_touch(bars, signal, cfg, _b_sig())
    other = SignalCfg(
        oscillator=sig.oscillator,
        rvol_min=sig.rvol_min,
        entry_tf=sig.entry_tf,
        target="2R" if sig.target != "2R" else "1R",
        k_confirm=sig.k_confirm,
        variant_id=sig.variant_id + "-other",
        test="A",
    )
    _clear_touch_cache()
    with pytest.raises(LookupError, match="stack_touch found"):
        stack_touch(bars, signal, cfg, other)
