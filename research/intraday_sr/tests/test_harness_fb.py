"""SPEC v1.3.5 harness paths: --test F (per kind), --test B, R7, S3, P1, and the loud stub."""

from __future__ import annotations

import json
import pickle
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from research.intraday_sr import grids
from research.intraday_sr.harness import portfolio as P
from research.intraday_sr.harness import readout as RO
from research.intraday_sr.harness import run as R
from research.intraday_sr.harness import walkforward as W
from research.intraday_sr.harness.config import CostCfg, N_DECLARED, N_PROGRAM, RiskCfg
from research.intraday_sr.harness.fb_signals import StubEngineError, signals_for_b, signals_for_f
from research.intraday_sr.harness.guard import Guard, LookaheadError
from research.intraday_sr.harness.p1_prescreen import P1InputError, prescreen, write_report
from research.intraday_sr.harness.portfolio import FrameBarSource, simulate
from research.intraday_sr.tests._harness_stubs import Z, flat_day, idx, t
from research.intraday_sr.tests.test_harness_wf_stats_readout import _run
from research.intraday_sr.types import BarSet, Formation, Signal

PINNED = "2ef95123c015d70a1751f559f21fa1249d0b08aea272319e232f2facb120aa22"
ROOT = Path(__file__).resolve().parents[3]
DAY = "2024-03-04"


@pytest.fixture(autouse=True)
def _reset_legacy():
    P.set_legacy(False, False)
    yield
    P.set_legacy(False, False)


def _lag(tf: str) -> timedelta:
    return timedelta(minutes=15 if tf == "5m" else 45)


def make_formation(symbol: str, kind: str, form_at, tf: str = "5m", zone_id: str | None = None) -> Formation:
    last = form_at - _lag(tf)
    if kind in ("W", "IHS"):
        prices = (100.0, 110.0, 100.4)
    else:
        prices = (110.0, 100.0, 109.6)
    pivots = (
        (form_at - timedelta(minutes=120), prices[0]),
        (form_at - timedelta(minutes=80), prices[1]),
        (last, prices[2]),
    )
    return Formation(kind, symbol, tf, pivots, (100.5, 0.0), 99.0, form_at, None, form_at, form_at, form_at, zone_id)


def fsig(symbol="AAA", day=DAY, at="10:00", kind="W", variant=None, form_at=None, test=None,
         zone_id="", trigger=100.10, stop=99.50, expires="15:30"):
    av = t(day, at)
    formed = av if form_at is None else form_at
    spec = dict(variant or {})
    test = test or spec.get("test") or f"F_{kind}"
    kind = spec.get("kind") or kind
    tf = spec.get("entry_tf") or "5m"
    vid = spec.get("variant_id") or f"{test}-{tf}-1R-tol0.25"
    side = "support" if kind in ("W", "IHS") else "resistance"
    z = Z(symbol, 99.0, 99.4, side, 0.8, {"touches": 1.0}, ("pdl",), av, av, av, atr_d=2.0, tf="5m")
    zid = z.zone_id if zone_id == "" and test == "B" else (zone_id or None)
    form = make_formation(symbol, kind, formed, tf=tf, zone_id=zid)
    direction = 1 if kind in ("W", "IHS") else -1
    return Signal(symbol, tf, direction, test, z, form, trigger, stop,
                  {"1R": float("nan"), "2R": float("nan")}, t(day, expires), {}, av, av, vid, confluence=0)


def _sim(frames, sigs, target="1R"):
    return simulate(sigs, FrameBarSource(frames), RiskCfg(target=target), CostCfg(), tier_fn=lambda s, d: "T2")


def test_grid_sha_and_program_n_are_unchanged_by_skipping_a_test():
    assert grids.GRID_SHA256 == grids.grid_sha256() == PINNED
    assert N_PROGRAM == 456
    assert N_DECLARED["A"] == 192 and N_DECLARED["B"] == 192 and N_DECLARED["F"] == 48
    assert len(grids.FORMATIONS) == 48 and len(grids.TEST_B) == 192
    assert {row["test"] for row in grids.FORMATIONS} == set(R.FORMATION_TESTS)
    assert all(sum(1 for row in grids.FORMATIONS if row["test"] == k) == 12 for k in R.FORMATION_TESTS)
    assert N_PROGRAM == 456          # skipping F or B does not lower the floor


def test_expand_and_adapter_variant_counts():
    assert R.expand_test("F") == list(R.FORMATION_TESTS)
    assert R.expand_test("F_W") == ["F_W"] and R.parse_test_args(None, None) == ["A"]
    assert R.parse_test_args("A", None) == ["A"] and R.parse_test_args(None, "B") == ["B"]
    assert R.parse_test_args("F", "F") == list(R.FORMATION_TESTS)
    with pytest.raises(SystemExit):
        R.parse_test_args("A", "B")
    with pytest.raises(SystemExit):
        R.expand_test("Z")
    ad = R.S0Adapter(symbols=["AAA"])
    assert len(ad.variants("A")) == 192 and len(ad.variants("B")) == 192
    assert {k: len(ad.variants(k)) for k in R.FORMATION_TESTS} == {k: 12 for k in R.FORMATION_TESTS}


def test_kind_selection_never_sees_another_kind():
    w = _run("F_W-5m-1R-tol0.15", [0.2] * 300, 300, start="2019-01-02")
    ihs = _run("F_IHS-5m-1R-tol0.15", [0.9] * 300, 300, start="2019-01-02")
    fold = W.make_folds()[:1]
    picked_w = W.walk_forward({"F_W-5m-1R-tol0.15": w}, fold).picks[0]["variant"]
    picked_i = W.walk_forward({"F_IHS-5m-1R-tol0.15": ihs}, fold).picks[0]["variant"]
    assert picked_w.startswith("F_W-") and picked_i.startswith("F_IHS-")
    assert picked_w != picked_i


def test_s3_under_200_train_trades_is_a_non_positive_fold():
    thin = {"v": _run("v", [0.4] * 50, 50, start="2019-06-03")}
    wf = W.walk_forward(thin, W.make_folds()[:1])
    assert wf.picks[0]["variant"] is None and len(wf.oos_trades) == 0
    assert wf.picks[0]["fold_positive"] is False
    assert wf.fold_criterion["n_unselected"] == 1 and wf.fold_criterion["n_non_positive"] == 1
    fat = {"v": _run("v", [0.2] * 400, 400, start="2019-01-02")}
    ok = W.walk_forward(fat, W.make_folds()[:1])
    assert ok.picks[0]["variant"] == "v" and ok.picks[0]["fold_positive"] is True
    assert ok.fold_criterion["n_unselected"] == 0
    neg = {"v": _run("v", [-0.1] * 400, 400, start="2019-01-02")}
    bad = W.walk_forward(neg, W.make_folds()[:1])
    assert bad.picks[0]["variant"] == "v" and bad.picks[0]["fold_positive"] is False
    assert bad.fold_criterion["n_unselected"] == 0 and bad.fold_criterion["n_non_positive"] == 1


def test_nonsmoke_readout_stays_interim_and_records_formation_dsr(tmp_path):
    trades = pd.DataFrame({"session": [date(2020, 1, 2)], "r": [0.1], "pnl": [1.0]})
    daily = pd.Series([0.0001], index=pd.Index([date(2020, 1, 2)]))
    h = RO.headline(trades, daily, 456, dsr_floor=48)
    block = {"selected": {"trades": 1, "gross_mean_r": 0.2, "cost_mean_r": 0.1, "net_mean_r": 0.1},
             "pooled": {"trades": 4, "gross_mean_r": 0.2, "cost_mean_r": 0.1, "net_mean_r": 0.1}}
    crit = {"n_folds": 25, "n_positive": 0, "n_non_positive": 25, "n_unselected": 25,
            "share_positive": 0.0, "passes": False}
    inp = RO.ReadoutInputs("v1.3.1", "abc", PINNED, {"F_W": 12}, ["AAA"], headlines={"F_W": h},
                           r7={"F_W": block}, fold_83={"F_W": crit}, spec_doc="v1.3.5",
                           spec_doc_commit=R.SPEC_DOC_COMMIT_FB, engine_spec="v1.3.5", interim=True)
    txt = RO.write_readout(inp, tmp_path).read_text()
    assert "INTERIM — 1 of 33" in txt and "informational only" in txt
    assert "DSR N for this test is **48**" in txt and "gross mean R" in txt
    assert "non-positive" in txt and "cannot pass" in txt
    assert "spec_doc v1.3.5" in txt and "engine_spec v1.3.5" in txt


def test_formation_headline_uses_dsr_n_48():
    trades = pd.DataFrame({"session": [date(2020, 1, 2)], "r": [0.1], "pnl": [1.0]})
    daily = pd.Series([0.0001], index=pd.Index([date(2020, 1, 2)]))
    h = RO.headline(trades, daily, 512, dsr_floor=48)
    assert h["dsr"]["n_trials"] == 48 and h["dsr_scope_n"] == 48
    program = RO.headline(trades, daily, 100)
    assert program["dsr"]["n_trials"] == 456 and program["dsr_scope_n"] is None


def test_r7_columns_split_gross_cost_and_net():
    ov = {idx("10:05"): (100.0, 100.40, 99.90, 100.20)}
    res = _sim({"AAA": flat_day("AAA", DAY, overrides=ov)}, [fsig(expires="10:40")])
    df = R._trades_df(res)
    assert len(df) == 1
    assert df["net_R"].iloc[0] == pytest.approx(df["r"].iloc[0])
    assert df["cost_R"].iloc[0] > 0
    assert df["gross_R"].iloc[0] == pytest.approx(df["net_R"].iloc[0] + df["cost_R"].iloc[0])


def test_guard_rejects_a_formation_later_than_the_decision_bar():
    late_at = t(DAY, "10:05")
    late = fsig(form_at=late_at)
    assert late.formation.available_at == late_at and late.available_at == t(DAY, "10:00")
    guard = Guard(late_at)
    guard.check(late.formation)                       # guard.now == formation.available_at
    with pytest.raises(LookaheadError):
        guard.check(late.formation, decision_ts=late.available_at)
    with pytest.raises(LookaheadError):
        _sim({"AAA": flat_day("AAA", DAY)}, [late])
    same = fsig()
    _sim({"AAA": flat_day("AAA", DAY)}, [same])       # formation.available_at == the decision bar


def test_halfday_cutoff_and_subtick_target_gap_apply_to_f_signals():
    n_half = idx("12:55") + 1
    ov = {idx("12:55"): (100.05, 100.30, 100.00, 100.20)}
    frames = {"AAA": flat_day("AAA", "2024-11-29", n=n_half, overrides=ov)}
    assert _sim(frames, [fsig(day="2024-11-29", at="12:50", expires="13:00")]).trades == []
    P.set_legacy(halfday=True)
    tr = _sim(frames, [fsig(day="2024-11-29", at="12:50", expires="13:00")]).trades
    assert len(tr) == 1 and tr[0].entry.ts.strftime("%H:%M") == "12:55"
    P.set_legacy(False, False)
    gap = {
        idx("10:00"): (100.0, 100.02, 99.95, 100.0),
        idx("10:05"): (100.0, 100.20, 99.95, 100.05),
        idx("10:10"): (100.704, 100.80, 100.70, 100.75),
    }
    tr = _sim({"AAA": flat_day("AAA", DAY, overrides=gap)}, [fsig(expires="10:30")]).trades
    assert len(tr) == 1 and tr[0].exit.reason == "target" and tr[0].exit.price == pytest.approx(100.704)
    P.set_legacy(target_gap=True)
    tr = _sim({"AAA": flat_day("AAA", DAY, overrides=gap)}, [fsig(expires="10:30")]).trades
    assert len(tr) == 1 and tr[0].exit.reason == "target" and tr[0].exit.price == pytest.approx(100.70)


def test_missing_entry_points_raise_before_any_bars_are_scanned():
    variant = next(dict(v) for v in grids.FORMATIONS if v["test"] == "F_W")
    with pytest.raises(StubEngineError, match="formation_signals"):
        signals_for_f(BarSet(pd.DataFrame()), t(DAY, "10:00"), t(DAY, "10:05"), variant)
    b = next(dict(v) for v in grids.TEST_B if v["target"] == "1R")
    with pytest.raises(StubEngineError, match="test_b_signals"):
        signals_for_b(BarSet(pd.DataFrame()), t(DAY, "10:00"), t(DAY, "10:05"), b)


class _Bars:
    smoke = True

    def __init__(self, variants, signals):
        self.days = [date(2024, 3, 5), date(2024, 6, 4)]
        self._variants = variants
        self._signals = signals
        self._frames = {s: pd.concat([flat_day(s, d.isoformat()) for d in self.days], ignore_index=True)
                        for s in ("AAA", "BBB")}

    def symbols(self):
        return ["AAA", "BBB"]

    def variants(self, test):
        return [dict(v) for v in self._variants if v.get("test", test) == test or v.get("test") == test]

    def engine_cfg(self, v):
        return "fixture"

    def bar_source(self, symbols):
        return FrameBarSource({s: self._frames[s] for s in symbols})

    def sessions(self):
        return list(self.days)

    def signals(self, symbol, v):
        return self._signals(self, symbol, v)


def _one(kind_rows, test):
    return [dict(v) for v in kind_rows if v["test"] == test][:1]


def test_stub_still_raises_when_allow_empty_is_set(tmp_path, monkeypatch):
    rows = _one(grids.FORMATIONS, "F_W") + _one(grids.FORMATIONS, "F_IHS")
    rows += _one(grids.FORMATIONS, "F_M") + _one(grids.FORMATIONS, "F_HS")

    def _call(self, symbol, v):
        fr = self._frames[symbol]
        return signals_for_f(BarSet(fr), fr["available_at"].iloc[0].to_pydatetime(),
                             fr["available_at"].iloc[-1].to_pydatetime(), v)

    monkeypatch.setattr(R, "load_adapter", lambda spec, _ad=_Bars(rows, _call): _ad)
    with pytest.raises(StubEngineError, match="formation_signals"):
        R.main(["--test", "F", "--allow-empty", "--tag", "stub", "--out", str(tmp_path), "--workers", "1",
                "--ledger", str(tmp_path / "ledger")])


def test_real_empty_formations_abort_unless_allow_empty(tmp_path, monkeypatch):
    rows = _one(grids.FORMATIONS, "F_W") + _one(grids.FORMATIONS, "F_IHS")
    rows += _one(grids.FORMATIONS, "F_M") + _one(grids.FORMATIONS, "F_HS")

    def _empty(*_a, **_k):
        return iter(())

    monkeypatch.setattr("research.intraday_sr.engine.formations.formation_signals", _empty, raising=False)

    def _call(self, symbol, v):
        fr = self._frames[symbol]
        return signals_for_f(BarSet(fr), fr["available_at"].iloc[0].to_pydatetime(),
                             fr["available_at"].iloc[-1].to_pydatetime(), v)

    monkeypatch.setattr(R, "load_adapter", lambda spec, _ad=_Bars(rows, _call): _ad)
    with pytest.raises(SystemExit, match="zero formations"):
        R.main(["--test", "F", "--tag", "empty", "--out", str(tmp_path), "--workers", "1"])
    R.main(["--test", "F", "--allow-empty", "--tag", "empty-ok", "--out", str(tmp_path), "--workers", "1",
            "--ledger", str(tmp_path / "ledger")])
    manifest = json.loads((tmp_path / "empty-ok" / "manifest.json").read_text())
    assert manifest["run_kind"] == "smoke" and manifest["spec_doc"] == "v1.3.5"


def _quarter_days():
    out = []
    for p in pd.period_range("2020Q1", "2026Q1", freq="Q"):
        d = p.start_time.date()
        while d.weekday() != 1:
            d += timedelta(days=1)
        out.append(d)
    return out


def _sample_formations(n=2):
    buckets = {k: [] for k in R.FORMATION_TESTS}
    for row in grids.FORMATIONS:
        if row["target"] == "1R" and len(buckets[row["test"]]) < n:
            buckets[row["test"]].append(dict(row))
    return [v for k in R.FORMATION_TESTS for v in buckets[k]]


class _InjectedF(_Bars):
    def __init__(self):
        super().__init__(_sample_formations(2), None)
        self.days = _quarter_days()
        self._frames = {s: self._frame(s) for s in ("AAA", "BBB")}

    def _frame(self, s):
        parts = []
        for d in self.days:
            parts.append(flat_day(s, d.isoformat(), overrides={idx("10:05"): (100.0, 100.40, 99.90, 100.20)}))
        return pd.concat(parts, ignore_index=True)

    def variants(self, test):
        return [dict(v) for v in self._variants if v["test"] == test]

    def signals(self, symbol, v):
        return [fsig(symbol, d.isoformat(), variant=v, expires="10:40") for d in self.days]


def test_end_to_end_test_f_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "load_adapter", lambda spec, _ad=_InjectedF(): _ad)
    ledger = tmp_path / "PROGRAM_LEDGER"
    R.main(["--test", "F", "--tag", "e2e", "--out", str(tmp_path), "--workers", "1", "--ledger", str(ledger)])
    out = tmp_path / "e2e"
    m = json.loads((out / "manifest.json").read_text())
    assert m["spec_doc"] == "v1.3.5" and m["spec_doc_commit"] == R.SPEC_DOC_COMMIT_FB
    assert m["engine_spec"] == "v1.3.5" and m["spec_version"] == "v1.3.1"
    assert m["grid_sha256"] == PINNED and m["dsr_n"] == 456
    assert m["dsr_by_test"] == {k: 48 for k in R.FORMATION_TESTS}
    assert m["run_kind"] == "smoke" and str(m["ledger"]).endswith("ledger_smoke")
    assert not ledger.exists()
    txt = (out / "READOUT.md").read_text()
    assert "PIPELINE SMOKE TEST" in txt and "informational only" in txt
    assert "gross mean R" in txt and "mean cost R" in txt and "net mean R" in txt
    assert "DSR N for this test is **48**" in txt and "non-positive" in txt
    assert "FINDING" in txt and "cannot pass" in txt
    for kind in R.FORMATION_TESTS:
        assert f"## Test {kind}:" in txt
        trades = pd.read_parquet(out / f"trades_{kind}.parquet")
        assert {"gross_R", "cost_R", "net_R"} <= set(trades.columns)
        assert len(trades) and (trades["net_R"] == trades["r"]).all()
        wf = json.loads((out / f"wf_{kind}.json").read_text())
        ids = {v["variant_id"] for v in _sample_formations(2) if v["test"] == kind}
        for pick in wf["picks"]:
            assert pick["variant"] is None or pick["variant"] in ids
        assert wf["fold_criterion"]["n_unselected"] >= 1
    parts = sorted((out / "ledger_smoke").glob("part-*.parquet"))
    logged = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    assert set(logged["test"]) == set(R.FORMATION_TESTS)
    assert set(logged["grid_family"]) == {"formations"}
    assert logged.apply(lambda r: str(r["variant_id"]).startswith(str(r["test"]) + "-"), axis=1).all()


class _InjectedB(_Bars):
    def __init__(self):
        rows = [dict(v) for v in grids.TEST_B if v["target"] == "1R" and v["entry_tf"] == "5m"][:2]
        super().__init__(rows, None)
        self._variants = rows
        hit = {idx("10:05"): (100.0, 100.40, 99.90, 100.20)}
        self._frames = {s: pd.concat([flat_day(s, d.isoformat(), overrides=hit) for d in self.days], ignore_index=True)
                        for s in ("AAA", "BBB")}

    def variants(self, test):
        assert test == "B"
        return [dict(v) for v in self._variants]

    def signals(self, symbol, v):
        return [fsig(symbol, d.isoformat(), variant=v, kind="W", test="B", expires="10:40") for d in self.days]


def test_end_to_end_test_b_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "load_adapter", lambda spec, _ad=_InjectedB(): _ad)
    R.main(["--test", "B", "--tag", "b", "--out", str(tmp_path), "--workers", "1",
            "--ledger", str(tmp_path / "PROGRAM_LEDGER")])
    out = tmp_path / "b"
    m = json.loads((out / "manifest.json").read_text())
    assert m["engine_spec"] == "v1.3.5" and m["spec_doc"] == "v1.3.5"
    assert m["spec_version"] == "v1.3.1" and m["dsr_n"] == 456
    assert m["dsr_by_test"]["B"] == 456 and m["dsr_by_test"]["B"] != 48
    txt = (out / "READOUT.md").read_text()
    assert "PIPELINE SMOKE TEST" in txt and "informational only" in txt
    trades = pd.read_parquet(out / "trades_B.parquet")
    assert {"gross_R", "cost_R", "net_R"} <= set(trades.columns) and len(trades)
    parts = sorted((out / "ledger_smoke").glob("part-*.parquet"))
    logged = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    assert set(logged["test"]) == {"B"} and set(logged["grid_family"]) == {""}


def _p1_frame():
    days = list(pd.bdate_range("2020-01-02", periods=30).date)
    rows, forms = [], []
    for d in days:
        touch = t(d.isoformat(), "10:00")
        rows.append({"session": d, "symbol": "AAA", "direction": 1, "touch_ts": touch, "zone_id": "Z1",
                     "tf": "5m", "gross_R": -0.4, "net_R": -1.0, "r": -1.0, "path": "oos_exact",
                     "guardrail": "d2+w5"})
        forms.append(make_formation("AAA", "W", touch + timedelta(minutes=15), zone_id="Z1"))
    rows.append({"session": days[0], "symbol": "AAA", "direction": 1, "touch_ts": t(days[0].isoformat(), "10:00"),
                 "zone_id": "Z9", "tf": "5m", "gross_R": 2.0, "net_R": 2.0, "r": 2.0, "path": "oos_exact",
                 "guardrail": "d2+w5"})
    rows.append({**rows[0], "path": "dev", "net_R": 3.0, "r": 3.0, "gross_R": 3.0})
    rows.append({**rows[0], "guardrail": "d2+w6", "net_R": 3.0, "r": 3.0, "gross_R": 3.0})
    return pd.DataFrame(rows), forms


def test_p1_on_synthetic_fixtures(tmp_path):
    trades, forms = _p1_frame()
    a = prescreen(trades, forms)
    b = prescreen(trades, forms)
    assert a == b and a["screened_out"] is True and a["label"] == "screened out by P1"
    assert a["net_mean_r"]["hi"] < 0 and a["gross_mean_r"]["mean"] is not None
    assert a["n_filtered"] == 30 and a["n_oos"] == 31          # wrong zone stays in the pool, not the filter
    assert a["pivot_tol"] == 0.25 and a["bootstrap"]["seed"] == 20260925
    assert prescreen(trades, []).get("label") == "no filtered trades"
    assert prescreen(trades, []).get("screened_out") is False
    with pytest.raises(P1InputError):
        prescreen(trades, forms, pivot_tol=0.15)
    _jp, md = write_report(a, tmp_path)
    assert "can never count toward a pass" in md.read_text()
    with pytest.raises(SystemExit, match="formations_at"):
        from research.intraday_sr.harness.p1_prescreen import main
        main([str(tmp_path / "missing.parquet"), "--out", str(tmp_path / "no")])
    pq = tmp_path / "trades.parquet"
    trades.to_parquet(pq)
    blob = tmp_path / "forms.pkl"
    blob.write_bytes(pickle.dumps(forms))
    from research.intraday_sr.harness.p1_prescreen import main
    got = main([str(pq), "--out", str(tmp_path / "rep"), "--formations", str(blob)])
    saved = json.loads((tmp_path / "rep" / "p1.json").read_text())
    assert got["label"] == saved["label"] == "screened out by P1"


def test_launch_scripts_stamp_v135():
    for name in ("FULL_TEST_F_COMMAND.sh", "FULL_TEST_B_COMMAND.sh"):
        txt = (ROOT / "runs" / "smoke_integ" / name).read_text()
        assert "v1.3.5" in txt and R.SPEC_DOC_COMMIT_FB in txt and "engine_spec" in txt
        assert "GRID_SHA256" in txt and "N_PROGRAM" in txt and "--keep-signals" in txt
