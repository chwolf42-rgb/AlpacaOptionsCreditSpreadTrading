"""Trial log (SPEC section 5): one row per (test, variant, fold, phase), including failed variants. The row
count per test (distinct variants) is the trial count N used by the deflated Sharpe (section 6/8)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

CT = ZoneInfo("America/Chicago")

SCHEMA = {
    "run_id": "string", "run_kind": "string",          # interim | full | holdout | smoke
    "spec_version": "string", "grid_sha256": "string", "engine_cfg": "string", "git_sha": "string",
    "test": "string", "variant_id": "string", "fold": "int32", "phase": "string",   # train | test | holdout
    "symbols": "string", "n_symbols": "int32",
    "status": "string", "error": "string",
    "trades": "int32", "trades_per_day": "float64", "win_rate": "float64", "mean_r": "float64",
    "sum_r": "float64", "profit_factor": "float64", "monthly_mean": "float64", "sharpe_daily": "float64",
    "cap_limited_share": "float64", "overlay": "string", "created_at_ct": "string",
}


@dataclass
class TrialRow:
    run_id: str
    run_kind: str
    spec_version: str
    grid_sha256: str
    engine_cfg: str
    git_sha: str
    test: str
    variant_id: str
    fold: int
    phase: str
    symbols: str
    n_symbols: int
    status: str = "ok"
    error: str = ""
    trades: int = 0
    trades_per_day: float = float("nan")
    win_rate: float = float("nan")
    mean_r: float = float("nan")
    sum_r: float = float("nan")
    profit_factor: float = float("nan")
    monthly_mean: float = float("nan")
    sharpe_daily: float = float("nan")
    cap_limited_share: float = float("nan")
    overlay: str = ""
    created_at_ct: str = field(default_factory=lambda: datetime.now(CT).isoformat(timespec="seconds"))


def git_sha(repo: Path | str = ".") -> str:
    try:
        return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


class TrialLog:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._rows: list[dict] = []

    def add(self, row: TrialRow) -> None:
        missing = set(SCHEMA) - set(asdict(row))
        if missing:
            raise ValueError(f"trial row missing {missing}")
        self._rows.append(asdict(row))

    def flush(self) -> pd.DataFrame:
        new = pd.DataFrame(self._rows, columns=list(SCHEMA)).astype(SCHEMA) if self._rows else None
        old = pd.read_parquet(self.path) if self.path.exists() else None
        frames = [f for f in (old, new) if f is not None]
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=list(SCHEMA)).astype(SCHEMA)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(self.path, index=False)
        self._rows = []
        return df

    @staticmethod
    def trial_count(df: pd.DataFrame, test: str, include_overlays: bool = False) -> int:
        d = df[df["test"] == test]
        if not include_overlays:
            d = d[d["overlay"].fillna("") == ""]
        return int(d["variant_id"].nunique())
