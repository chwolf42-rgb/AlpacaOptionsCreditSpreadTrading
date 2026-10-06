"""GB2 review fixes: invalidation cancel, F/B gates, and the P1 join."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import pytest

from research.intraday_sr import grids
from research.intraday_sr.harness import run as R
from research.intraday_sr.harness import spill as SP
from research.intraday_sr.harness.config import CostCfg, RiskCfg
from research.intraday_sr.harness.cp4 import GrossRError, cost_and_gross
from research.intraday_sr.harness.fb_signals import FORMATION_K_ZONES, EngineContractError, engine_cfg_for_variant, signals_for_f
from research.intraday_sr.harness.fb_signals import validate_b_signal, validate_f_signal
from research.intraday_sr.harness.p1_prescreen import P1InputError, prescreen, resolve_stack_touches
from research.intraday_sr.harness.portfolio import FrameBarSource, simulate
from research.intraday_sr.tests._harness_stubs import flat_day, idx, sig, t
from research.intraday_sr.tests.test_harness_fb import fsig
from research.intraday_sr.types import BarSet

DAY = "2024-03-04"


@pytest.fixture(autouse=True)
def _fb_engine_is_v135(monkeypatch):
    """This branch has no engine/version.py. F/B runs inject Dev 2's stamp; the refusal test overrides it."""
    real = R.engine_stamp_fields

    def fake(repo):
        got = real(repo)
        got["engine_spec"] = "v1.3.5"
        got["engine_spec_source"] = "engine/version.py"
        return got

    monkeypatch.setattr(R, "engine_stamp_fields", fake)


def _vid() -> str:
    return next(v["variant_id"] for v in grids.TEST_A if v["entry_tf"] == "5m" and int(v["K"]) == 5 and v["target"] == "1R")


def _touch_fn(plan):
    from research.intraday_sr.harness.spill import _us
    from research.intraday_sr.types import BarSet as BS
    from research.intraday_sr.types import EngineCfg, SignalCfg

    def fake(bars, signal, cfg, sigcfg):
        assert isinstance(bars, BS) and isinstance(cfg, EngineCfg) and isinstance(sigcfg, SignalCfg)
        assert int(cfg.touch_window_bars) == grids.TOUCH_WINDOW_BARS
        return plan[_us(signal.available_at)]

    return fake


def _spill(tmp, signals):
    SP.write_compact(tmp / "_signals", signals[0].variant_id, signals[0].symbol, signals)
    return tmp / "_signals"


def _row(signal, **extra):
    base = {"session": date(2020, 1, 6), "symbol": signal.symbol, "direction": int(signal.direction),
            "variant_id": signal.variant_id, "signal_available_at": signal.available_at,
            "gross_R": 0.2, "net_R": 0.1, "r": 0.1, "path": "oos_exact", "guardrail": "d2+w5"}
    base.update(extra)
    return base


def test_formation_engine_cfg_passes_k_zones_5_explicitly(monkeypatch):
    from research.intraday_sr.harness import fb_signals as FB
    seen = []
    real = FB.EngineCfg

    def spy(*args, **kwargs):
        seen.append((args, dict(kwargs)))
        return real(*args, **kwargs)

    monkeypatch.setattr(FB, "EngineCfg", spy)
    captured = {}

    def fake(bars, start, end, cfg, variant):
        captured["cfg"] = cfg
        return iter(())

    monkeypatch.setattr("research.intraday_sr.engine.formations.formation_signals", fake, raising=False)
    variant = next(dict(v) for v in grids.FORMATIONS if v["test"] == "F_W")
    assert "K" not in variant
    signals_for_f(BarSet(pd.DataFrame()), t(DAY, "10:00"), t(DAY, "10:05"), variant)
    assert captured["cfg"].k_zones == FORMATION_K_ZONES == 5
    assert seen == [((), {"k_zones": 5})]
    row = next(v for v in grids.TEST_B if int(v["K"]) == 3)
    assert engine_cfg_for_variant(row).k_zones == 3


def test_zone_target_signals_without_a_zone_are_dropped(monkeypatch):
    variant = next(dict(v) for v in grids.FORMATIONS if v["test"] == "F_W" and v["target"] == "zone")
    bad = fsig(variant=variant)
    good = fsig(variant=variant, zone_target=110.0, trigger=100.2)
    monkeypatch.setattr("research.intraday_sr.engine.formations.formation_signals",
                        lambda *a, **k: iter([bad, good]), raising=False)
    out = signals_for_f(BarSet(pd.DataFrame()), t(DAY, "09:30"), t(DAY, "16:00"), variant)
    assert len(out) == 1 and out[0].trigger == pytest.approx(100.2)
    with pytest.raises(EngineContractError, match="zone.score"):
        validate_f_signal(fsig(variant=variant, score=0.8, zone_target=110.0), variant)


def test_validate_b_requires_zone_tf_and_symbol():
    variant = next(dict(v) for v in grids.TEST_B if v["target"] == "1R" and v["entry_tf"] == "5m")
    good = fsig(variant=variant, test="B")
    validate_b_signal(good, variant)
    from research.intraday_sr.tests.test_harness_fb import make_formation
    from research.intraday_sr.types import Signal
    other_form = make_formation("BBB", good.formation.kind, good.formation.available_at, tf=good.tf,
                                zone_id=str(good.zone.zone_id), invalidation=float(good.formation.invalidation))
    other = Signal(good.symbol, good.tf, good.direction, good.test, good.zone, other_form, good.trigger,
                   good.stop, dict(good.targets), good.expires_at, dict(good.components), good.as_of_ts,
                   good.available_at, good.variant_id, confluence=good.confluence)
    with pytest.raises(EngineContractError, match="formation symbol"):
        validate_b_signal(other, variant)
    wrong_tf = dict(variant)
    wrong_tf["entry_tf"] = "15m"
    with pytest.raises(EngineContractError, match="formation.tf"):
        validate_b_signal(good, wrong_tf)
    mismatched = fsig(variant=variant, test="B", zone_id="not-the-zone")
    with pytest.raises(EngineContractError, match="zone_id"):
        validate_b_signal(mismatched, variant)


def test_f_same_bar_tie_breaks_by_symbol_when_score_is_zero():
    ov = {idx("10:05"): (100.0, 100.40, 99.90, 100.20)}
    frames = {s: flat_day(s, DAY, overrides=ov) for s in ("AAA", "BBB")}
    sigs = [fsig(symbol=s, expires="10:40") for s in ("BBB", "AAA")]
    res = simulate(sigs, FrameBarSource(frames), RiskCfg(target="1R", max_concurrent=1), CostCfg(),
                   tier_fn=lambda s, d: "T2")
    assert [t.signal.symbol for t in res.trades] == ["AAA"]


def test_allow_empty_refuses_a_real_adapter(tmp_path, monkeypatch):
    class Real:
        smoke = False

        def symbols(self):
            return ["AAA"]

        def variants(self, test):
            return []

        def signals(self, symbol, variant):
            return []

        def engine_cfg(self, variant):
            return ""

        def bar_source(self, symbols):
            return FrameBarSource({})

        def sessions(self):
            return [date(2024, 3, 4)]

    monkeypatch.setattr(R, "load_adapter", lambda spec: Real())
    ledger = tmp_path / "PROGRAM_LEDGER"
    with pytest.raises(SystemExit, match="smoke/fixture"):
        R.main(["--test", "A", "--allow-empty", "--tag", "x", "--out", str(tmp_path), "--workers", "1",
                "--ledger", str(ledger)])
    assert not ledger.exists()


def test_fb_refuses_when_engine_stamp_is_not_v135(tmp_path, monkeypatch):
    def fallback(repo):
        return {"harness_commit": "h", "engine_commit": "e", "engine_tree": "t",
                "engine_spec": "v1.3.3", "engine_spec_source": "harness fallback (engine/version.py absent)"}

    monkeypatch.setattr(R, "engine_stamp_fields", fallback)

    class Ad:
        smoke = True

        def symbols(self):
            return ["AAA"]

        def variants(self, test):
            return []

        def signals(self, symbol, variant):
            return []

        def engine_cfg(self, variant):
            return ""

        def bar_source(self, symbols):
            return FrameBarSource({})

        def sessions(self):
            return [date(2024, 3, 4)]

    monkeypatch.setattr(R, "load_adapter", lambda spec: Ad())
    with pytest.raises(SystemExit, match="engine_stamp"):
        R.main(["--test", "F", "--tag", "nostamp", "--out", str(tmp_path), "--workers", "1"])


def test_mix_a_with_f_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "load_adapter", lambda spec: type("A", (), {"smoke": True, "symbols": lambda self: ["AAA"]})())
    with pytest.raises(SystemExit, match="mix"):
        R.main(["--tests", "A,F", "--tag", "mix", "--out", str(tmp_path), "--workers", "1"])


def test_every_variant_errored_aborts_even_with_allow_empty(tmp_path, monkeypatch):
    row = next(dict(v) for v in grids.FORMATIONS if v["test"] == "F_W")

    class Ad:
        smoke = True

        def symbols(self):
            return ["AAA"]

        def variants(self, test):
            return [dict(row)] if test == "F_W" else []

        def signals(self, symbol, variant):
            raise RuntimeError("detector blew up")

        def engine_cfg(self, variant):
            return "fixture"

        def bar_source(self, symbols):
            return FrameBarSource({"AAA": flat_day("AAA", DAY)})

        def sessions(self):
            return [date(2024, 3, 4)]

    monkeypatch.setattr(R, "load_adapter", lambda spec: Ad())
    with pytest.raises(SystemExit, match="every variant errored"):
        R.main(["--test", "F_W", "--allow-empty", "--tag", "boom", "--out", str(tmp_path), "--workers", "1",
                "--ledger", str(tmp_path / "L")])


def _one_r(test: str) -> list[dict]:
    rows = [v for v in grids.FORMATIONS if v["test"] == test and v["entry_tf"] == "5m" and v["target"] == "1R"]
    assert len(rows) >= 2
    return [dict(rows[0]), dict(rows[1])]


def test_invalidation_cancel_through_compact_spill(tmp_path, monkeypatch):
    """Close beyond the pattern extreme cancels. A close through the zone does not, once invalidation is set.

    Long: cancel when close < invalidation (zone low is further down). Keep when close is through the zone
    low but still above the pattern extreme. Short is the mirror: cancel when close > invalidation, keep when
    close is through the zone high but still under the pattern extreme. f_inval is that extreme, not the zone.
    """
    long_cancel, long_keep = _one_r("F_W")
    short_cancel, short_keep = _one_r("F_M")
    rows = {r["variant_id"]: r for r in (long_cancel, long_keep, short_cancel, short_keep)}

    def frame(symbol, overrides):
        return flat_day(symbol, DAY, overrides=overrides)

    # 10:00 close 99 breaks long invalidation 99.5 and not zone [90, 95].
    # The keep long's zone low is 99.8, so the same close would have cancelled on the zone.
    long_bars = {idx("10:00"): (100.0, 100.05, 98.9, 99.0), idx("10:05"): (99.0, 101.0, 98.8, 100.5)}
    # 10:00 close 102 breaks short invalidation 101 and not zone high 120.
    # The keep short's zone high is 101, so the same close would have cancelled on the zone.
    short_bars = {idx("10:00"): (103.0, 103.2, 102.4, 102.0), idx("10:05"): (102.0, 102.2, 99.4, 100.0)}

    class Ad:
        smoke = True

        def symbols(self):
            return ["AAA", "BBB"]

        def variants(self, test):
            if test == "F_W":
                return [dict(rows[long_cancel["variant_id"]]), dict(rows[long_keep["variant_id"]])]
            if test == "F_M":
                return [dict(rows[short_cancel["variant_id"]]), dict(rows[short_keep["variant_id"]])]
            return []

        def engine_cfg(self, variant):
            return "fixture"

        def bar_source(self, symbols):
            return FrameBarSource({
                "AAA": frame("AAA", long_bars),
                "BBB": frame("BBB", short_bars),
            })

        def sessions(self):
            return [date(2024, 3, 4)]

        def signals(self, symbol, variant):
            vid = variant["variant_id"]
            if vid == long_cancel["variant_id"] and symbol == "AAA":
                return [fsig(day=DAY, at="10:00", expires="10:30", variant=variant, zlo=90.0, zhi=95.0,
                             invalidation=99.5)]
            if vid == long_keep["variant_id"] and symbol == "AAA":
                return [fsig(day=DAY, at="10:00", expires="10:30", variant=variant, zlo=99.8, zhi=110.0,
                             invalidation=90.0)]
            if vid == short_cancel["variant_id"] and symbol == "BBB":
                return [fsig(symbol="BBB", day=DAY, at="10:00", expires="10:30", variant=variant, trigger=100.0,
                             stop=101.5, zlo=90.0, zhi=120.0, invalidation=101.0)]
            if vid == short_keep["variant_id"] and symbol == "BBB":
                return [fsig(symbol="BBB", day=DAY, at="10:00", expires="10:30", variant=variant, trigger=100.0,
                             stop=101.5, zlo=90.0, zhi=101.0, invalidation=110.0)]
            return []

    monkeypatch.setattr(R, "load_adapter", lambda spec, _ad=Ad(): _ad)
    R.main(["--tests", "F_W,F_M", "--tag", "inv", "--out", str(tmp_path), "--workers", "1", "--keep-signals",
            "--ledger", str(tmp_path / "L")])
    long_dev = pd.read_parquet(tmp_path / "inv" / "trades_F_W.parquet")
    long_dev = long_dev[long_dev["path"] == "dev"]
    short_dev = pd.read_parquet(tmp_path / "inv" / "trades_F_M.parquet")
    short_dev = short_dev[short_dev["path"] == "dev"]
    assert long_cancel["variant_id"] not in set(long_dev["variant_id"])
    assert long_keep["variant_id"] in set(long_dev["variant_id"])
    assert short_cancel["variant_id"] not in set(short_dev["variant_id"])
    assert short_keep["variant_id"] in set(short_dev["variant_id"])
    spilled = {
        long_cancel["variant_id"]: (99.5, "AAA"),
        long_keep["variant_id"]: (90.0, "AAA"),
        short_cancel["variant_id"]: (101.0, "BBB"),
        short_keep["variant_id"]: (110.0, "BBB"),
    }
    for vid, (level, sym) in spilled.items():
        loaded = SP.read_compact(tmp_path / "inv" / "_signals", vid, [sym])
        assert loaded[0].cancel_level == pytest.approx(level)
        assert loaded[0].formation_available_at < loaded[0].available_at
        # The spilled level is the pattern extreme, not a zone bound.
        assert loaded[0].cancel_level != pytest.approx(loaded[0].z_low)
        assert loaded[0].cancel_level != pytest.approx(loaded[0].z_high)


def test_compact_equal_formation_time_is_lookahead_not_a_logged_trial(tmp_path):
    """Equality of formation.available_at and the decision bar is lookahead on the compact path too."""
    from research.intraday_sr.harness.guard import LookaheadError
    late = fsig(form_at=t(DAY, "10:00"))
    SP.write_compact(tmp_path, late.variant_id, late.symbol, [late])
    loaded = SP.read_compact(tmp_path, late.variant_id, [late.symbol])
    assert loaded[0].formation_available_at == loaded[0].available_at
    with pytest.raises(LookaheadError, match="strictly before"):
        simulate(loaded, FrameBarSource({"AAA": flat_day("AAA", DAY)}), RiskCfg(target="1R"), CostCfg(),
                 tier_fn=lambda s, d: "T2")


def test_test_a_compact_spill_matches_direct_simulation(tmp_path, monkeypatch):
    days = [date(2024, 3, 4), date(2024, 3, 5)]
    ov = {idx("10:05"): (100.0, 100.40, 99.90, 100.20)}

    class Ad:
        smoke = True

        def symbols(self):
            return ["AAA"]

        def variants(self, test):
            return [dict(test="A", K=3, oscillator="rsi14_30_70", rvol_min=1.5, entry_tf="5m",
                         target="1R", k_confirm=0, variant_id="A-fx0")]

        def engine_cfg(self, variant):
            return "fixture"

        def bar_source(self, symbols):
            frames = {"AAA": pd.concat([flat_day("AAA", d.isoformat(), overrides=ov) for d in days], ignore_index=True)}
            return FrameBarSource(frames)

        def sessions(self):
            return list(days)

        def signals(self, symbol, variant):
            return [sig(symbol=symbol, day=d.isoformat(), variant_id=variant["variant_id"], expires="10:40")
                    for d in days]

    ad = Ad()
    monkeypatch.setattr(R, "load_adapter", lambda spec: ad)
    R.main(["--test", "A", "--tag", "abit", "--out", str(tmp_path), "--workers", "1", "--keep-signals",
            "--ledger", str(tmp_path / "L")])
    variant = ad.variants("A")[0]
    raw = ad.signals("AAA", variant)
    loaded = SP.read_compact(tmp_path / "abit" / "_signals", "A-fx0", ["AAA"])
    assert all(pd.isna(s.cancel_level) for s in loaded)
    assert all(s.formation_available_at is None for s in loaded)
    src = ad.bar_source(["AAA"])
    risk, costs = RiskCfg(target="1R"), CostCfg()
    tier = lambda s, d: "T2"
    a = simulate(raw, src, risk, costs, tier_fn=tier)
    b = simulate(loaded, src, risk, costs, tier_fn=tier)
    assert [(t.r, t.pnl, t.entry.price, t.exit.price) for t in a.trades] == \
           [(t.r, t.pnl, t.entry.price, t.exit.price) for t in b.trades]
    assert a.counters.get("cancel_zone_close", 0) == b.counters.get("cancel_zone_close", 0)
    assert b.counters.get("cancel_invalidation", 0) == 0
    file_trades = pd.read_parquet(tmp_path / "abit" / "trades_A.parquet")
    assert {"zone_id", "trigger", "stop", "expires_at", "formation_id", "invalidation",
            "formation_available_at"} <= set(file_trades.columns)


def test_gross_r_derivation_matches_trades_df_and_rejects_zero_r():
    ov = {idx("10:05"): (100.0, 100.40, 99.90, 100.20)}
    res = simulate([fsig(expires="10:40")], FrameBarSource({"AAA": flat_day("AAA", DAY, overrides=ov)}),
                   RiskCfg(target="1R"), CostCfg(), tier_fn=lambda s, d: "T2")
    df = R._trades_df(res)
    cost, gross = cost_and_gross(df["r"].iloc[0], df["pnl"].iloc[0], df["entry_cost"].iloc[0], df["exit_cost"].iloc[0])
    assert cost == pytest.approx(df["cost_R"].iloc[0])
    assert gross == pytest.approx(df["gross_R"].iloc[0])
    with pytest.raises(GrossRError):
        cost_and_gross(0.0, 1.0, 0.1, 0.1)
    with pytest.raises(GrossRError):
        cost_and_gross(1.0, 0.0, 0.1, 0.1)


def test_p1_join_accepts_same_touch_and_zone_and_rejects_the_rest(tmp_path):
    vid = _vid()
    window = grids.TOUCH_WINDOW_BARS
    happy = sig(day="2020-01-06", at="10:30", expires="11:30", variant_id=vid)
    twin = sig(day="2020-01-06", at="10:30", expires="11:30", variant_id=vid, trigger=100.4, zlo=99.6, zhi=99.9)
    # same zone bounds as happy (sig defaults), different trigger, same time: same touch must be accepted
    touch = t("2020-01-06", "10:00")
    plan = {SP._us(happy.available_at): (touch, touch)}
    # rewrite twin onto the same zone object so bounds match
    from research.intraday_sr.types import Signal
    twin = Signal(happy.symbol, happy.tf, happy.direction, happy.test, happy.zone, None, 100.4, happy.stop,
                  dict(happy.targets), happy.expires_at, dict(happy.components), happy.as_of_ts, happy.available_at, vid)
    spill = _spill(tmp_path, [happy, twin])
    got = resolve_stack_touches(pd.DataFrame([_row(happy)]), spill, stack_touch=_touch_fn(plan))
    assert len(got) == 1
    assert got.iloc[0]["touch_ts"] == got.iloc[0]["rc_ts"]
    assert got.iloc[0]["z_low"] == pytest.approx(happy.zone.low)
    assert got.iloc[0]["tf"] == "5m"

    other = tmp_path / "amb"
    wide = sig(day="2020-01-06", at="10:30", expires="11:30", variant_id=vid, trigger=100.8, zlo=90.0, zhi=91.0)
    spill2 = _spill(other, [happy, wide])
    with pytest.raises(P1InputError, match="touch_ts, z_low, z_high"):
        resolve_stack_touches(pd.DataFrame([_row(happy)]), spill2, stack_touch=_touch_fn(plan))

    missing = _spill(tmp_path / "miss", [happy])
    late = _row(happy, signal_available_at=t("2020-01-06", "10:35"))
    with pytest.raises(P1InputError, match="matched no signal"):
        resolve_stack_touches(pd.DataFrame([late]), missing, stack_touch=_touch_fn({}))

    outside = _spill(tmp_path / "out", [happy])
    rc = touch + timedelta(minutes=5 * (window + 1))
    with pytest.raises(P1InputError, match=r"allowed 0\.\."):
        resolve_stack_touches(pd.DataFrame([_row(happy)]), outside,
                              stack_touch=_touch_fn({SP._us(happy.available_at): (touch, rc)}))

    before = _spill(tmp_path / "before", [happy])
    with pytest.raises(P1InputError, match="rc_ts is before touch_ts"):
        resolve_stack_touches(pd.DataFrame([_row(happy)]), before, stack_touch=_touch_fn({
            SP._us(happy.available_at): (touch, touch - timedelta(minutes=5))}))

    at_avail = _spill(tmp_path / "at", [happy])
    stamp = happy.available_at
    with pytest.raises(P1InputError, match="not strictly before"):
        resolve_stack_touches(pd.DataFrame([_row(happy)]), at_avail,
                              stack_touch=_touch_fn({SP._us(stamp): (stamp, stamp)}))


def test_p1_zero_matches_and_nan_fail_loudly():
    trades, forms = __import__("research.intraday_sr.tests.test_harness_fb", fromlist=["_p1_frame"])._p1_frame()
    far = [forms[0]]
    # a formation on another symbol cannot match
    from research.intraday_sr.tests.test_harness_fb import make_formation
    alien = make_formation("ZZZ", "W", t("2020-01-02", "10:15"))
    with pytest.raises(P1InputError, match="matched 0"):
        prescreen(trades, [alien])
    bad = trades.copy()
    bad.loc[bad.index[0], "net_R"] = float("nan")
    with pytest.raises(P1InputError, match="not finite"):
        prescreen(bad, forms)
    with pytest.raises(P1InputError, match="path"):
        prescreen(trades.drop(columns=["path"]), forms)


def test_non_smoke_b_requires_a_clear_p1_report(tmp_path, monkeypatch):
    class Real:
        smoke = False

        def symbols(self):
            return ["AAA"]

        def variants(self, test):
            return []

        def signals(self, symbol, variant):
            return []

        def engine_cfg(self, variant):
            return ""

        def bar_source(self, symbols):
            return FrameBarSource({})

        def sessions(self):
            return [date(2024, 3, 4)]

    monkeypatch.setattr(R, "load_adapter", lambda spec: Real())
    with pytest.raises(SystemExit, match="p1-json"):
        R.main(["--test", "B", "--tag", "b", "--out", str(tmp_path), "--workers", "1"])
    report = tmp_path / "p1.json"
    report.write_text(json.dumps({"label": "screened out by P1", "screened_out": True}))
    with pytest.raises(SystemExit, match="screened_out"):
        R.main(["--test", "B", "--tag", "b2", "--out", str(tmp_path), "--workers", "1", "--p1-json", str(report)])
