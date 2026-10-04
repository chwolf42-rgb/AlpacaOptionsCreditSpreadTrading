"""Comparison guardrail configurations (SPEC v1.3 G2): run AFTER selection, on the same per-fold picks and frozen
finalists, with only RiskCfg changed. Output goes ONLY to guardrail_compare.parquet in its own directory.

Selection, finalists, FREEZE and holdout code (walkforward.py) never import this module or read that file;
tests/test_harness_guardrail_separation.py enforces it. These rows are not trial-log rows and add nothing to N.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

COMPARE_DIRNAME = "guardrail_compare"
COMPARE_FILENAME = "guardrail_compare.parquet"


def compare_path(out_dir: Path) -> Path:
    """Separate sub-directory, never the trial-log / FREEZE directory."""
    return Path(out_dir) / COMPARE_DIRNAME / COMPARE_FILENAME


def write_compare(out_dir: Path, rows: list[dict]) -> Path:
    p = compare_path(out_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    if p.exists():
        df = pd.concat([pd.read_parquet(p), df], ignore_index=True)
    df.to_parquet(p, index=False)
    return p
