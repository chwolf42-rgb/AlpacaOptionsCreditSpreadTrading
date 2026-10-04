"""SPEC section 4.12 test (c): the harness guard raises on an injected future object."""

import pandas as pd
import pytest

from research.intraday_sr.harness.guard import Guard, LookaheadError
from research.intraday_sr.harness.portfolio import FrameBarSource, simulate
from research.intraday_sr.tests._harness_stubs import flat_day, idx, sig, t


def test_guard_raises_on_future_object():
    g = Guard(t("2024-03-04", "10:00"))
    s = sig(at="10:05")
    with pytest.raises(LookaheadError):
        g.check(s)
    w = g.wrap(s)
    assert w.available_at == s.available_at          # scheduling metadata only
    with pytest.raises(LookaheadError):
        _ = w.trigger
    g.advance(t("2024-03-04", "10:05"))
    assert w.trigger == s.trigger


def test_guard_raises_on_nested_future_zone_inside_simulate():
    # zone becomes available AFTER the signal claims to be: an engine bug the harness must catch
    s = sig(at="10:00", zone_at=t("2024-03-04", "11:00"))
    frames = {"AAA": flat_day("AAA", "2024-03-04", overrides={idx("10:05"): (100.05, 100.3, 100.0, 100.2)})}
    with pytest.raises(LookaheadError):
        simulate([s], FrameBarSource(frames), tier_fn=lambda *_: "T2")


def test_guard_rejects_naive_timestamps_and_backwards_clock():
    g = Guard(t("2024-03-04", "10:00"))

    class O:
        available_at = pd.Timestamp("2024-03-04 09:00")
    with pytest.raises(LookaheadError):
        g.check(O())
    with pytest.raises(LookaheadError):
        g.advance(t("2024-03-04", "09:00"))
