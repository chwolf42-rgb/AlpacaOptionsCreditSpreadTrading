"""Caps, axes, and the canonical grid hash."""

from __future__ import annotations

import json

from research.intraday_sr.grids import (
    CAP_FORMATIONS,
    CAP_OPTIONS,
    CAP_OPTIONS_0DTE,
    CAP_TEST_A,
    CAP_TEST_B,
    COMPARISON_GUARDRAILS,
    DAILY_LOSS_STOP,
    FORMATIONS,
    GRID_SHA256,
    K_CLUSTER,
    MAX_CONCURRENT,
    MAX_ENTRIES_PER_DAY,
    MAX_PER_SYMBOL,
    N_TRIALS,
    OPTIONS,
    OPTIONS_0DTE,
    PRIMARY_GUARDRAIL,
    SPEC_VERSION,
    TEST_A,
    TEST_B,
    UNIVERSE,
    canonical_json,
    format_grid_summary,
    grid_sha256,
    load_universe_symbols,
    universe_sha256,
)
from research.intraday_sr.types import EngineCfg


def test_equity_caps_and_axes():
    assert len(TEST_A) == CAP_TEST_A == 192
    assert len(TEST_B) == CAP_TEST_B == 192
    for row in TEST_A + TEST_B:
        assert "k_cluster" not in row
        assert row["K"] in (3, 5)
        assert row["oscillator"] in ("rsi14_30_70", "stoch14_3_3_20_80")
        assert row["rvol_min"] in (1.5, 2.0)
        assert row["entry_tf"] in ("5m", "15m")
        assert row["target"] in ("1R", "2R", "zone")
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


def test_fixed_constants_and_primary_guardrail():
    assert K_CLUSTER == 0.25
    assert MAX_ENTRIES_PER_DAY == 12
    assert MAX_CONCURRENT == 4
    assert MAX_PER_SYMBOL == 1
    assert DAILY_LOSS_STOP == -0.015
    assert SPEC_VERSION == "v1.3.1"
    assert PRIMARY_GUARDRAIL == {"daily_losses": 2, "weekly_losses": 5}
    assert COMPARISON_GUARDRAILS == (
        {"name": "none"},
        {"name": "d2+w6", "daily_losses": 2, "weekly_losses": 6},
    )
    raw = canonical_json()
    document = json.loads(raw)
    assert set(document) == {
        "comparison_guardrails",
        "engine_cfg",
        "fixed",
        "formations",
        "options",
        "options_0dte",
        "primary_guardrail",
        "spec_version",
        "test_a",
        "test_b",
        "universe",
        "universe_sha256",
    }
    assert document["spec_version"] == "v1.3.1"
    assert document["primary_guardrail"] == {"daily_losses": 2, "weekly_losses": 5}
    assert document["comparison_guardrails"] == [
        {"name": "none"},
        {"name": "d2+w6", "daily_losses": 2, "weekly_losses": 6},
    ]
    assert document["universe_sha256"] == universe_sha256()
    assert document["engine_cfg"]["k_cluster"] == 0.25
    assert document["engine_cfg"]["max_losses_day"] == 2
    assert document["engine_cfg"]["max_losses_week"] == 5
    assert document["engine_cfg"]["dev_end"] == "2026-03-31"
    assert document["fixed"]["primary_guardrail"] == document["primary_guardrail"]
    from dataclasses import asdict

    assert document["engine_cfg"] == asdict(EngineCfg())
    forbidden = {"daily_losses", "weekly_losses", "guardrail", "max_losses_day", "max_losses_week", "k_cluster"}
    for row in document["test_a"] + document["test_b"] + document["formations"] + document["options"] + document["options_0dte"]:
        assert forbidden.isdisjoint(row)
    assert N_TRIALS == 450


def test_hash_is_stable_and_in_the_summary():
    assert grid_sha256() == GRID_SHA256
    assert len(GRID_SHA256) == 64
    summary = format_grid_summary()
    assert GRID_SHA256 in summary
    assert "192" in summary
    assert "options_0dte 9" in summary
    assert "N 450" in summary
    assert "primary_guardrail daily_losses=2 weekly_losses=5" in summary
    assert "v1.3.1" in summary
    assert universe_sha256() in summary


def test_trial_count_is_450():
    assert N_TRIALS == 450
    assert len(TEST_A) + len(TEST_B) + len(FORMATIONS) + len(OPTIONS) + len(OPTIONS_0DTE) == 450


def test_universe_is_loaded_from_the_json_file():
    loaded = load_universe_symbols()
    assert UNIVERSE == loaded
    assert len(UNIVERSE) == 33
    assert UNIVERSE[:3] == ("SPY", "QQQ", "IWM")
    assert "META" in UNIVERSE and "FB" not in UNIVERSE
    assert "GOOGL" in UNIVERSE and "GOOG" not in UNIVERSE
    assert "NVDA" in UNIVERSE and "BRK.B" in UNIVERSE
    assert UNIVERSE[-1] == "MRK"
    digest = universe_sha256()
    assert len(digest) == 64
    assert digest == "9a8e2f03823873f046cb1b28338fdeaa042dc36028a330628ba096e3139de5f2"
