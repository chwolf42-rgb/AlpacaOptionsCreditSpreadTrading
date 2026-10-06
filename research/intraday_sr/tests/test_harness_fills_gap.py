"""A1b fill fix: an open past the target by any amount (even < 1 tick) fills at the open, so no exit fill can land
outside its bar. Property test on seeded random bars + the two A1 FillOutsideBar bars (MRK, C)."""

import math
import random

import pytest

from research.intraday_sr.harness.fills import STOP, TARGET, exit_on_bar


def _bar(rng: random.Random, base: float):
    """Random OHLC with o, c in [l, h]; sometimes un-rounded (adjusted) prices, sometimes flat/degenerate bars."""
    span = rng.choice([0.0, rng.uniform(0.0, 0.02), rng.uniform(0.0, 2.0)])
    l = base + rng.uniform(-1.0, 1.0)
    h = l + span
    o = rng.choice([l, h, rng.uniform(l, h)])
    if rng.random() < 0.5:                                   # float32-style bar prices, as in the 5m cache
        import struct
        f32 = lambda x: struct.unpack("f", struct.pack("f", x))[0]
        l, h, o = f32(l), f32(h), f32(o)
        o = min(max(o, l), h)
        if h < l:
            l, h = h, l
    return o, h, l


def _near(rng: random.Random, o: float, h: float, l: float, tick: float) -> float:
    """A price near the bar: sub-tick gaps around o/h/l, exact touches, or anywhere in a wider band (un-rounded)."""
    anchor = rng.choice([o, h, l])
    return anchor + rng.choice([0.0, rng.uniform(-tick, tick), rng.uniform(-3 * tick, 3 * tick),
                                rng.uniform(-1.5, 1.5)])


@pytest.mark.parametrize("seed", [7, 11, 2026])
def test_every_exit_fill_lies_inside_the_bar(seed):
    rng = random.Random(seed)
    n_hits = {(d, eb): 0 for d in (1, -1) for eb in (True, False)}
    n_gap_target = 0
    for _ in range(20_000):
        base = rng.uniform(20.0, 500.0)
        adj = rng.choice([1.0, rng.uniform(0.5, 1.5)])
        tick = 0.01 / adj
        o, h, l = _bar(rng, base)
        d = rng.choice([1, -1])
        entry_bar = rng.random() < 0.5
        if entry_bar:
            # precondition on the entry bar: the stop sits on the loss side of an entry fill inside the bar
            e = rng.uniform(l, h)
            stop = e - d * abs(_near(rng, o, h, l, tick) - e) - d * rng.uniform(0.0, 0.5) - d * 1e-9
            target = e + d * abs(_near(rng, o, h, l, tick) - e) + d * rng.uniform(0.0, 0.5) + d * 1e-9
        else:
            a, b = _near(rng, o, h, l, tick), _near(rng, o, h, l, tick)
            stop, target = (min(a, b), max(a, b)) if d > 0 else (max(a, b), min(a, b))
            if stop == target:
                target += d * tick
        hit = exit_on_bar(d, stop, target, o, h, l, tick, entry_bar)
        if hit is None:
            continue
        n_hits[(d, entry_bar)] += 1
        assert l <= hit.price <= h, (d, entry_bar, stop, target, o, h, l, tick, hit)
        assert hit.kind in (STOP, TARGET)
        if entry_bar:
            assert hit.kind == STOP and not hit.gap
        if hit.kind == TARGET and hit.gap:
            assert hit.price == o and d * (o - target) > 0
            n_gap_target += 1
    assert all(v > 100 for v in n_hits.values()), n_hits      # all four (direction, entry_bar) cases exercised
    assert n_gap_target > 100


@pytest.mark.parametrize("d", [1, -1])
def test_sub_tick_gap_past_target_fills_at_open(d):
    tick = 0.01
    target = 100.0
    o = target + d * 0.004                                    # past the target by < 1 tick
    h, l = (o + 0.10, o) if d > 0 else (o, o - 0.10)          # whole bar past the target
    hit = exit_on_bar(d, target - d * 1.0, target, o, h, l, tick, False)
    assert hit is not None and hit.kind == TARGET and hit.gap and hit.price == o and l <= hit.price <= h


@pytest.mark.parametrize("d", [1, -1])
def test_touch_from_inside_still_needs_one_tick_trade_through(d):
    tick = 0.01
    target = 100.0
    o = target - d * 0.05                                     # opens on the near side
    far = target + d * 0.005                                  # trades through by only half a tick
    h, l = (far, o) if d > 0 else (o, far)
    assert exit_on_bar(d, target - d * 1.0, target, o, h, l, tick, False) is None
    far = target + d * 0.01
    h, l = (far, o) if d > 0 else (o, far)
    hit = exit_on_bar(d, target - d * 1.0, target, o, h, l, tick, False)
    assert hit is not None and hit.kind == TARGET and not hit.gap and hit.price == target


def test_open_exactly_at_target_is_not_a_gap():
    hit = exit_on_bar(1, 99.0, 100.0, 100.0, 100.005, 99.99, 0.01, False)
    assert hit is None


# The two A1 FillOutsideBar bars. OHLC and adj_factor are the exact simulator inputs, read on 2026-10-06 from the
# fixed-33 5m cache through the harness loader (S0Adapter.frame -> sim_bar_frame); tick = 0.01 / adj_factor.
MRK_2026_01_27_1055 = dict(o=106.01000213623047, h=106.16000366210938, l=106.01000213623047, adj=1.0206166067713258)
C_2021_02_22_1540 = dict(o=54.380001068115234, h=54.380001068115234, l=54.2599983215332, adj=1.2033057851239668)


def test_fixture_mrk_long_target_gap_fills_at_open():
    b = MRK_2026_01_27_1055
    target, tick = 106.00112328813854, 0.01 / b["adj"]
    assert 0 < b["o"] - target < tick                          # the sub-tick gap the old rule missed
    hit = exit_on_bar(1, target - 2.0, target, b["o"], b["h"], b["l"], tick, False)
    assert hit is not None and hit.kind == TARGET and hit.gap
    assert hit.price == b["o"] and b["l"] <= hit.price <= b["h"]


def test_fixture_c_short_target_gap_fills_at_open():
    b = C_2021_02_22_1540
    target, tick = 54.385378999766296, 0.01 / b["adj"]
    assert 0 < target - b["o"] < tick
    hit = exit_on_bar(-1, target + 2.0, target, b["o"], b["h"], b["l"], tick, False)
    assert hit is not None and hit.kind == TARGET and hit.gap
    assert hit.price == b["o"] and b["l"] <= hit.price <= b["h"]
