"""A1b: declared program N = 456 lives in the harness (not grids.py), GRID_SHA256 stays pinned, and the manifest
records the true launch time plus harness/engine provenance outside grid_document()."""

import json
import re
from pathlib import Path

import pandas as pd

from research.intraday_sr import grids
from research.intraday_sr.harness import config as C
from research.intraday_sr.harness import run as R
from research.intraday_sr.harness import s0grids

PINNED = "2ef95123c015d70a1751f559f21fa1249d0b08aea272319e232f2facb120aa22"
REPO = Path(__file__).resolve().parents[3]


def test_grid_sha256_unchanged_and_n_program_outside_the_grid():
    assert grids.GRID_SHA256 == grids.grid_sha256() == s0grids.grid_sha256() == PINNED
    assert grids.SPEC_VERSION == "v1.3.1" and grids.N_TRIALS == 450 == C.N_TOTAL
    assert C.N_PROGRAM == 456 == C.N_DECLARED["A"] + C.N_DECLARED["B"] + C.N_DECLARED["F"] + C.N_OVERLAY_O1
    doc = grids.grid_document()
    assert not any("program" in k.lower() or "engine_spec" in k or "engine_commit" in k for k in doc)
    assert "456" not in grids.canonical_json() or PINNED == grids.grid_sha256()
    assert not hasattr(grids, "N_PROGRAM")


def test_engine_stamp_fields_from_git():
    f = R.engine_stamp_fields(REPO)
    hexsha = re.compile(r"[0-9a-f]{40}")
    assert hexsha.fullmatch(f["harness_commit"]) and hexsha.fullmatch(f["engine_commit"])
    assert hexsha.fullmatch(f["engine_tree"])
    try:
        import research.intraday_sr.engine.version  # noqa: F401
    except ImportError:
        assert f["engine_spec"] == R.ENGINE_SPEC_FALLBACK == "v1.3.3"
    assert set(f).isdisjoint(grids.grid_document())


def test_manifest_records_launch_pass2_start_and_provenance(tmp_path, monkeypatch):
    from research.intraday_sr.tests.test_harness_run_chunked import FixtureAdapter
    ad = FixtureAdapter()
    monkeypatch.setattr(R, "load_adapter", lambda spec, _ad=ad: _ad)
    R.main(["--tests", "A", "--tag", "prov", "--out", str(tmp_path), "--workers", "1"])
    m = json.loads((tmp_path / "prov" / "manifest.json").read_text())
    t = json.loads((tmp_path / "prov" / "timing.json").read_text())
    start, p2, fin = (pd.Timestamp(m[k]) for k in ("started_at_ct", "pass2_started_at_ct", "finished_at_ct"))
    assert start <= p2 <= fin and t["started_at_ct"] == m["started_at_ct"]
    for k in ("harness_commit", "engine_commit", "engine_tree", "engine_spec", "engine_spec_source"):
        assert m[k] and m[k] != "unknown", k
    assert m["harness_commit"] == m["git_sha"] and m["spec_version"] == "v1.3.1"
    assert m["dsr_n"] == 456
