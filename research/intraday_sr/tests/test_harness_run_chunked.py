"""Per-symbol chunking in harness.run: the chunked pass (one worker per symbol, variants grouped by entry_tf, engine
caches cleared between symbols) must write exactly the ledger rows of the legacy per-variant path."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

import pandas as pd
import pytest

from research.intraday_sr.harness import run as R
from research.intraday_sr.harness.portfolio import FrameBarSource
from research.intraday_sr.harness.triallog import TrialLog
from research.intraday_sr.tests._harness_stubs import flat_day, idx, sig

SYMS = ["AAA", "BBB", "CCC"]
VOLATILE = {"run_id", "created_at_ct"}


def _days() -> list[date]:
    """One session a month (first Tuesday), 2019-01 .. 2026-03: every fold window has trades."""
    out = []
    for p in pd.period_range("2019-01", "2026-03", freq="M"):
        d = p.start_time.date()
        while d.weekday() != 1:
            d += timedelta(days=1)
        out.append(d)
    return out


def _h(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


class FixtureAdapter:
    """Small deterministic adapter: 3 symbols, 4 variants over two entry TFs, 87 sessions."""
    smoke = True

    def __init__(self, fail: tuple | None = None):
        self.days = _days()
        self.fail = fail
        self.calls: list[tuple] = []
        self._frames = {s: self._frame(s) for s in SYMS}

    def _frame(self, s):
        parts = []
        for d in self.days:
            up = _h(s, d) % 3 != 0
            hit = (100.0, 101.8, 99.95, 101.6) if up else (100.0, 100.05, 99.30, 99.4)
            parts.append(flat_day(s, d.isoformat(), overrides={idx("10:00"): (100.0, 100.2, 99.95, 100.15),
                                                               idx("10:30"): hit}))
        return pd.concat(parts, ignore_index=True)

    def symbols(self):
        return list(SYMS)

    def variants(self, test):
        tfs, tg = ["15m", "5m", "15m", "5m"], ["1R", "2R", "zone", "1R"]
        return [dict(test="A", K=3 + 2 * (i // 2), oscillator="rsi14_30_70", rvol_min=1.5, entry_tf=tfs[i],
                     target=tg[i], k_confirm=i % 2, variant_id=f"A-fx{i}") for i in range(4)]

    def engine_cfg(self, v):
        return "fixture"

    def bar_source(self, symbols):
        return FrameBarSource({s: self._frames[s] for s in symbols})

    def sessions(self):
        return list(self.days)

    def signals(self, symbol, v):
        self.calls.append((symbol, v["variant_id"]))
        if self.fail == (symbol, v["variant_id"]):
            raise ValueError(f"fixture failure {symbol} {v['variant_id']}")
        k = int(v["variant_id"][-1])
        return [sig(symbol=symbol, day=d.isoformat(), variant_id=v["variant_id"])
                for d in self.days if _h(symbol, d, k) % 4 != 0]


def _run(tmp_path, tag, *extra, adapter=None):
    ad = adapter or FixtureAdapter()
    R.load_adapter = lambda spec, _ad=ad: _ad          # monkeypatched per test below
    R.main(["--tests", "A", "--tag", tag, "--out", str(tmp_path), *extra])
    df = TrialLog(tmp_path / tag / "ledger_smoke").read()
    cols = [c for c in df.columns if c not in VOLATILE]
    return df[cols].sort_values(["variant_id", "fold", "phase"]).reset_index(drop=True), ad


@pytest.fixture(autouse=True)
def _restore_loader(monkeypatch):
    monkeypatch.setattr(R, "load_adapter", R.load_adapter)
    yield


def test_chunk_order_groups_entry_tf_then_k():
    vs = FixtureAdapter().variants("A")
    order = [v["variant_id"] for v in R.chunk_order(vs)]
    assert order == ["A-fx1", "A-fx3", "A-fx0", "A-fx2"]          # 5m (K3, K5) then 15m (K3, K5)


@pytest.mark.parametrize("workers", ["1", "2"])
def test_chunked_ledger_rows_match_unchunked(tmp_path, workers):
    legacy, _ = _run(tmp_path, "legacy", "--workers", "1", "--no-chunk")
    chunked, ad = _run(tmp_path, f"chunked{workers}", "--workers", workers)
    assert len(legacy) and int(legacy["trades"].sum()) > 0
    pd.testing.assert_frame_equal(legacy, chunked)
    if workers == "1":   # in-process: each symbol is done in full, in chunk order, before the next one starts
        assert ad.calls == [(s, v) for s in SYMS for v in ["A-fx1", "A-fx3", "A-fx0", "A-fx2"]]


def test_chunked_clears_engine_cache_between_symbols(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(R, "clear_engine_caches", lambda: seen.append(len(seen)))
    ad = FixtureAdapter()
    R.load_adapter = lambda spec, _ad=ad: _ad
    R.main(["--tests", "A", "--tag", "clr", "--out", str(tmp_path), "--workers", "1", "--timing-only"])
    assert len(seen) == len(SYMS)


def test_chunked_signal_error_is_same_errored_trial(tmp_path):
    legacy, _ = _run(tmp_path, "e_legacy", "--workers", "1", "--no-chunk", adapter=FixtureAdapter(("BBB", "A-fx2")))
    chunked, _ = _run(tmp_path, "e_chunked", "--workers", "1", adapter=FixtureAdapter(("BBB", "A-fx2")))
    pd.testing.assert_frame_equal(legacy, chunked)
    err = chunked[chunked["status"] == "error"]
    assert list(err["variant_id"]) == ["A-fx2"] and "fixture failure BBB" in err["error"].iloc[0]


def test_symbols_filter_and_timing_only(tmp_path):
    ad = FixtureAdapter()
    R.load_adapter = lambda spec, _ad=ad: _ad
    t = R.main(["--tests", "A", "--tag", "tim", "--out", str(tmp_path), "--symbols", "BBB,AAA", "--workers", "1",
                "--timing-only"])
    assert set(t["pass1"]["per_symbol"]) == {"AAA", "BBB"}
    assert set(t["pass2"]["A"]["per_variant"]) == {f"A-fx{i}" for i in range(4)}
    assert not (tmp_path / "tim" / "ledger_smoke").exists() and (tmp_path / "tim" / "timing.json").is_file()
    with pytest.raises(SystemExit):
        R.main(["--tests", "A", "--tag", "bad", "--out", str(tmp_path), "--symbols", "ZZZ"])


def test_spill_file_loads_in_a_fresh_process(tmp_path):
    """Spilled S0 signals (MappingProxyType fields) unpickle outside the run, equal to the originals."""
    import subprocess
    import sys

    from research.intraday_sr.harness import spill as SP

    sigs = [sig(symbol="AAA", day="2024-03-04"), sig(symbol="AAA", day="2024-03-05")]
    SP.write(tmp_path, "A-fx0", "AAA", sigs)
    assert repr(SP.read(tmp_path, "A-fx0", ["AAA"])) == repr(sigs)      # repr: the stub targets hold NaN
    code = ("import pickle,sys; s=pickle.load(open(sys.argv[1],'rb')); "
            "print(len(s), type(s[0].components).__name__, s[0].components['n_confirm'])")
    outp = subprocess.run([sys.executable, "-c", code, str(tmp_path / "A-fx0" / "AAA.pkl")], capture_output=True,
                          text=True, cwd=str(__import__("pathlib").Path(__file__).resolve().parents[3]), check=True)
    assert outp.stdout.split() == ["2", "mappingproxy", "3.0"]
