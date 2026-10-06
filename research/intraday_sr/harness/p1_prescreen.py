"""P1 Test B pre-screen (SPEC v1.3.5). A proxy that can only stop a run.

Input is an A1b-style trades table restricted to the 25 OOS test quarters (2020-01-01 ..
2026-03-31), all Test A variants pooled, primary guardrail d2+w5. When ``path`` is present only
``oos_exact`` rows are kept (the dev path and the selected path would double-count). When
``guardrail`` is present only ``d2+w5`` is kept.

Filter: a trade whose touch bar is the last pivot of an F1–F7 formation at pivot_tol 0.25, on
the same symbol, zone_id, side and TF. Long (W/IHS): that pivot is the last low (2nd bottom or
right shoulder). Short (M/HS): it is the last high (2nd top or right shoulder). The harness
takes the latest ``Formation.pivots`` timestamp as that point (F7's last pivot). ``touch_ts`` is
the touch bar's open.

The A1b trades file does not store ``touch_ts``, ``zone_id``, ``tf``, or ``gross_R``.
``resolve_stack_touches`` fills the touch and the spilled zone bounds. Nothing is dropped
silently. ``path`` and ``guardrail`` are required. The OOS pool must contain 192 variant ids.

1. Join each OOS trade to ``<out>/_signals/<variant_id>/<symbol>.npz`` on
   ``(variant_id, symbol, signal_available_at, direction)``. Zone bounds are ``z_low`` and
   ``z_high``. Entry tf is the single ``5m`` or ``15m`` token on ``variant_id``. A key with
   zero signals fails. A key with several signals is kept only when every candidate resolves
   to the same ``(touch_ts, z_low, z_high)``. Otherwise the run fails with the count.

2. ``stack_touch(bars, signal, cfg, sig) -> (touch_ts, rc_ts)``. Missing raises
   ``StubEngineError``. Tests pass a fake. This module does not open a market-data cache.

3. Both timestamps are strictly before ``signal.available_at``. The touch bar can be the RC::

       touch_ts <= rc_ts <= touch_ts + cfg.touch_window_bars bars

   on the entry tf (0 through ``touch_window_bars``, 3 today).

A trade matches a formation when the touch bar is the formation's last pivot bar, on the
same symbol, tf and side, and the pivot price lies inside ``[z_low, z_high]``. Formations
with no ``zone_id`` are not skipped. ``zone_id`` is not the match key (it hashes K).

``gross_R`` is derived when absent: ``risk_usd = pnl / r``, ``cost_R = (entry_cost + exit_cost) / risk_usd``,
``gross_R = r + cost_R``. ``r == 0`` or ``pnl == 0`` fails. A non-finite CI upper bound, or
zero matched trades when formations were supplied, fails loudly. It is never "not screened".

Metric: pooled net mean R with a 95% day-block bootstrap CI, seed 20260925, 5,000 resamples.
Rule: CI upper < 0 means ``screened out by P1``. N stays 456 either way.

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
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from research.intraday_sr.harness import stats as S
from research.intraday_sr.harness.fb_signals import FORMATIONS_IN, StubEngineError
from research.intraday_sr.types import ET, Formation

STACK_TOUCH = "research.intraday_sr.engine.signals.stack_touch"
_ENTRY_TF = ("5m", "15m")
_EXAMPLES = 3

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


def _require_columns(trades: pd.DataFrame, need: list[str]) -> None:
    missing = [c for c in need if c not in trades.columns]
    if missing:
        raise P1InputError("P1 trades are missing " + ", ".join(missing) + ". Rows are not dropped.")


def oos_pool(trades: pd.DataFrame) -> pd.DataFrame:
    """25 OOS test quarters, all 192 Test A variants, primary d2+w5."""
    frame = _with_gross(trades)
    _require_columns(frame, ["session", "symbol", "direction", "variant_id", "touch_ts", "tf",
                             "z_low", "z_high", "gross_R", "path", "guardrail"])
    if "net_R" not in frame.columns and "r" not in frame.columns:
        raise P1InputError("P1 trades need net_R or r.")
    pooled = _guarded_oos(frame)
    _require_finite_pool(pooled)
    n_var = int(pooled["variant_id"].astype(str).nunique())
    if n_var != 192:
        raise P1InputError(f"P1 OOS pool has {n_var} distinct variant_id values; the Test A grid has 192.")
    return pooled


def _with_gross(trades: pd.DataFrame) -> pd.DataFrame:
    if "gross_R" in trades.columns:
        return trades
    from research.intraday_sr.harness.cp4 import GrossRError, attach_gross_r
    try:
        return attach_gross_r(trades)
    except GrossRError as exc:
        raise P1InputError(str(exc)) from exc


def _guarded_oos(trades: pd.DataFrame) -> pd.DataFrame:
    """Exactly path oos_exact, guardrail d2+w5, sessions in the 25 OOS quarters."""
    _require_columns(trades, ["session", "path", "guardrail"])
    df = trades
    df = df[df["path"].astype(str) == "oos_exact"]
    df = df[df["guardrail"].astype(str) == PRIMARY]
    sess = pd.to_datetime(df["session"]).dt.date
    return df[(sess >= OOS_START) & (sess <= OOS_END)].copy()


def _require_finite_pool(df: pd.DataFrame) -> None:
    import math
    if df.empty:
        raise P1InputError("P1 OOS pool is empty. Refusing to screen on no rows.")
    for col in ("touch_ts", "tf", "z_low", "z_high"):
        if df[col].isna().any():
            raise P1InputError(f"P1 pooled column {col} has a null. Rows are not dropped.")
    net = _net(df)
    gross = pd.to_numeric(df["gross_R"], errors="coerce")
    lo = pd.to_numeric(df["z_low"], errors="coerce")
    hi = pd.to_numeric(df["z_high"], errors="coerce")
    for name, series in (("net_R", net), ("gross_R", gross), ("z_low", lo), ("z_high", hi)):
        vals = series.to_numpy(float)
        if not len(vals) or not all(math.isfinite(float(v)) for v in vals):
            raise P1InputError(f"P1 pooled {name} is not finite on every row. Refusing to drop or to call that not screened.")


def stack_touch_fn():
    """Developer 2's read-only touch helper. Missing raises StubEngineError, not an invented bar."""
    import importlib
    try:
        return getattr(importlib.import_module("research.intraday_sr.engine.signals"), "stack_touch")
    except (ImportError, AttributeError) as exc:
        raise StubEngineError(
            "engine.signals.stack_touch is not defined. Expected "
            f"{STACK_TOUCH}(bars: BarSet, signal: Signal, cfg: EngineCfg, sig: SignalCfg) "
            "-> tuple[datetime, datetime]  # (touch_ts, rc_ts) for that Test A signal's (touch, rc) pair. "
            "Refusing to invent touch bars."
        ) from exc


def _entry_tf(variant_id: str) -> str:
    hits = [part for part in str(variant_id).split("-") if part in _ENTRY_TF]
    if len(hits) != 1:
        raise P1InputError(
            f"variant_id {variant_id!r} does not carry exactly one entry tf (5m or 15m). "
            "Compact spills do not store Signal.tf; P1 reads the token on variant_id."
        )
    return hits[0]


def _join_key(variant_id, symbol, available_at, direction) -> tuple:
    from research.intraday_sr.harness.spill import _us
    return (str(variant_id), str(symbol), _us(_et(available_at, "signal_available_at")), int(direction))


def _key_text(key: tuple, matches: int) -> str:
    vid, sym, us, direction = key
    return f"{vid} {sym} ts_us={us} dir={direction} matches={matches}"


def _spill_index(spill_dir: Path) -> dict[tuple, list]:
    from research.intraday_sr.harness.spill import read_compact
    root = Path(spill_dir)
    if not root.is_dir():
        raise P1InputError(f"signal spill directory does not exist: {root}")
    index: dict[tuple, list] = {}
    n_files = 0
    for vid_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for npz in sorted(vid_dir.glob("*.npz")):
            n_files += 1
            for signal in read_compact(root, vid_dir.name, [npz.stem]):
                tf = _entry_tf(signal.variant_id)
                key = _join_key(signal.variant_id, signal.symbol, signal.available_at, signal.direction)
                index.setdefault(key, []).append((signal, tf, str(signal.zone.zone_id)))
    if n_files == 0:
        raise P1InputError(f"no compact signal spills (*.npz) under {root}")
    return index


def _test_a_cfg(variant_id: str, tf: str):
    from research.intraday_sr.grids import TEST_A
    from research.intraday_sr.types import EngineCfg, SignalCfg
    row = next((v for v in TEST_A if v["variant_id"] == variant_id), None)
    if row is None:
        raise P1InputError(f"variant_id {variant_id!r} is not on grids.TEST_A")
    if str(row["entry_tf"]) != tf:
        raise P1InputError(
            f"{variant_id}: spill entry tf {tf} != grids.TEST_A entry_tf {row['entry_tf']}"
        )
    cfg = EngineCfg(k_zones=int(row["K"]))
    sig = SignalCfg(
        oscillator=row["oscillator"], rvol_min=float(row["rvol_min"]), entry_tf=row["entry_tf"],
        target=row["target"], k_confirm=int(row["k_confirm"]), variant_id=row["variant_id"], test="A",
    )
    return cfg, sig


def _bar_steps(touch: datetime, rc: datetime, tf: str) -> int:
    width = _WIDTH[str(tf)]
    seconds = (rc - touch).total_seconds()
    step = width.total_seconds()
    if step <= 0 or seconds % step != 0:
        raise P1InputError(f"rc_ts - touch_ts is {rc - touch}, not a whole number of {tf} bars")
    return int(seconds // step)


def _touch_reasons(touch: datetime, rc: datetime, available: datetime, tf: str, window: int) -> list[str]:
    reasons = []
    if not touch < available:
        reasons.append("touch_ts is not strictly before signal.available_at")
    if not rc < available:
        reasons.append("rc_ts is not strictly before signal.available_at")
    try:
        steps = _bar_steps(touch, rc, tf)
    except P1InputError as exc:
        reasons.append(str(exc))
        return reasons
    if steps < 0:
        reasons.append("rc_ts is before touch_ts")
    elif steps > window:
        reasons.append(f"{steps} bars between touch and rc, allowed 0..{window} on {tf}")
    return reasons


def _candidates(trades: pd.DataFrame, spill_dir: Path) -> tuple[pd.DataFrame, list]:
    """OOS rows plus the spill hits for each. Zero hits fail. Several hits stay for the touch check."""
    _require_columns(trades, ["symbol", "direction", "variant_id", "signal_available_at", "path", "guardrail"])
    df = _guarded_oos(trades).reset_index(drop=True)
    index = _spill_index(spill_dir)
    groups: list = []
    missing_rows: list[str] = []
    for row in df.itertuples(index=False):
        key = _join_key(row.variant_id, row.symbol, row.signal_available_at, row.direction)
        hits = index.get(key, [])
        if len(hits) == 0:
            missing_rows.append(_key_text(key, 0))
        groups.append(hits)
    if missing_rows:
        raise P1InputError(
            f"P1 spill join: {len(missing_rows)} of {len(df)} OOS trades matched no signal. "
            f"missing examples: {missing_rows[:_EXAMPLES]}"
        )
    return df, groups


def resolve_stack_touches(trades: pd.DataFrame, spill_dir: Path, *, stack_touch=None, bars=None) -> pd.DataFrame:
    """Join the spill and call stack_touch. Several signals are kept only when touch and zone agree."""
    from research.intraday_sr.harness.spill import _us
    fn = stack_touch if stack_touch is not None else stack_touch_fn()
    joined, groups = _candidates(trades, spill_dir)
    if bars is None:
        from research.intraday_sr.types import BarSet
        bars = BarSet(pd.DataFrame())
    touches: list[datetime] = []
    rcs: list[datetime] = []
    zone_ids: list[str] = []
    tfs: list[str] = []
    lows: list[float] = []
    highs: list[float] = []
    violations: list[str] = []
    for row, hits in zip(joined.itertuples(index=False), groups):
        resolved = []
        row_bad = False
        for signal, tf, zone_id in hits:
            cfg, sig = _test_a_cfg(str(signal.variant_id), tf)
            pair = fn(bars, signal, cfg, sig)
            if not isinstance(pair, tuple) or len(pair) != 2:
                violations.append(f"{_key_text(_join_key(row.variant_id, row.symbol, row.signal_available_at, row.direction), len(hits))}: stack_touch did not return one pair")
                row_bad = True
                break
            touch = _et(pair[0], "touch_ts")
            rc = _et(pair[1], "rc_ts")
            available = _et(row.signal_available_at, "signal_available_at")
            reasons = _touch_reasons(touch, rc, available, tf, int(cfg.touch_window_bars))
            if reasons:
                violations.append(
                    f"{row.variant_id} {row.symbol} touch_ts={touch.isoformat()} rc_ts={rc.isoformat()}: "
                    + "; ".join(reasons)
                )
                row_bad = True
                break
            resolved.append((touch, rc, float(signal.z_low), float(signal.z_high), tf, zone_id))
        if row_bad:
            continue
        keys = {(_us(touch), lo, hi) for touch, _rc, lo, hi, _tf, _zid in resolved}
        if len(keys) != 1:
            violations.append(
                f"{row.variant_id} {row.symbol} matches={len(resolved)} resolved to {len(keys)} "
                "(touch_ts, z_low, z_high) values"
            )
            continue
        touch, rc, lo, hi, tf, zone_id = resolved[0]
        touches.append(touch)
        rcs.append(rc)
        lows.append(lo)
        highs.append(hi)
        tfs.append(tf)
        zone_ids.append(zone_id)
    if violations:
        raise P1InputError(
            f"P1 touch contract failed on {len(violations)} of {len(joined)} OOS trades. "
            f"Examples: {violations[:_EXAMPLES]}"
        )
    if len(touches) != len(joined):
        raise P1InputError(f"P1 resolved {len(touches)} touches for {len(joined)} OOS trades.")
    out = joined.copy()
    out["touch_ts"] = touches
    out["rc_ts"] = rcs
    out["z_low"] = lows
    out["z_high"] = highs
    out["tf"] = tfs
    out["zone_id"] = zone_ids
    return out


def _net(df: pd.DataFrame) -> pd.Series:
    if "net_R" in df.columns:
        return pd.to_numeric(df["net_R"], errors="coerce")
    return pd.to_numeric(df["r"], errors="coerce")


def filter_formation_touches(trades: pd.DataFrame, formations: list[Formation]) -> pd.DataFrame:
    """Last pivot on the touch bar, same symbol, tf and side, pivot price inside [z_low, z_high].

    ``formation.zone_id`` is not used. A standalone detector has no zone, and zone_id hashes K.
    """
    buckets: dict[tuple, list] = {}
    for form in formations:
        key = (form.symbol, form.tf, formation_direction(form.kind))
        buckets.setdefault(key, []).append(form)
    keep = []
    for row in trades.itertuples(index=False):
        key = (row.symbol, str(row.tf), int(row.direction))
        touch = _et(row.touch_ts, "touch_ts")
        lo, hi = float(row.z_low), float(row.z_high)
        hit = False
        for form in buckets.get(key, ()):
            pivot_ts, px = last_touch_pivot(form)
            if _same_bar(touch, row.tf, pivot_ts) and lo <= float(px) <= hi:
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
        if len(formations) > 0:
            raise P1InputError(
                f"P1 matched 0 of {len(pooled)} OOS trades to {len(formations)} formations. "
                "Refusing to report not screened. This is a join or matching fault for Architect review."
            )
        return {
            **base,
            "screened_out": False,
            "label": "no filtered trades",
            "net_mean_r": {"mean": None, "lo": None, "hi": None},
            "gross_mean_r": {"mean": None, "lo": None, "hi": None},
            "note": "No formations were supplied, so there is no CI.",
        }
    day = pd.to_datetime(filtered["session"]).dt.date.to_numpy()
    net = S.day_block_mean_r(_net(filtered).to_numpy(float), day)
    gross = S.day_block_mean_r(pd.to_numeric(filtered["gross_R"], errors="coerce").to_numpy(float), day)
    hi = net.get("hi")
    if hi is None or hi != hi:          # None or NaN
        raise P1InputError(
            f"P1 net mean R CI upper bound is {hi!r}. Refusing to treat a non-finite bound as not screened."
        )
    screened = float(hi) < 0.0
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
    ap.add_argument("trades", type=Path, help="A1b trades parquet (OOS quarters). touch_ts/zone_id/tf come from --signals when absent.")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--formations", type=Path, default=None,
                    help="pickle of list[Formation] from the pivot_tol 0.25 detector (fixtures)")
    ap.add_argument("--signals", type=Path, default=None,
                    help="pass-1 compact spill dir <out>/_signals. Required when trades lack touch_ts, zone_id, or tf.")
    ap.add_argument("--a1b-run-id", default="", help="A1b run_id recorded in p1.json")
    args = ap.parse_args(argv)
    try:
        formations = load_formations(args.formations)
    except StubEngineError as exc:
        raise SystemExit(str(exc)) from exc
    frame = pd.read_parquet(args.trades)
    if any(col not in frame.columns for col in ("touch_ts", "zone_id", "tf")):
        if args.signals is None:
            raise SystemExit(
                "P1 trades have no touch_ts, zone_id, or tf. "
                "Pass --signals <out>/_signals (kept pass-1 compact spills)."
            )
        try:
            frame = resolve_stack_touches(frame, args.signals)
        except StubEngineError as exc:
            raise SystemExit(str(exc)) from exc
    result = prescreen(frame, formations)
    import hashlib
    from research.intraday_sr.harness.run import engine_stamp_fields
    stamp = engine_stamp_fields(Path(__file__).resolve().parents[3])
    result["a1b_run_id"] = args.a1b_run_id
    result["trades_sha256"] = hashlib.sha256(Path(args.trades).read_bytes()).hexdigest()
    result["formations_sha256"] = hashlib.sha256(Path(args.formations).read_bytes()).hexdigest()
    result["engine_commit"] = stamp.get("engine_commit")
    result["engine_spec"] = stamp.get("engine_spec")
    write_report(result, args.out)
    print(result["label"])
    return result


if __name__ == "__main__":
    main()
