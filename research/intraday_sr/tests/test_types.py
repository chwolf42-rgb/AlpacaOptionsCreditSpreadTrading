"""§3 constructors and the CP0 invariants."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from research.intraday_sr.types import (
    ET,
    EngineCfg,
    Formation,
    Signal,
    Zone,
    nearest_zones_at,
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
        tf="15m",
        atr_d=1.0,
    )
    payload.update(overrides)
    return Zone(**payload)


def _targets() -> dict[str, float]:
    return {"1R": 103.0, "2R": 105.0, "zone": 104.0}


def test_zone_id_includes_tf_and_atr_is_required():
    first = _zone()
    second = _zone()
    assert first.zone_id == second.zone_id
    assert first.zone_id == zone_id_for("ADBE", "support", 100.0, 100.2, "cfg-a", "15m")
    assert _zone(engine_cfg="cfg-b").zone_id != first.zone_id
    assert _zone(side="resistance").zone_id != first.zone_id
    assert _zone(tf="5m").zone_id != first.zone_id
    assert first.atr_d == 1.0
    with pytest.raises(ValueError):
        _zone(atr_d=float("nan"))
    with pytest.raises(ValueError):
        _zone(atr_d=0.0)
    with pytest.raises(ValueError):
        _zone(tf="1h")


def test_explicit_zone_id_is_verified():
    expected = zone_id_for("ADBE", "support", 100.0, 100.2, "cfg-a", "15m")
    assert _zone(zone_id=expected).zone_id == expected
    with pytest.raises(ValueError):
        _zone(zone_id="given")


def test_zone_bounds_and_valid_from():
    with pytest.raises(ValueError):
        _zone(low=101.0, high=100.0)
    with pytest.raises(ValueError):
        _zone(valid_from_ts=_ts(11, 0), available_at=_ts())
    with pytest.raises(ValueError):
        _zone(as_of_ts=_ts(11, 0), available_at=_ts(), valid_from_ts=_ts())


def test_nearest_zones_at_is_side_aware_and_current():
    low = _zone(low=90.0, high=91.0)
    mid = _zone(low=99.0, high=100.6)
    high = _zone(low=110.0, high=111.0, side="resistance")
    below, above = nearest_zones_at([high, low, mid], "ADBE", 100.0, _ts())
    assert below is low
    assert above is high
    assert mid not in (below, above)
    contained, nothing = nearest_zones_at([mid], "ADBE", 100.0, _ts())
    assert contained is None and nothing is None
    assert nearest_zones_at([low], "SPY", 100.0, _ts()) == (None, None)
    future = _zone(low=90.0, high=91.0, as_of_ts=_ts(11), available_at=_ts(11), valid_from_ts=_ts(11))
    assert nearest_zones_at([future], "ADBE", 100.0, _ts()) == (None, None)
    stale = _zone(low=80.0, high=81.0, as_of_ts=_ts(9), available_at=_ts(9), valid_from_ts=_ts(9))
    fresh = _zone(low=92.0, high=93.0, as_of_ts=_ts(10), available_at=_ts(10), valid_from_ts=_ts(10))
    below, _above = nearest_zones_at([stale, fresh], "ADBE", 100.0, _ts(10))
    assert below is fresh
    wrong_side = _zone(low=110.0, high=111.0, side="support")
    assert nearest_zones_at([wrong_side], "ADBE", 100.0, _ts()) == (None, None)


def _formation(**overrides) -> Formation:
    left = (_ts(10, 0), 100.0)
    neck = (_ts(11, 0), 110.0)
    right = (_ts(12, 0), 100.4)
    payload = dict(
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
    payload.update(overrides)
    return Formation(**payload)


def test_extreme_is_derived_and_measured_move_is_gone():
    formed = _formation()
    assert formed.formation_id
    assert formed.extreme == (_ts(10, 0), 100.0)
    assert not hasattr(formed, "measured_move")
    again = _formation()
    assert again.formation_id == formed.formation_id
    assert "pivot_tol_atr" in formation_source_note()
    matching = _formation(extreme=(_ts(10, 0), 100.0))
    assert matching.extreme == formed.extreme
    with pytest.raises(ValueError):
        _formation(extreme=(_ts(15, 0), 90.0))
    with pytest.raises(ValueError):
        _formation(confirmed_ts=_ts(12, 0))
    with pytest.raises(ValueError):
        _formation(retest_ts=_ts(12, 30))


def formation_source_note() -> str:
    from research.intraday_sr.types import formation_id_for

    return formation_id_for.__doc__ or ""


def test_signal_targets_and_confluence():
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
        targets=_targets(),
        expires_at=_ts(11, 0),
        components={"oscillator": 1.0, "macd": 0.0, "rvol": 1.0},
        as_of_ts=_ts(),
        available_at=_ts(),
        variant_id="A-example",
        confluence=2,
    )
    assert signal.confluence == 2
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
            targets={"1R": 103.0},
            expires_at=_ts(11, 0),
            components={},
            as_of_ts=_ts(),
            available_at=_ts(),
            variant_id="A-example",
        )
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
            targets=_targets(),
            expires_at=_ts(11, 0),
            components={},
            as_of_ts=_ts(),
            available_at=_ts(),
            variant_id="A-example",
            confluence=1,
        )


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValueError):
        _zone(as_of_ts=datetime(2024, 6, 3, 10, 0))
    utc = datetime(2024, 6, 3, 14, 0, tzinfo=timezone.utc)
    zone = _zone(as_of_ts=utc, valid_from_ts=utc, available_at=utc)
    assert zone.as_of_ts.tzinfo == ET
    assert zone.as_of_ts.hour == 10


def test_engine_cfg_rejects_a_drifted_frozen_field():
    assert EngineCfg(k_zones=3).k_zones == 3
    assert EngineCfg().max_losses_day == 2
    assert EngineCfg().max_losses_week == 5
    with pytest.raises(ValueError):
        EngineCfg(k_cluster=0.35)
    with pytest.raises(ValueError):
        EngineCfg(dev_end="2026-09-30")


def test_retest_after_the_break_is_accepted():
    formed = _formation(retest_ts=_ts(13, 0) + timedelta(minutes=5))
    assert formed.retest_ts > formed.break_ts
