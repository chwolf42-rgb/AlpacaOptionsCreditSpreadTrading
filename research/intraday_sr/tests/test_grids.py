"""Caps, axes, and the canonical grid hash."""

from __future__ import annotations

import json

from research.intraday_sr.grids import (
    CAP_FORMATIONS,
    CAP_OPTIONS,
    CAP_OPTIONS_0DTE,
    CAP_TEST_A,
    CAP_TEST_B,
    DAILY_LOSS_STOP,
    FORMATIONS,
    GRID_SHA256,
    K_CLUSTER,
    MAX_CONCURRENT,
    MAX_ENTRIES_PER_DAY,
    MAX_PER_SYMBOL,
    OPTIONS,
    OPTIONS_0DTE,
    TEST_A,
    TEST_B,
    UNIVERSE,
    canonical_json,
    format_grid_summary,
    grid_sha256,
)


def test_equity_caps_and_axes():
    assert len(TEST_A) == CAP_TEST_A == 192
    assert len(TEST_B) == CAP_TEST_B == 192
    for row in TEST_A + TEST_B:
        assert "k_cluster" not in row
        assert row["K"] in (3, 5)
        assert row["oscillator"] in ("rsi14_30_70", "stoch14_3_3_20_80")
        assert row["rvol_min"] in (1.5, 2.0)
        assert row["entry_tf"] in ("5m", "15m")
        assert row["target"] in ("1R", "2R", "next_zone")
        assert row["k_confirm"] in (0, 1, 2, 3)
    assert {row["k_confirm"] for row in TEST_A} == {0, 1, 2, 3}
    a_ids = {row["variant_id"][1:] for row in TEST_A}
    b_ids = {row["variant_id"][1:] for row in TEST_B}
    assert a_ids == b_ids
    assert all(row["variant_id"].startswith("A-") for row in TEST_A)
    assert all(row["variant_id"].startswith("B-") for row in TEST_B)


def test_formation_and_options_caps():
    assert len(FORMATIONS) == CAP_FORMATIONS == 48
    assert {row["kind"] for row in FORMATIONS} == {"W", "IHS", "M", "HS"}
    assert {row["pivot_tol_atr"] for row in FORMATIONS} == {0.15, 0.25}
    assert len(OPTIONS) == CAP_OPTIONS == 9
    assert len(OPTIONS_0DTE) == CAP_OPTIONS_0DTE == 9
    assert len(OPTIONS) + len(OPTIONS_0DTE) == 18
    assert {row["time_exit_et"] for row in OPTIONS_0DTE} == {"15:45"}
    assert {row["stop_pct"] for row in OPTIONS_0DTE} == {-30, -40, -50}
    assert {row["take_profit_pct"] for row in OPTIONS_0DTE} == {50, 65, 80}
    assert all(row["premium_dollars"] == 2000 for row in OPTIONS_0DTE)


def test_fixed_constants_and_no_guardrails():
    assert K_CLUSTER == 0.25
    assert MAX_ENTRIES_PER_DAY == 12
    assert MAX_CONCURRENT == 4
    assert MAX_PER_SYMBOL == 1
    assert DAILY_LOSS_STOP == -0.015
    raw = canonical_json()
    document = json.loads(raw)
    assert set(document) == {
        "fixed",
        "formations",
        "options",
        "options_0dte",
        "test_a",
        "test_b",
        "universe",
    }
    assert "d2" not in document
    assert "w5" not in document
    assert "w6" not in document
    assert "guardrail" not in raw


def test_hash_is_stable_and_in_the_summary():
    assert grid_sha256() == GRID_SHA256
    assert len(GRID_SHA256) == 64
    summary = format_grid_summary()
    assert GRID_SHA256 in summary
    assert "192" in summary
    assert "options_0dte 9" in summary
    assert "guardrails" in summary


def test_universe_is_the_v12_list():
    assert len(UNIVERSE) == 33
    assert UNIVERSE[:3] == ("SPY", "QQQ", "IWM")
    assert "META" in UNIVERSE and "FB" not in UNIVERSE
    assert "GOOGL" in UNIVERSE and "GOOG" not in UNIVERSE
    assert "NVDA" in UNIVERSE and "BRK.B" in UNIVERSE
    assert UNIVERSE[-1] == "MRK"
