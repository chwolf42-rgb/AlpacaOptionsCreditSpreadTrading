"""Trial log (SPEC section 5): one row per (test, variant, fold, phase), including failed variants. The row
count per test (distinct variants) is the trial count N used by the deflated Sharpe (section 6/8)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
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
    # SPEC v1.3.5: formations ledger rows carry grid family "formations". Empty for A/B.
    "grid_family": "string",
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
    grid_family: str = ""


def git_sha(repo: Path | str = ".") -> str:
    try:
        return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


DEFAULT_LEDGER = Path("/workspace/research4/runs/PROGRAM_LEDGER")    # box-wide program ledger (all runs)
from research.intraday_sr.harness.config import N_PROGRAM as N_FLOOR  # 456: SPEC v1.3.2 O1.9 declared program trials


_GUARDRAIL_LABEL = re.compile(r"none|d\d+(\+w\d+)?|w\d+", re.I)   # guardrail configs (0 trials), never options rows


class LedgerError(RuntimeError):
    pass


class TrialLog:
    """Append-only PROGRAM ledger (R1 ruling). `path` is a directory of immutable part files; each flush creates a
    new part (exclusive create) and never rewrites or deletes an existing one. A trial is a distinct
    (grid_sha256, run_id, test, variant_id, overlay) among non-smoke rows; options overlay rows count (SPEC O1.9),
    guardrail-configuration rows do not (G2: 0 trials). Re-running the same grid hash under a new run id adds trials;
    re-flushing the same run id does not double count. The DSR N, FREEZE.md and the readout header read
    N = max(456, program_trial_count) from here, cumulative at the candidate's freeze time.
    """

    def __init__(self, path: Path | str = DEFAULT_LEDGER):
        self.path = Path(path)
        if self.path.suffix == ".parquet":
            raise LedgerError("the trial log is a ledger directory of part files, not a single parquet file")
        self._rows: list[dict] = []

    def add(self, row: TrialRow) -> None:
        missing = set(SCHEMA) - set(asdict(row))
        if missing:
            raise ValueError(f"trial row missing {missing}")
        if not row.grid_sha256 or not row.run_id:
            raise LedgerError("ledger rows are keyed by grid_sha256 + run_id; both are required")
        self._rows.append(asdict(row))

    def parts(self) -> list[Path]:
        return sorted(self.path.glob("part-*.parquet")) if self.path.exists() else []

    def read(self) -> pd.DataFrame:
        ps = self.parts()
        if not ps:
            return pd.DataFrame(columns=list(SCHEMA)).astype(SCHEMA)
        return pd.concat([pd.read_parquet(p) for p in ps], ignore_index=True)

    def flush(self) -> pd.DataFrame:
        if self._rows:
            new = pd.DataFrame(self._rows, columns=list(SCHEMA)).astype(SCHEMA)
            self.path.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(CT).strftime("%Y%m%dT%H%M%S%f")
            run = str(self._rows[0]["run_id"]).replace("/", "_")
            p = self.path / f"part-{stamp}-{run}-{uuid.uuid4().hex[:8]}.parquet"
            with open(p, "xb") as fh:              # exclusive create: append-only, never overwrite
                new.to_parquet(fh, index=False)
            self._rows = []
        return self.read()

    def sha256(self) -> str:
        h = hashlib.sha256()
        for p in self.parts():
            h.update(p.name.encode())
            h.update(hashlib.sha256(p.read_bytes()).digest())
        return h.hexdigest()

    @staticmethod
    def trial_count(df: pd.DataFrame, test: str, include_overlays: bool = False) -> int:
        """Per-test distinct variants in `df` (display only; the DSR uses program_trial_count)."""
        d = df[df["test"] == test]
        if not include_overlays:
            d = d[d["overlay"].fillna("") == ""]
        return int(d["variant_id"].nunique())

    @staticmethod
    def is_guardrail_overlay(label) -> bool:
        """Guardrail-configuration labels (none, d2, d2+w5, d2+w6, d3+w6, w5, ...): comparison rows, 0 trials (G2)."""
        return bool(_GUARDRAIL_LABEL.fullmatch(str(label or "").strip()))

    @staticmethod
    def program_trial_count(df: pd.DataFrame, as_of_ct: Optional[str] = None) -> int:
        """Cumulative program trials: distinct (grid_sha256, run_id, test, variant_id, overlay), all tests, all runs,
        excluding smoke runs and guardrail-configuration rows, created at or before `as_of_ct` (ISO CT; e.g. the
        freeze time). Options overlay rows (non-empty `overlay`, e.g. a (scenario, book) label) COUNT: SPEC v1.3.2
        O1.9 says overlay rows are logged and feed DSR N."""
        if df is None or not len(df):
            return 0
        ov = df["overlay"].fillna("").astype(str)
        d = df[(df["run_kind"].fillna("") != "smoke") & ~ov.map(TrialLog.is_guardrail_overlay)].copy()
        d["overlay"] = d["overlay"].fillna("")
        if as_of_ct is not None:
            ts = pd.to_datetime(d["created_at_ct"], utc=True, format="ISO8601")
            d = d[ts <= pd.Timestamp(as_of_ct).tz_convert("UTC")]
        return int(len(d.drop_duplicates(["grid_sha256", "run_id", "test", "variant_id", "overlay"])))

    def dsr_n(self, as_of_ct: Optional[str] = None) -> int:
        return max(N_FLOOR, self.program_trial_count(self.read(), as_of_ct))
