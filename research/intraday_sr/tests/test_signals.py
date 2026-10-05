"""Test A levels stay banded to the stamp that is being evaluated."""

from __future__ import annotations

import importlib
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.intraday_sr.engine import levels_at, signals, signals_funnel
from research.intraday_sr.engine.levels import _LevelTape, _levels_at_stamp, _pivot_tape
from research.intraday_sr.engine.signals import (
    SignalFunnel,
    _StableZone,
    _ZoneSetup,
    _book_for_recompute,
    _build,
    _entry_prices,
    _match_stable_ids,
    _step,
)
from research.intraday_sr.engine.zone_cache import (
    clear_zone_cache,
    level_cfg_token,
    prefix_digest,
    prefix_hashes,
    zone_cfg_token,
)
from research.intraday_sr.engine.zones import fast_zones
from research.intraday_sr.grids import TEST_A
from research.intraday_sr.tests.fixtures.synthetic import _concat, _frame_from_closes, _volumes, _weekdays, trend_bars
from research.intraday_sr.types import ET, BarSet, EngineCfg, SignalCfg, Zone


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


def _keys(levels) -> set[tuple[str, float]]:
    return {(level.kind, round(level.price, 5)) for level in levels}


def _march_to_june():
    """Price near 100 through 21 Mar 2024, then a drift that leaves those pivots behind."""
    days = _weekdays(date(2024, 2, 1), 100)
    frames = []
    clock = np.arange(78, dtype=np.float64)
    for index, day in enumerate(days):
        closes = 100.0 + np.sin(clock / 5.0)
        if day == date(2024, 3, 21):
            closes = closes.copy()
            closes[20] = 102.2
        elif day > date(2024, 3, 21):
            closes = closes + (day - date(2024, 3, 21)).days * 0.8
        frames.append(_frame_from_closes(day, closes, symbol="SPY", volumes=_volumes(78, index)))
    return _concat(frames)


def test_stamp_levels_match_a_truncated_levels_at():
    frame = trend_bars()
    cfg = EngineCfg()
    pivots, prices = _pivot_tape("TREND", frame, cfg)
    for offset in (16 * 78 + 36, 24 * 78 + 20, len(frame) - 1):
        stamp = frame["available_at"].iloc[offset].to_pydatetime()
        prefix = frame.iloc[: offset + 1]
        direct = levels_at(BarSet(prefix), stamp, cfg)
        stamped = _levels_at_stamp("TREND", prefix, pivots, prices, stamp, cfg)
        assert _keys(stamped) == _keys(direct)


def test_march_levels_stay_candidates_when_the_window_ends_in_june(monkeypatch):
    frame = _march_to_june()
    cfg = EngineCfg()
    march = frame[frame["session"] == date(2024, 3, 21)]
    stamp = march["available_at"].iloc[23].to_pydatetime()
    assert stamp.astimezone(ET).hour == 11 and stamp.astimezone(ET).minute == 30
    prefix = frame.loc[frame["available_at"] <= stamp].reset_index(drop=True)
    direct = levels_at(BarSet(prefix), stamp, cfg)
    march_pivots = [level for level in direct if level.kind == "pivot_5m" and level.price > 101.0]
    assert march_pivots
    pivot_price = max(level.price for level in march_pivots)

    june = frame["available_at"].iloc[-1].to_pydatetime()
    assert june.astimezone(ET).date() > date(2024, 3, 21) + timedelta(days=60)
    later = levels_at(BarSet(frame), june, cfg)
    assert all(abs(level.price - pivot_price) > 0.05 for level in later)

    seen: dict[str, set[tuple[str, float]]] = {}

    def spy(levels, **kwargs):
        when = kwargs["stamp"].astimezone(ET)
        if when.date() == date(2024, 3, 21) and when.hour == 11 and when.minute == 30:
            seen["keys"] = _keys(levels)
        return fast_zones(levels, **kwargs)

    signal_engine = importlib.import_module("research.intraday_sr.engine.signals")
    monkeypatch.setattr(signal_engine, "fast_zones", spy)
    start = frame["available_at"].iloc[0].to_pydatetime()
    list(signals(BarSet(frame), start, june, cfg, _sig()))
    assert seen["keys"] == _keys(direct)
    assert ("pivot_5m", round(pivot_price, 5)) in seen["keys"]


def test_zone_cfg_hash_includes_k_and_level_hash_does_not():
    assert zone_cfg_token(EngineCfg(k_zones=3)) != zone_cfg_token(EngineCfg(k_zones=5))
    assert level_cfg_token(EngineCfg(k_zones=3)) == level_cfg_token(EngineCfg(k_zones=5))


def test_warm_cache_reuses_levels_across_signal_axes(monkeypatch):
    clear_zone_cache()
    signal_engine = importlib.import_module("research.intraday_sr.engine.signals")
    # A settings-path test reloads this module, so the class has to be the one
    # the live ``signals`` function closes over, not an earlier import.
    tape_cls = signal_engine._LevelTape
    calls = {"levels": 0, "zones": 0}
    real_levels = tape_cls.levels_at
    real_zones = signal_engine.fast_zones

    def count_levels(self, cutoff, stamp):
        calls["levels"] += 1
        return real_levels(self, cutoff, stamp)

    def count_zones(*args, **kwargs):
        calls["zones"] += 1
        return real_zones(*args, **kwargs)

    monkeypatch.setattr(tape_cls, "levels_at", count_levels)
    monkeypatch.setattr(signal_engine, "fast_zones", count_zones)
    frame = trend_bars()
    bars = BarSet(frame)
    start = frame["available_at"].iloc[0].to_pydatetime()
    end = frame["available_at"].iloc[-1].to_pydatetime()
    cfg = EngineCfg(k_zones=3)
    primary = _sig()
    list(signals(bars, start, end, cfg, primary))
    levels_once = calls["levels"]
    zones_once = calls["zones"]
    assert levels_once > 0 and zones_once > 0
    other = SignalCfg(
        oscillator=primary.oscillator,
        rvol_min=primary.rvol_min,
        entry_tf=primary.entry_tf,
        target="2R" if primary.target != "2R" else "1R",
        k_confirm=3,
        variant_id="cache-probe",
        test="A",
    )
    list(signals(bars, start, end, cfg, other))
    assert calls["levels"] == levels_once
    assert calls["zones"] == zones_once
    list(signals(bars, start, end, EngineCfg(k_zones=5), primary))
    assert calls["levels"] == levels_once
    assert calls["zones"] > zones_once


def test_funnel_counts_cover_touch_hold_arm_and_emit():
    clear_zone_cache()
    frame = trend_bars()
    bars = BarSet(frame)
    start = frame["available_at"].iloc[0].to_pydatetime()
    end = frame["available_at"].iloc[-1].to_pydatetime()
    cfg = EngineCfg(k_zones=3)
    sig = _sig()
    funnel = signals_funnel(bars, start, end, cfg, sig)
    assert funnel.emits == len(list(signals(bars, start, end, cfg, sig)))
    assert funnel.emits >= 1
    assert funnel.touches >= funnel.holds >= funnel.arms >= funnel.k_confirm_pass
    # A zone can be absent on the touch bar and present on the arm bar, so
    # the forward tally is not a ceiling on emits. This variant is 1R.
    assert funnel.build_fail_no_ahead_zone == 0
    assert funnel.build_fail_zone_lt_1R == 0


def _arm_zone(**overrides) -> Zone:
    stamp = datetime(2024, 6, 3, 10, 0, tzinfo=ET)
    payload = dict(
        symbol="SPY",
        low=99.0,
        high=99.4,
        side="support",
        score=0.5,
        components={"touches": 0.2},
        kinds=("pivot_5m",),
        as_of_ts=stamp,
        valid_from_ts=stamp,
        available_at=stamp,
        engine_cfg="cfg-a",
        tf="5m",
        atr_d=2.0,
    )
    payload.update(overrides)
    return Zone(**payload)


def _arm_cfg(target: str) -> SignalCfg:
    return SignalCfg(
        oscillator="rsi14_30_70",
        rvol_min=1.5,
        entry_tf="5m",
        target=target,
        k_confirm=0,
        variant_id=f"A-K3-rsi14_30_70-rvol1.5-5m-{target}-k0",
        test="A",
    )


def _arm_step(zones: list[Zone], target: str) -> tuple[list, SignalFunnel]:
    """One support touch that holds on bar 0 and arms on bar 1."""
    stamp = datetime(2024, 6, 3, 10, 5, tzinfo=ET)
    opens = np.array([99.3, 99.7], dtype=np.float64)
    highs = np.array([100.0, 99.9], dtype=np.float64)
    lows = np.array([99.2, 99.55], dtype=np.float64)
    closes = np.array([99.5, 99.7], dtype=np.float64)
    zeros = np.zeros(2, dtype=np.float64)
    funnel = SignalFunnel()
    found = _step(
        index=1,
        zones=zones,
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        available=[stamp, stamp],
        osc=zeros,
        hist=zeros,
        macd_line=zeros,
        macd_signal=zeros,
        volume_ratio=zeros,
        factors=np.ones(2, dtype=np.float64),
        oversold=30.0,
        overbought=70.0,
        cfg=EngineCfg(),
        sig=_arm_cfg(target),
        width=timedelta(minutes=5),
        setups={},
        funnel=funnel,
    )
    return found, funnel


def test_fixed_r_emits_without_an_opposite_zone_and_zone_target_does_not():
    support = _arm_zone()
    near = _arm_zone(side="resistance", low=100.2, high=100.5)
    # 1R and 2R are measured from the stop. A missing opposite zone is not a skip.
    for target in ("1R", "2R"):
        found, funnel = _arm_step([support], target)
        assert len(found) == 1
        assert set(found[0].targets) == {"1R", "2R"}
        assert funnel.build_fail_no_ahead_zone == 0
        assert funnel.build_fail_zone_lt_1R == 0
        with_near, near_funnel = _arm_step([support, near], target)
        assert len(with_near) == 1
        assert with_near[0].targets["zone"] == near.low
        assert near_funnel.build_fail_no_ahead_zone == 0
        assert near_funnel.build_fail_zone_lt_1R == 0

    missing, missing_funnel = _arm_step([support], "zone")
    assert missing == []
    assert missing_funnel.build_fail_no_ahead_zone == 1
    assert missing_funnel.build_fail_zone_lt_1R == 0

    close_zone, close_funnel = _arm_step([support, near], "zone")
    assert close_zone == []
    assert close_funnel.build_fail_no_ahead_zone == 0
    assert close_funnel.build_fail_zone_lt_1R == 1

    # Direct gate: room under 1R is a skip only for the zone target.
    stamp = datetime(2024, 6, 3, 10, 5, tzinfo=ET)
    highs = np.array([100.0, 99.9], dtype=np.float64)
    lows = np.array([99.2, 99.55], dtype=np.float64)
    closes = np.array([99.5, 99.7], dtype=np.float64)
    signal, reason = _build(
        support,
        0,
        1,
        highs,
        lows,
        closes,
        [stamp, stamp],
        np.ones(2),
        {"oscillator": 0.0, "macd": 0.0, "rvol": 0.0},
        [support, near],
        EngineCfg(),
        _arm_cfg("zone"),
        timedelta(minutes=5),
    )
    assert signal is None and reason == "zone_lt_1r"


def test_entry_prices_keep_the_unclamped_extremes():
    frame = pd.DataFrame(
        {"high": [100.0], "low": [100.0], "high_unclamped": [120.0], "low_unclamped": [80.0]}
    )
    priced = _entry_prices(frame)
    assert float(priced["high"].iloc[0]) == 120.0
    assert float(priced["low"].iloc[0]) == 80.0


def test_level_tape_matches_levels_at_on_a_spy_window():
    """The per-stamp builder and levels_at agree on a real SPY slice."""
    root = Path("/tmp/d2data")
    if not (root / "part_0000_SPY.parquet").is_file():
        pytest.skip("SPY cache is not on this machine")
    from research.intraday_sr.data.adjust import load_adj_factors
    from research.intraday_sr.data.cache import load_symbol

    factors = load_adj_factors(root / "adj_factors" / "adj_factors.parquet")
    frame, _report = load_symbol(root, "SPY", factors=factors, start=date(2019, 1, 2), end=date(2019, 6, 28))
    cfg = EngineCfg(k_zones=3)
    plan = _LevelTape("SPY", frame, cfg)
    clear_zone_cache()
    stamps = []
    if "bad_print" in frame.columns:
        flagged = frame.index[frame["bad_print"].to_numpy(dtype=bool)]
        if len(flagged):
            stamps.append(frame["available_at"].iloc[int(flagged[0])].to_pydatetime())
    for month in (4, 5, 6):
        day = frame.loc[pd.to_datetime(frame["ts"]).dt.month == month].iloc[30]
        stamps.append(day["available_at"].to_pydatetime())
    bars = BarSet(frame)
    for stamp in stamps:
        cutoff = int((frame["available_at"] <= stamp).sum())
        fast = plan.levels_at(cutoff, stamp)
        direct = levels_at(bars, stamp, cfg)
        assert [(level.kind, level.price, level.available_at) for level in fast] == [
            (level.kind, level.price, level.available_at) for level in direct
        ]


def test_prefix_hash_changes_when_ohlc_is_swapped_or_volume_changes():
    frame = trend_bars().head(40).reset_index(drop=True)
    original = prefix_digest(prefix_hashes(frame), len(frame) - 1)
    swapped = frame.copy()
    swapped.loc[3, ["open", "close"]] = swapped.loc[3, ["close", "open"]].to_numpy()
    assert prefix_digest(prefix_hashes(swapped), len(swapped) - 1) != original
    louder = frame.copy()
    louder.loc[5, "volume"] = float(louder.loc[5, "volume"]) + 1.0
    assert prefix_digest(prefix_hashes(louder), len(louder) - 1) != original
    short = frame.iloc[:17].reset_index(drop=True)
    assert prefix_digest(prefix_hashes(short), len(short) - 1) == prefix_digest(prefix_hashes(frame), len(short) - 1)


def _id_zone(low: float, high: float, score: float, *, side: str = "support", minute: int = 0) -> Zone:
    stamp = datetime(2024, 6, 3, 10, minute, tzinfo=ET)
    return Zone(
        symbol="SPY",
        low=low,
        high=high,
        side=side,  # type: ignore[arg-type]
        score=score,
        components={"touches": score},
        kinds=("hvn",),
        as_of_ts=stamp,
        valid_from_ts=stamp,
        available_at=stamp,
        engine_cfg="cfg-a",
        tf="5m",
        atr_d=2.0,
    )


def test_overlap_of_exactly_half_the_narrower_width_matches():
    previous = [_StableZone(_id_zone(0.0, 100.0, 0.4), "keep")]
    exact = _id_zone(50.0, 150.0, 0.4)
    missed = _id_zone(51.0, 151.0, 0.4)
    assert _match_stable_ids(previous, [exact])[0].stable_id == "keep"
    fresh = _match_stable_ids(previous, [missed])[0]
    assert fresh.stable_id == missed.zone_id
    assert fresh.stable_id != "keep"


def test_stable_match_is_one_to_one_and_deterministic():
    shared = _StableZone(_id_zone(0.0, 100.0, 0.5), "only")
    wide = _id_zone(0.0, 100.0, 0.1)
    partial = _id_zone(40.0, 140.0, 0.9)
    first = _match_stable_ids([shared], [wide, partial])
    second = _match_stable_ids([shared], [wide, partial])
    assert [item.stable_id for item in first] == [item.stable_id for item in second]
    assert first[0].stable_id == "only"
    assert first[1].stable_id == partial.zone_id

    # Equal overlap: the higher previous score wins, then the lower low.
    low_score = _StableZone(_id_zone(0.0, 100.0, 0.2), "low-score")
    high_score = _StableZone(_id_zone(0.0, 100.0, 0.9), "high-score")
    scored = _match_stable_ids([low_score, high_score], [_id_zone(0.0, 100.0, 0.3)])
    assert scored[0].stable_id == "high-score"
    higher_low = _StableZone(_id_zone(1.0, 101.0, 0.5), "higher-low")
    lower_low = _StableZone(_id_zone(0.0, 100.0, 0.5), "lower-low")
    tied = _match_stable_ids([higher_low, lower_low], [_id_zone(0.5, 100.5, 0.5)])
    assert tied[0].stable_id == "lower-low"


def test_identity_resets_at_the_session_boundary():
    day = date(2024, 6, 3)
    nxt = date(2024, 6, 4)
    zone = _id_zone(98.0, 99.0, 0.5)
    shifted = _id_zone(98.2, 99.2, 0.6, minute=15)
    tracked, previous, setups, ids = _book_for_recompute(day, None, [], {}, [zone])
    assert ids == [zone.zone_id]
    setups[ids[0]] = _ZoneSetup()
    setups[ids[0]].block_until = 4
    tracked, previous, setups, ids = _book_for_recompute(day, tracked, previous, setups, [shifted])
    assert ids == [zone.zone_id]
    kept = ids[0]
    assert setups[kept].block_until == 4
    _tracked, _previous, cleared, fresh = _book_for_recompute(nxt, tracked, previous, setups, [shifted])
    assert fresh[0] != kept
    assert cleared == {}


def test_shifted_zone_emits_once_and_a_later_touch_can_emit_again():
    """A small shift keeps the armed setup. After the order expires, a new touch emits."""
    first = _id_zone(98.0, 99.0, 0.8)
    shifted = _id_zone(97.9, 98.7, 0.7, minute=15)
    matched = _match_stable_ids([_StableZone(first, first.zone_id)], [shifted])
    assert matched[0].stable_id == first.zone_id
    assert shifted.zone_id != first.zone_id

    n = 12
    opens = np.full(n, 99.2)
    highs = np.full(n, 99.6)
    lows = np.full(n, 99.3)
    closes = np.full(n, 99.4)
    lows[0] = 98.5
    closes[0] = 99.5
    lows[1] = 99.0
    lows[4] = 98.8
    lows[7] = 98.5
    closes[7] = 99.5
    highs[8] = 99.4
    available = [datetime(2024, 6, 3, 10, 5 * i, tzinfo=ET) for i in range(n)]
    zeros = np.zeros(n)
    cfg = EngineCfg()
    sig = _arm_cfg("1R")
    book: dict[str, _ZoneSetup] = {}

    def run(index: int, zone: Zone, stable: str) -> list:
        return _step(
            index=index,
            zones=[zone],
            opens=opens,
            highs=highs,
            lows=lows,
            closes=closes,
            available=available,
            osc=zeros,
            hist=zeros,
            macd_line=zeros,
            macd_signal=zeros,
            volume_ratio=zeros,
            factors=np.ones(n),
            oversold=30.0,
            overbought=70.0,
            cfg=cfg,
            sig=sig,
            width=timedelta(minutes=5),
            setups=book,
            stable_ids=[stable],
        )

    first_emit = run(1, first, first.zone_id)
    assert len(first_emit) == 1
    assert first_emit[0].zone.low == first.low
    assert first_emit[0].zone.high == first.high
    # The shifted zone would arm this same touch again on bar 4 if the id were new.
    assert run(4, shifted, matched[0].stable_id) == []
    later = _id_zone(97.9, 98.7, 0.7, minute=30)
    again = _match_stable_ids([matched[0]], [later])
    assert again[0].stable_id == first.zone_id
    second = run(8, later, again[0].stable_id)
    assert len(second) == 1
    assert second[0].zone.as_of_ts == later.as_of_ts
    assert second[0].available_at == available[8]


def test_a_close_beyond_the_zone_cancels_and_a_new_touch_can_emit():
    original = _id_zone(98.0, 99.0, 0.6)
    tightened = _id_zone(98.5, 99.4, 0.6, minute=15)
    matched = _match_stable_ids([_StableZone(original, original.zone_id)], [tightened])
    assert matched[0].stable_id == original.zone_id
    n = 8
    opens = np.full(n, 99.3)
    highs = np.full(n, 99.8)
    lows = np.full(n, 99.5)
    closes = np.full(n, 99.6)
    lows[0] = 98.5
    closes[0] = 99.5
    lows[3] = 99.0
    closes[2] = 98.4
    lows[5] = 98.6
    closes[5] = 99.6
    highs[6] = 99.5
    closes[6] = 99.5
    available = [datetime(2024, 6, 3, 11, 5 * i, tzinfo=ET) for i in range(n)]
    zeros = np.zeros(n)
    book: dict[str, _ZoneSetup] = {}

    def run(index: int, zone: Zone) -> list:
        return _step(
            index=index,
            zones=[zone],
            opens=opens,
            highs=highs,
            lows=lows,
            closes=closes,
            available=available,
            osc=zeros,
            hist=zeros,
            macd_line=zeros,
            macd_signal=zeros,
            volume_ratio=zeros,
            factors=np.ones(n),
            oversold=30.0,
            overbought=70.0,
            cfg=EngineCfg(),
            sig=_arm_cfg("1R"),
            width=timedelta(minutes=5),
            setups=book,
            stable_ids=[original.zone_id],
        )

    assert run(1, original) == []
    assert (0, 0) in book[original.zone_id].arm_for
    assert run(2, tightened) == []
    assert (0, 0) in book[original.zone_id].consumed
    emitted = run(6, tightened)
    assert len(emitted) == 1
    assert emitted[0].zone.high == tightened.high
