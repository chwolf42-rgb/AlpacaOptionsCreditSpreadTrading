"""Harness follow-ups from the S0 (#20) review: target labels, signed daily stop, grids.PRIMARY_GUARDRAIL,
per-signal contract errors, expires_at semantics, same-bar loss blocking."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from research.intraday_sr.harness import config as C
from research.intraday_sr.harness.config import RiskCfg
from research.intraday_sr.harness.portfolio import FrameBarSource, SignalContractError, simulate
from research.intraday_sr.tests._harness_stubs import flat_day, idx, sig

D = "2024-03-04"


def run(frames, sigs, risk=RiskCfg()):
    return simulate(sigs, FrameBarSource(frames), risk, tier_fn=lambda s, d: "T2")


def test_target_labels_canonical_zone_alias_next_zone_unknown_rejected():
    assert C.canonical_target("zone") == "zone" and C.canonical_target("next_zone") == "zone"
    assert C.canonical_target("1R") == "1R" and C.canonical_target("2R") == "2R"
    for bad in ("3R", "Zone", "next-zone", ""):
        with pytest.raises(C.UnknownTarget):
            RiskCfg(target=bad)
    # a forged RiskCfg that bypassed __post_init__ is still rejected by the simulator
    r = RiskCfg()
    object.__setattr__(r, "target", "mid")
    ov = {idx("10:05"): (100.05, 100.30, 100.00, 100.20)}
    with pytest.raises(C.UnknownTarget):
        run({"AAA": flat_day("AAA", D, overrides=ov)}, [sig()], risk=r)


def test_zone_and_next_zone_give_same_result():
    ov = {idx("10:05"): (100.05, 100.30, 100.00, 100.20), idx("10:10"): (100.20, 101.60, 100.15, 101.50)}
    a = run({"AAA": flat_day("AAA", D, overrides=ov)}, [sig()], risk=RiskCfg(target="zone"))
    b = run({"AAA": flat_day("AAA", D, overrides=ov)}, [sig()], risk=RiskCfg(target="next_zone"))
    assert a.trades[0].exit.price == b.trades[0].exit.price == pytest.approx(101.5)


def test_daily_loss_stop_is_signed_and_matches_grids():
    assert RiskCfg().daily_loss_stop == -0.015 == C.SPEC_DAILY_LOSS_STOP
    with pytest.raises(AssertionError):
        RiskCfg(daily_loss_stop=0.015)


def test_primary_guardrail_read_from_grids_and_asserted():
    g, stop, src = C.primary_from_grids(SimpleNamespace(PRIMARY_GUARDRAIL={"max_losses_day": 2, "max_losses_week": 5},
                                                        DAILY_LOSS_STOP=-0.015))
    assert g == {"max_losses_day": 2, "max_losses_week": 5} and stop == -0.015 and src == "grids.PRIMARY_GUARDRAIL"
    assert C.primary_from_grids(SimpleNamespace(PRIMARY_GUARDRAIL=(2, 5)))[0] == g          # S0 tuple form
    assert C.primary_from_grids(SimpleNamespace())[2] == "spec"
    with pytest.raises(AssertionError):
        C.primary_from_grids(SimpleNamespace(PRIMARY_GUARDRAIL={"max_losses_day": 2, "max_losses_week": 6}))
    with pytest.raises(AssertionError):
        C.primary_from_grids(SimpleNamespace(PRIMARY_GUARDRAIL=(2, 5), DAILY_LOSS_STOP=0.015))
    assert (RiskCfg().max_losses_day, RiskCfg().max_losses_week) == (2, 5)


def test_nan_atr_d_raises_clear_per_signal_error():
    ov = {idx("10:05"): (100.05, 100.30, 100.00, 100.20)}
    s = sig(atr_d=float("nan"), variant_id="A-K5-x")
    with pytest.raises(SignalContractError, match=r"ATR_d.*A-K5-x AAA available_at=2024-03-04 10:00"):
        run({"AAA": flat_day("AAA", D, overrides=ov)}, [s])


def test_zone_target_missing_raises_per_signal():
    ov = {idx("10:05"): (100.05, 100.30, 100.00, 100.20)}
    s = replace(sig(), targets={"1R": 1.0})
    with pytest.raises(SignalContractError, match="targets\\['zone'\\]"):
        run({"AAA": flat_day("AAA", D, overrides=ov)}, [s], risk=RiskCfg(target="zone"))


def test_expires_at_is_first_dead_bar_open():
    ov = {idx("10:25"): (100.05, 100.30, 100.00, 100.20)}      # bar opens 10:25 < expires_at 10:30: live
    assert len(run({"AAA": flat_day("AAA", D, overrides=ov)}, [sig()]).trades) == 1
    ov = {idx("10:30"): (100.05, 100.30, 100.00, 100.20)}      # bar opens AT expires_at: dead
    r = run({"AAA": flat_day("AAA", D, overrides=ov)}, [sig()])
    assert len(r.trades) == 0 and r.counters.get("expired") == 1


def test_loss_at_exit_blocks_entry_in_same_bar():
    # two losses: AAA stopped at 10:10, BBB stopped at 10:15 (2nd loss -> day limit). CCC's trigger trades in the
    # SAME 10:15 bar: exits run first, so the limit blocks it in that bar.
    loss = lambda o: (o, o + 0.02, 99.30, 99.40)
    fa = flat_day("AAA", D, overrides={idx("10:05"): (100.05, 100.30, 100.00, 100.20), idx("10:10"): loss(100.0)})
    fb = flat_day("BBB", D, overrides={idx("10:05"): (100.05, 100.30, 100.00, 100.20), idx("10:15"): loss(100.0)})
    fc = flat_day("CCC", D, overrides={idx("10:15"): (100.05, 100.30, 100.00, 100.20)})
    sigs = [sig("AAA"), sig("BBB"), sig("CCC", expires="11:00")]
    r = run({"AAA": fa, "BBB": fb, "CCC": fc}, sigs)
    assert [t.signal.symbol for t in r.trades] == ["AAA", "BBB"] and all(t.r < 0 for t in r.trades)
    row = r.sessions.iloc[0]
    assert str(row["day_limit_trip_ts"].time()) == "10:15:00" and row["signals_cancelled_at_trip"] == 1
