"""SPEC v1.3 G2/G8: selection, finalists, FREEZE and holdout code read only the primary configuration and never
read guardrail_compare.parquet."""
import ast
import builtins
import inspect
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.intraday_sr.harness import compare as CMP
from research.intraday_sr.harness import walkforward as W
from research.intraday_sr.harness.config import PRIMARY_LABEL


def _run(vid, n=300, config=PRIMARY_LABEL):
    days = pd.bdate_range("2024-01-02", periods=n).date
    t = pd.DataFrame({"session": days, "r": 0.1, "pnl": 10.0, "symbol": "AAA"})
    return W.VariantRun(vid, t, pd.Series(0.001, index=pd.Index(days)), config=config)


def test_walkforward_module_never_references_compare_output():
    src = Path(inspect.getsourcefile(W)).read_text()
    assert "guardrail_compare" not in src and CMP.COMPARE_FILENAME not in src
    tree = ast.parse(src)
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
           {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and m.endswith("harness.compare") for m in mods)


@pytest.mark.parametrize("fn", ["select", "walk_forward", "finalists"])
def test_selection_refuses_comparison_runs(fn):
    runs = {"a": _run("a"), "b": _run("b", config="none")}
    with pytest.raises(W.ComparisonConfigInSelection):
        if fn == "select":
            W.select(runs, date(2024, 1, 1), date(2026, 1, 1))
        elif fn == "walk_forward":
            W.walk_forward(runs)
        else:
            W.finalists(runs, pd.DataFrame())


def test_freeze_refuses_comparison_config(tmp_path):
    from research.intraday_sr.harness.triallog import TrialLog
    with pytest.raises(W.ComparisonConfigInSelection):
        W.write_freeze(tmp_path / "FREEZE.md", {}, TrialLog(tmp_path / "ledger"), "sha", "grid", "v1.3", config="d2+w6")


def test_selection_and_freeze_never_open_the_compare_file(tmp_path, monkeypatch):
    cp = CMP.write_compare(tmp_path, [{"config": "none", "trades": 1}])
    assert cp.parent.name == CMP.COMPARE_DIRNAME              # its own directory
    opened = []
    real_open, real_rp = builtins.open, pd.read_parquet
    def spy_open(f, *a, **k):
        opened.append(str(f)); return real_open(f, *a, **k)
    def spy_rp(p, *a, **k):
        opened.append(str(p)); return real_rp(p, *a, **k)
    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(pd, "read_parquet", spy_rp)
    runs = {"a": _run("a"), "b": _run("b", 250)}
    wf = W.walk_forward(runs)
    from research.intraday_sr.harness.triallog import TrialLog
    W.write_freeze(tmp_path / "FREEZE.md", wf.finalists, TrialLog(tmp_path / "ledger"), "sha", "grid", "v1.3")
    assert not any(CMP.COMPARE_FILENAME in o for o in opened)
