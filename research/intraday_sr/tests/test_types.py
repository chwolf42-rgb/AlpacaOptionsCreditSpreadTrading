"""§3 constructors still work. S0 additions fill themselves in."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from research.intraday_sr.types import (
    ET,
    Formation,
    Signal,
    Zone,
    nearest_zones,
    zone_id_for,
)


def _ts(hour: int = 10, minute: int = 0) -> datetime:
    return datetime(2024, 6, 3, hour, minute, tzinfo=ET)


def _zone(**overrides) -> Zone:
    payload = dict(
        symbol="ADBE",
        low=100.0,
        high=100.2,
        side="support",
        score=0.5,
        components={"touches": 0.2},
        kinds=("pivot_5m",),
        as_of_ts=_ts(),
        valid_from_ts=_ts(),
        available_at=_ts(),
        engine_cfg="cfg-a",
    )
    payload.update(overrides)
    return Zone(**payload)


def test_zone_id_is_stable_and_depends_on_mid_and_cfg():
    first = _zone()
    second = _zone()
    assert first.zone_id == second.zone_id
    assert first.zone_id == zone_id_for("ADBE", "support", 100.0, 100.2, "cfg-a")
    assert _zone(engine_cfg="cfg-b").zone_id != first.zone_id
    assert _zone(side="resistance").zone_id != first.zone_id
    assert first.tf == ""
    assert first.atr_d != first.atr_d  # default NaN


def test_explicit_zone_id_is_kept():
    zone = _zone(zone_id="given")
    assert zone.zone_id == "given"


def test_nearest_zones_are_strictly_above_and_below():
    low = _zone(low=90.0, high=91.0)
    mid = _zone(low=99.5, high=100.5)
    high = _zone(low=110.0, high=111.0, side="resistance")
    below, above = nearest_zones([high, low, mid], 100.0)
    assert below is low
    assert above is high
    # The zone that contains the price is neither target.
    assert mid not in (below, above)


def test_formation_defaults_extreme_and_measured_move():
    left = (_ts(10, 0), 100.0)
    neck = (_ts(11, 0), 110.0)
    right = (_ts(12, 0), 100.4)
    formed = Formation(
        kind="W",
        symbol="WSHAPE",
        tf="5m",
        pivots=(left, neck, right),
        neckline=(110.0, 0.0),
        invalidation=100.0,
        break_ts=_ts(13, 0),
        retest_ts=None,
        confirmed_ts=_ts(12, 15),
        as_of_ts=_ts(13, 0),
        available_at=_ts(13, 0),
        zone_id=None,
    )
    assert formed.formation_id
    assert formed.extreme[1] == 100.0
    assert formed.measured_move == pytest.approx(120.0)
    again = Formation(
        kind="W",
        symbol="WSHAPE",
        tf="5m",
        pivots=(left, neck, right),
        neckline=(110.0, 0.0),
        invalidation=100.0,
        break_ts=_ts(13, 0),
        retest_ts=None,
        confirmed_ts=_ts(12, 15),
        as_of_ts=_ts(13, 0),
        available_at=_ts(13, 0),
        zone_id=None,
    )
    assert again.formation_id == formed.formation_id


def test_signal_confluence_defaults_to_zero_and_rejects_four():
    zone = _zone()
    signal = Signal(
        symbol="ADBE",
        tf="5m",
        direction=1,
        test="A",
        zone=zone,
        formation=None,
        trigger=101.0,
        stop=99.0,
        targets={"1R": 103.0},
        expires_at=_ts(11, 0),
        components={},
        as_of_ts=_ts(),
        available_at=_ts(),
        variant_id="A-example",
    )
    assert signal.confluence == 0
    with pytest.raises(ValueError):
        Signal(
            symbol="ADBE",
            tf="5m",
            direction=1,
            test="A",
            zone=zone,
            formation=None,
            trigger=101.0,
            stop=99.0,
            targets={},
            expires_at=_ts(11, 0),
            components={},
            as_of_ts=_ts(),
            available_at=_ts(),
            variant_id="A-example",
            confluence=4,
        )


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValueError):
        _zone(as_of_ts=datetime(2024, 6, 3, 10, 0))
    # Other zones are accepted and normalized to ET.
    utc = datetime(2024, 6, 3, 14, 0, tzinfo=timezone.utc)
    zone = _zone(as_of_ts=utc, valid_from_ts=utc, available_at=utc)
    assert zone.as_of_ts.tzinfo == ET
    assert zone.as_of_ts.hour == 10
