"""Engine spec stamp stays off the frozen grid hash."""

from __future__ import annotations

from research.intraday_sr.engine.version import engine_stamp
from research.intraday_sr.grids import GRID_SHA256


def test_engine_stamp_names_v1_3_5():
    assert engine_stamp() == {"engine_spec": "v1.3.5"}


def test_grid_hash_stays_on_v1_3_1():
    assert GRID_SHA256 == "2ef95123c015d70a1751f559f21fa1249d0b08aea272319e232f2facb120aa22"
