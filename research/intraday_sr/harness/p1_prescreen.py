"""P1 Test B pre-screen (SPEC v1.3.5). A proxy that can only stop a run.

Input is an A1b-style trades table restricted to the 25 OOS test quarters (2020-01-01 ..
2026-03-31), all Test A variants pooled, primary guardrail d2+w5. When ``path`` is present only
``oos_exact`` rows are kept (the dev path and the selected path would double-count). When
``guardrail`` is present only ``d2+w5`` is kept.

Filter: a trade whose touch bar is the last pivot of an F1–F7 formation at pivot_tol 0.25, on
the same symbol, zone_id, side and TF. Long (W/IHS): that pivot is the last low (2nd bottom or
right shoulder). Short (M/HS): it is the last high (2nd top or right shoulder). The harness
takes the latest ``Formation.pivots`` timestamp as that point (F7's last pivot). ``touch_ts`` is
the touch bar's open. ``zone_id`` and ``tf`` must already be on the trades table: the Test A
trades file this harness writes does not carry the stack touch bar, so P1 reads an extract that
does. A missing column is an error, not a silent pass.

Metric: pooled net mean R (column ``net_R``, else ``r``) with a 95% day-block bootstrap CI,
seed 20260925, 5,000 resamples (``stats.day_block_mean_r``). Gross mean R (``gross_R``) is
reported beside it. Rule: CI upper < 0 means ``screened out by P1``. No filtered trades does
not screen Test B out (there is no CI). N stays 456 either way.

The entry is the RC trigger, not F8. This is a proxy. It can only stop a run. It can never
count toward a pass.

Engine entry, if formations are not passed in (fixtures pass them in; this module does not
load market data)::

    research.intraday_sr.engine.formations.formations_in(
        bars: BarSet,
        start: datetime,
        end: datetime,
        cfg: EngineCfg,
        pivot_tol: float,   # P1 uses 0.25 only
    ) -> Iterator[Formation]

``formations_at`` returning [] is the stub. A missing ``formations_in`` raises
``StubEngineError`` rather than screening on zero formations.
"""

from __future__ import annotations

import argparse
import json
import pickle
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from research.intraday_sr.harness import stats as S
from research.intraday_sr.harness.fb_signals import FORMATIONS_IN, StubEngineError
from research.intraday_sr.types import ET, Formation

PIVOT_TOL = 0.25
PRIMARY = "d2+w5"
OOS_START, OOS_END = date(2020, 1, 1), date(2026, 3, 31)
_WIDTH = {"5m": timedelta(minutes=5), "15m": timedelta(minutes=15)}
_LONG = frozenset({"W", "IHS"})
_SHORT = frozenset({"M", "HS"})
CAVEAT = (
    "Proxy caveat: the entry is the RC trigger, not F8, so this is a proxy. "
    "P1 can only stop a run. It can never count toward a pass."
)


class P1InputError(ValueError):
    """The trades extract or the formation list cannot support the P1 rule."""


def formation_direction(kind: str) -> int:
    if kind in _LONG:
        return 1
    if kind in _SHORT:
        return -1
    raise P1InputError(f"formation kind {kind!r} is not W, IHS, M, or HS")


def last_touch_pivot(formation: Formation):
    """Latest pivot: last low for W/IHS, last high for M/HS (B2 / P1)."""
    if not formation.pivots:
        raise P1InputError(f"formation {formation.formation_id} has no pivots")
    return max(formation.pivots, key=lambda item: item[0])


def _et(value, name: str):
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        raise P1InputError(f"{name} must be tz-aware")
    return ts.tz_convert(ET).to_pydatetime()


def _same_bar(touch, tf: str, pivot) -> bool:
    if touch == pivot:
        return True
    try:
        width = _WIDTH[str(tf)]
    except KeyError as exc:
        raise P1InputError(f"tf must be 5m or 15m, got {tf!r}") from exc
    return touch <= pivot < touch + width


def oos_pool(trades: pd.DataFrame) -> pd.DataFrame:
    """25 OOS test quarters, all variants, primary d2+w5."""
    need = ["session", "symbol", "direction", "touch_ts", "zone_id", "tf", "gross_R"]
    missing = [c for c in need if c not in trades.columns]
    if "net_R" not in trades.columns and "r" not in trades.columns:
        missing.append("net_R|r")
    if missing:
        raise P1InputError(
            "P1 trades extract is missing "
            + ", ".join(missing)
            + ". The harness trades file does not store the Test A stack touch bar; "
            "touch_ts (bar open), zone_id and tf have to be on the extract."
        )
    df = trades
    if "path" in df.columns:
        df = df[df["path"].astype(str) == "oos_exact"]
    if "guardrail" in df.columns:
        df = df[df["guardrail"].astype(str) == PRIMARY]
    sess = pd.to_datetime(df["session"]).dt.date
    return df[(sess >= OOS_START) & (sess <= OOS_END)].copy()


def _net(df: pd.DataFrame) -> pd.Series:
    if "net_R" in df.columns:
        return pd.to_numeric(df["net_R"], errors="coerce")
    return pd.to_numeric(df["r"], errors="coerce")


def filter_formation_touches(trades: pd.DataFrame, formations: list[Formation]) -> pd.DataFrame:
    """Keep trades whose touch bar is the formation's last pivot on the same zone, side and TF."""
    buckets: dict[tuple, list] = {}
    for form in formations:
        if not form.zone_id:
            continue
        key = (form.symbol, form.tf, str(form.zone_id), formation_direction(form.kind))
        buckets.setdefault(key, []).append(form)
    keep = []
    for row in trades.itertuples(index=False):
        key = (row.symbol, str(row.tf), str(row.zone_id), int(row.direction))
        touch = _et(row.touch_ts, "touch_ts")
        hit = False
        for form in buckets.get(key, ()):
            pivot_ts, _px = last_touch_pivot(form)
            if _same_bar(touch, row.tf, pivot_ts):
                hit = True
                break
        keep.append(hit)
    return trades.loc[keep].copy()


def prescreen(trades: pd.DataFrame, formations: list[Formation], *, pivot_tol: float = PIVOT_TOL) -> dict:
    """Pooled net mean R and the P1 rule. ``formations`` are the pivot_tol 0.25 detector output."""
    if float(pivot_tol) != PIVOT_TOL:
        raise P1InputError(f"P1 pivot_tol is fixed at {PIVOT_TOL}, got {pivot_tol}")
    pooled = oos_pool(trades)
    filtered = filter_formation_touches(pooled, list(formations))
    base = {
        "pivot_tol": PIVOT_TOL,
        "guardrail": PRIMARY,
        "oos_window": [OOS_START.isoformat(), OOS_END.isoformat()],
        "n_oos": int(len(pooled)),
        "n_filtered": int(len(filtered)),
        "n_formations": int(len(formations)),
        "bootstrap": {"seed": S.SEED, "resamples": S.RESAMPLES, "level": 0.95},
        "caveat": CAVEAT,
        "rule": "CI upper < 0 means screened out by P1",
        "n_program": 456,
        "n_note": "Screening Test B out does not lower N. N_PROGRAM stays 456.",
    }
    if filtered.empty:
        return {
            **base,
            "screened_out": False,
            "label": "no filtered trades",
            "net_mean_r": {"mean": None, "lo": None, "hi": None},
            "gross_mean_r": {"mean": None, "lo": None, "hi": None},
            "note": "No CI, so P1 does not screen Test B out.",
        }
    day = pd.to_datetime(filtered["session"]).dt.date.to_numpy()
    net = S.day_block_mean_r(_net(filtered).to_numpy(float), day)
    gross = S.day_block_mean_r(pd.to_numeric(filtered["gross_R"], errors="coerce").to_numpy(float), day)
    screened = net.get("hi") is not None and float(net["hi"]) < 0.0
    return {
        **base,
        "screened_out": bool(screened),
        "label": "screened out by P1" if screened else "not screened out by P1",
        "net_mean_r": net,
        "gross_mean_r": gross,
    }


def missing_detector_error() -> StubEngineError:
    import importlib
    mod = importlib.import_module("research.intraday_sr.engine.formations")
    fn = getattr(mod, "formations_in", None)
    if callable(fn):
        return StubEngineError(
            f"{FORMATIONS_IN} is defined, but this script does not load market data. "
            "Pass --formations (a pickle of Formation objects) for a fixture."
        )
    return StubEngineError(
        "engine.formations.formations_in is not defined. formations_at() is the S0 stub and "
        "returns []. Refusing to screen Test B on zero formations. "
        f"Expected {FORMATIONS_IN}(bars: BarSet, start: datetime, end: datetime, cfg: EngineCfg, "
        "pivot_tol: float) -> Iterator[Formation], called at pivot_tol 0.25. "
        "Pass --formations for a fixture."
    )


def load_formations(path: Path | None) -> list[Formation]:
    if path is None:
        raise missing_detector_error()
    with open(path, "rb") as fh:
        got = pickle.load(fh)
    if not isinstance(got, list) or any(not isinstance(x, Formation) for x in got):
        raise P1InputError("--formations must be a pickle of list[Formation]")
    return got


def write_report(result: dict, out: Path) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    jp, mp = out / "p1.json", out / "p1.md"
    jp.write_text(json.dumps(result, indent=1, default=str))
    net, gross = result["net_mean_r"], result["gross_mean_r"]
    lines = [
        "# P1 Test B pre-screen",
        "",
        f"**{result['label']}**",
        "",
        CAVEAT,
        "",
        f"- pivot_tol: {result['pivot_tol']} (fixed)",
        f"- guardrail: {result['guardrail']}",
        f"- OOS trades pooled (25 test quarters, all variants): {result['n_oos']}",
        f"- trades whose touch bar is a formation last pivot: {result['n_filtered']}",
        f"- net mean R: {net}",
        f"- gross mean R (beside net): {gross}",
        f"- bootstrap seed {result['bootstrap']['seed']}, resamples {result['bootstrap']['resamples']}",
        "",
        "Rule: if the net mean R 95% day-block CI upper bound is < 0, Test B is screened out by P1.",
        "A screened-out test is not run. It stays in N (N_PROGRAM 456). It cannot count toward a pass.",
        "",
    ]
    mp.write_text("\n".join(lines))
    return jp, mp


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description="P1 Test B pre-screen (SPEC v1.3.5). Proxy only.")
    ap.add_argument("trades", type=Path, help="A1b trades parquet extract (OOS quarters, touch_ts, zone_id, tf)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--formations", type=Path, default=None,
                    help="pickle of list[Formation] from the pivot_tol 0.25 detector (fixtures)")
    args = ap.parse_args(argv)
    try:
        formations = load_formations(args.formations)
    except StubEngineError as exc:
        raise SystemExit(str(exc)) from exc
    result = prescreen(pd.read_parquet(args.trades), formations)
    write_report(result, args.out)
    print(result["label"])
    return result


if __name__ == "__main__":
    main()
