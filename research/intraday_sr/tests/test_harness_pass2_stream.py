"""Streaming pass 2: compact columnar spill + deterministic cross-symbol merge.

Acceptance (Architect): (1) the merge order is explicit and deterministic: available_at, then symbol, then the
signal's order within its symbol; (2)/(3) simulation output from the compact loader is identical to the legacy
full-object path (here on fixtures; the real-spill SPY x192 and SPY/QQQ/IWM hash checks live in the run report)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from research.intraday_sr.harness import run as R
from research.intraday_sr.harness import spill as SP
from research.intraday_sr.harness.config import PRIMARY, CostCfg, RiskCfg
from research.intraday_sr.harness.portfolio import simulate
from research.intraday_sr.tests._harness_stubs import sig
from research.intraday_sr.tests.test_harness_run_chunked import SYMS, FixtureAdapter, _h, _run

SIM_FIELDS = ("symbol", "available_at", "expires_at", "direction", "trigger", "stop", "variant_id")
ZONE_FIELDS = ("symbol", "low", "high", "score", "atr_d", "available_at", "zone_id")


def _mixed(symbol: str, day: str, k: int = 0) -> list:
    """Several signals per symbol: out of time order, two at the same available_at (different triggers), one with
    no zone target (targets keys 1R/2R only must survive) and a shared Zone object."""
    out = [sig(symbol=symbol, day=day, at="10:15", trigger=100.20, variant_id=f"A-fx{k}"),
           sig(symbol=symbol, day=day, at="10:00", trigger=100.10, variant_id=f"A-fx{k}"),
           sig(symbol=symbol, day=day, at="10:00", trigger=100.12, score=0.9, variant_id=f"A-fx{k}"),
           sig(symbol=symbol, day=day, at="09:55", direction=-1, trigger=99.4, stop=100.3, zlo=99.6, zhi=99.9,
               zone_target=98.0, variant_id=f"A-fx{k}")]
    from research.intraday_sr.types import Signal
    s = out[1]
    out.append(Signal(s.symbol, s.tf, s.direction, s.test, s.zone, None, 100.11, s.stop, {"1R": 101.0, "2R": 102.0},
                      s.expires_at, dict(s.components), s.as_of_ts, s.available_at, s.variant_id))
    return out


def _legacy_order(sigs_by_sym: dict, symbols: list) -> list:
    flat = [s for sym in symbols for s in sigs_by_sym[sym]]
    return sorted(flat, key=lambda s: (s.available_at, s.symbol))          # what simulate() does (stable)


def _key(s):
    return (s.symbol, s.available_at, float(s.trigger), int(s.direction))


def test_compact_roundtrip_keeps_every_field_the_simulator_reads(tmp_path):
    sigs = _mixed("AAA", "2024-03-04")
    SP.write_compact(tmp_path, "A-fx0", "AAA", sigs)
    got = SP.read_compact(tmp_path, "A-fx0", ["AAA"])
    want = sorted(sigs, key=lambda s: s.available_at)                     # stable: emit order kept on ties
    assert len(got) == len(want)
    for g, w in zip(got, want):
        for f in SIM_FIELDS:
            assert getattr(g, f) == getattr(w, f), f
        assert type(g.trigger) is float and type(g.direction) is int
        assert list(g.targets) == list(w.targets)
        assert all(pd.isna(g.targets[k]) and pd.isna(w.targets[k]) or g.targets[k] == w.targets[k] for k in w.targets)
        for f in ZONE_FIELDS:
            assert getattr(g.zone, f) == getattr(w.zone, f), f
        assert g.available_at.date() == w.available_at.date()
    assert sum(1 for g in got if list(g.targets) == ["1R", "2R"]) == 1     # a zone-less target map stays zone-less


def test_us_is_exact_for_datetime_and_timestamp():
    from research.intraday_sr.types import ET
    d = datetime(2021, 11, 5, 15, 55, 0, 123456, tzinfo=ET)
    assert SP._dt(SP._us(d), {}) == d
    assert SP._us(pd.Timestamp(d)) == SP._us(d)
    with pytest.raises(ValueError):
        SP._us(datetime(2021, 1, 1))
    with pytest.raises(ValueError):
        SP._us(pd.Timestamp("2021-01-04 10:00:00.000000001", tz="America/New_York"))


@pytest.mark.parametrize("symbols", [["AAA", "BBB", "CCC"], ["CCC", "AAA", "BBB"]])
def test_merge_order_is_available_at_then_symbol_then_within_symbol_order(tmp_path, symbols):
    by = {s: _mixed(s, "2024-03-04") + _mixed(s, "2024-03-05") for s in SYMS}
    for s in SYMS:
        SP.write_compact(tmp_path, "A-fx0", s, by[s])
    got = SP.read_compact(tmp_path, "A-fx0", symbols)
    # explicit spec of the order: (available_at, symbol, index within the symbol's emitted list)
    spec = sorted(((s.available_at, s.symbol, i), s) for sym in SYMS for i, s in enumerate(by[sym]))
    assert [_key(g) for g in got] == [_key(s) for _, s in spec]
    # deterministic: independent of the order symbols are listed in, and equal to simulate()'s legacy order
    assert [_key(g) for g in got] == [_key(s) for s in _legacy_order(by, ["AAA", "BBB", "CCC"])]
    ties = [(_key(g)) for g in got if g.symbol == "AAA" and g.available_at.strftime("%H:%M") == "10:00"]
    assert [t[2] for t in ties[:3]] == [100.10, 100.12, 100.11]            # within-symbol emit order on equal stamps


def _trade_rows(res):
    rows = []
    for t, m in zip(res.trades, res.meta):
        rows.append((t.signal.symbol, t.signal.available_at, t.entry.ts, t.entry.price, t.entry.qty, t.entry.cost,
                     t.exit.ts, t.exit.price, t.exit.reason, t.exit.cost, t.r, t.pnl, t.variant_id,
                     tuple(sorted((k, str(v)) for k, v in m.items()))))
    return rows


@pytest.mark.parametrize("target", ["1R", "2R", "zone"])
def test_simulation_identical_old_vs_compact_on_multi_symbol_fixture(tmp_path, target):
    ad = FixtureAdapter()
    src = ad.bar_source(SYMS)
    by = {}
    for s in SYMS:
        base = ad.signals(s, {"variant_id": "A-fx0"})
        extra = [x for d in ad.days[::5] for x in _mixed(s, d.isoformat())]
        by[s] = base + extra
        SP.write_compact(tmp_path, "A-fx0", s, by[s])
    legacy = [x for s in SYMS for x in by[s]]                              # legacy pass-2 list (symbol order)
    compact = SP.read_compact(tmp_path, "A-fx0", SYMS)
    risk = PRIMARY.apply(RiskCfg(target=target))
    a = simulate(legacy, src, risk, CostCfg(), sessions=ad.days)
    b = simulate(compact, src, risk, CostCfg(), sessions=ad.days)
    assert len(a.trades) > 0
    assert _trade_rows(a) == _trade_rows(b)
    pd.testing.assert_series_equal(a.daily, b.daily)
    assert a.counters == b.counters
    pd.testing.assert_frame_equal(a.sessions, b.sessions)
    a2 = simulate([x for x in legacy if x.available_at.year == 2022], src, risk, CostCfg(), sessions=ad.days[36:48],
                  window_starts={ad.days[36]})
    b2 = simulate([x for x in compact if x.available_at.year == 2022], src, risk, CostCfg(), sessions=ad.days[36:48],
                  window_starts={ad.days[36]})
    assert _trade_rows(a2) == _trade_rows(b2)


@pytest.fixture(autouse=True)
def _restore_loader(monkeypatch):
    monkeypatch.setattr(R, "load_adapter", R.load_adapter)
    yield


def test_ledger_rows_compact_vs_legacy_pickle_objects_vs_no_chunk(tmp_path):
    legacy, _ = _run(tmp_path, "nochunk", "--workers", "1", "--no-chunk")
    objects, _ = _run(tmp_path, "objects", "--workers", "1", "--spill-format", "pickle", "--pass2-loader", "objects",
                     "--keep-signals")
    compact, _ = _run(tmp_path, "compact", "--workers", "2", "--pass2-workers", "1", "--keep-signals")
    assert int(legacy["trades"].sum()) > 0
    pd.testing.assert_frame_equal(legacy, objects)
    pd.testing.assert_frame_equal(legacy, compact)
    sp = tmp_path / "compact" / "_signals"
    assert sorted(p.name for p in (sp / "A-fx0").iterdir()) == [f"{s}.npz" for s in SYMS]
    # a legacy pickle spill also feeds the compact loader (converted one symbol at a time)
    pk = tmp_path / "objects" / "_signals"
    assert sorted(p.name for p in (pk / "A-fx0").iterdir()) == [f"{s}.pkl" for s in SYMS]
    for v in ("A-fx0", "A-fx1", "A-fx2", "A-fx3"):
        assert [_key(x) for x in SP.read_compact(pk, v, SYMS)] == [_key(x) for x in SP.read_compact(sp, v, SYMS)]


def test_timing_records_pass2_workers_and_formats(tmp_path):
    ad = FixtureAdapter()
    R.load_adapter = lambda spec, _ad=ad: _ad
    t = R.main(["--tests", "A", "--tag", "p2w", "--out", str(tmp_path), "--workers", "2", "--pass2-workers", "1",
                "--timing-only"])
    assert t["pass2_workers"] == 1 and t["workers"] == 2
    assert t["spill_format"] == "compact" and t["pass2_loader"] == "compact"
    assert t["pass1"]["spill_bytes"] > 0


def test_s0_frame_handoff_by_file_keeps_parent_frames_empty(tmp_path, monkeypatch):
    """Pass-1 workers hand their loaded frame to the parent as a file; the parent's bar source reads each file once
    and keeps no full frame, and sessions() still covers every symbol."""
    fx = FixtureAdapter()
    worker = R.S0Adapter(cache_root=tmp_path, symbols=SYMS)
    worker._frames = {s: fx._frames[s] for s in SYMS}
    monkeypatch.setitem(R._G, "frame_dir", str(tmp_path / "_frames"))
    paths = {s: worker.symbol_payload(s) for s in SYMS}
    assert all(isinstance(p, str) and p.endswith(f"{s}.pkl") for s, p in paths.items())
    parent = R.S0Adapter(cache_root=tmp_path, symbols=SYMS)
    for s in SYMS:
        parent.adopt_symbol_payload(s, paths[s])
    src = parent.bar_source(SYMS)
    assert parent._frames == {}
    want = fx.bar_source(SYMS)
    for s in SYMS:
        for d in fx.days[:5]:
            pd.testing.assert_frame_equal(src.session_frame(s, d), want.session_frame(s, d))
    assert parent.sessions() == sorted(fx.days)
    monkeypatch.setitem(R._G, "frame_dir", None)
    assert worker.symbol_payload("AAA") is worker._frames["AAA"]            # no frame dir: in-memory handoff


def test_pass1_releases_engine_signals_after_each_spill(tmp_path, monkeypatch):
    """release_oversized_signals runs once per (variant, symbol), only after that list is on disk; clear_zone_cache
    still runs once per symbol."""
    ad = FixtureAdapter()
    R.load_adapter = lambda spec, _ad=ad: _ad
    seen, clears = [], []
    spill = tmp_path / "rel" / "_signals"

    def fake_release():
        done = sorted(str(p.relative_to(spill)) for p in spill.glob("*/*.npz"))
        seen.append(done)
    monkeypatch.setattr(R, "release_engine_signals", fake_release)
    monkeypatch.setattr(R, "clear_engine_caches", lambda: clears.append(1))
    R.main(["--tests", "A", "--tag", "rel", "--out", str(tmp_path), "--workers", "1", "--timing-only",
            "--keep-signals"])
    assert len(seen) == len(SYMS) * 4 and len(clears) == len(SYMS)
    assert [len(x) for x in seen] == list(range(1, len(SYMS) * 4 + 1))      # file k exists before release k
