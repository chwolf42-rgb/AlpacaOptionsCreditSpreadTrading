"""Harness-side conformance check for Developer 2's `grids.py` against SPEC v1.3 (v1.1 A1/A2 grids, G1/G2).

`grids.py` is owned by Developer 2 (S0). This module only *checks* it, so the harness refuses to start a run on a
grid that drifted from the frozen spec. Adapt `extract()` once the S0 layout is known; the checks are layout-free.
"""
from __future__ import annotations

import hashlib
import itertools
import json
from typing import Any, Iterable, Mapping, Sequence

V11_AXES = {                       # SPEC v1.1 A1: Test A and Test B each 2*2*2*2*3*4 = 192
    "K": {3, 5},
    "oscillator": 2,               # RSI14 30/70, Stoch(14,3,3) 20/80 (labels are Developer 2's)
    "rvol_min": {1.5, 2.0},
    "tf": {"5m", "15m"},
    "target": 3,                   # 1R, 2R, zone (canonical; "next_zone" accepted as a temporary alias)
    "k_confirm": {0, 1, 2, 3},
}
K_CLUSTER_FIXED = 0.25
PER_TEST = 192
FORMATIONS = 48                    # 4 kinds x TF 2 x target 3 x pivot tol {0.15, 0.25} 2
OPTIONS_BASELINE, OPTIONS_0DTE = 9, 9
N_TOTAL = 2 * PER_TEST + FORMATIONS + OPTIONS_BASELINE + OPTIONS_0DTE      # 450 (SPEC v1.3 G2)
ALIASES = {"K": ("K", "k", "zones_per_side", "n_zones"), "oscillator": ("oscillator", "osc"),
           "rvol_min": ("rvol_min",), "tf": ("tf", "entry_tf", "timeframe"), "target": ("target",),
           "k_confirm": ("k_confirm",), "k_cluster": ("k_cluster", "k_cluster_atr")}


def grid_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _get(v: Mapping, axis: str):
    for k in ALIASES.get(axis, (axis,)):
        if k in v:
            return v[k]
    raise KeyError(axis)


def check_test_grid(name: str, variants: Sequence[Mapping]) -> list[str]:
    errs = []
    if len(variants) != PER_TEST:
        errs.append(f"{name}: {len(variants)} variants, SPEC v1.1 requires {PER_TEST}")
    seen = set()
    for axis, want in V11_AXES.items():
        try:
            vals = {_get(v, axis) for v in variants}
        except KeyError:
            errs.append(f"{name}: axis '{axis}' missing")
            continue
        if isinstance(want, set) and vals != want:
            errs.append(f"{name}: axis '{axis}' values {sorted(vals, key=str)} != {sorted(want, key=str)}")
        if isinstance(want, int) and len(vals) != want:
            errs.append(f"{name}: axis '{axis}' has {len(vals)} values, expected {want}")
        if axis == "target":
            from research.intraday_sr.harness.config import UnknownTarget, canonical_target
            for t in vals:
                try:
                    canonical_target(t)
                except UnknownTarget as e:
                    errs.append(f"{name}: {e}")
    try:
        kc = {float(_get(v, "k_cluster")) for v in variants}
        if kc != {K_CLUSTER_FIXED}:
            errs.append(f"{name}: k_cluster {sorted(kc)} must be fixed at {K_CLUSTER_FIXED} ATR_d")
    except KeyError:
        pass                         # fixed constant outside the variant dicts is fine
    for v in variants:
        try:
            key = tuple(str(_get(v, a)) for a in V11_AXES)
        except KeyError:
            break
        if key in seen:
            errs.append(f"{name}: duplicate variant {key}")
        seen.add(key)
    ids = [v.get("variant_id") for v in variants if "variant_id" in v]
    if ids and len(set(ids)) != len(ids):
        errs.append(f"{name}: variant_id not unique")
    return errs


SPEC_FORMATION_AXES = {"kind": {"W", "IHS", "M", "HS"}, "entry_tf": {"5m", "15m"}, "target": {"1R", "2R", "zone"},
                       "pivot_tol_atr": {0.15, 0.25}}
SPEC_OPTIONS_AXES = {"structure": {"long_atm", "long_otm_1", "debit_vertical"}, "dte_bucket": {"0-1", "2-4", "5-7"}}
SPEC_0DTE_AXES = {"stop_pct": {-30, -40, -50}, "take_profit_pct": {50, 65, 80}, "time_exit_et": {"15:45"},
                  "premium_dollars": {2000}}
SPEC_EQUITY_AXES = {"K": {3, 5}, "oscillator": {"rsi14_30_70", "stoch14_3_3_20_80"}, "rvol_min": {1.5, 2.0},
                    "entry_tf": {"5m", "15m"}, "target": {"1R", "2R", "zone"}, "k_confirm": {0, 1, 2, 3}}
# SPEC values the FIXED block must carry (v1.1 A1/A2, v1.3 G1, v1.3.1 C3). Extra FIXED keys (R5 additions) are allowed.
SPEC_FIXED = {"k_cluster_atr": 0.25, "entries_per_day": 12, "max_concurrent": 4, "max_per_symbol": 1,
              "daily_loss_stop": -0.015, "pivot_n": {"5m": 3, "15m": 3, "1h": 2, "1d": 2}}
SPEC_PRIMARY_GUARDRAIL = {"daily_losses": 2, "weekly_losses": 5}
SPEC_COMPARISON_GUARDRAILS = [{"name": "none"}, {"name": "d2+w6", "daily_losses": 2, "weekly_losses": 6}]


def check_exact_grid(name: str, rows: Sequence[Mapping], axes: Mapping[str, set], n: int,
                     test_label=None) -> list[str]:
    """Exact check: the rows are the full Cartesian product of the SPEC axis value sets, nothing more or less."""
    errs = []
    if len(rows) != n:
        errs.append(f"{name}: {len(rows)} rows, SPEC requires {n}")
    for ax, want in axes.items():
        got = {r.get(ax, "<missing>") for r in rows}
        if got != set(want):
            errs.append(f"{name}: axis {ax!r} values {sorted(got, key=str)} != SPEC {sorted(want, key=str)}")
    combos = [tuple(r.get(ax) for ax in axes) for r in rows]
    if len(set(combos)) != len(combos):
        errs.append(f"{name}: duplicate axis combinations")
    if set(combos) != set(itertools.product(*[sorted(v, key=str) for v in axes.values()])):
        errs.append(f"{name}: rows are not the exact Cartesian product of the SPEC axes")
    ids = [r.get("variant_id") for r in rows]
    if None in ids or len(set(ids)) != len(ids):
        errs.append(f"{name}: variant_id missing or not unique")
    for r in rows:
        extra = set(r) - set(axes) - {"variant_id", "test", "grid"}
        if extra:
            errs.append(f"{name}: unexpected keys {sorted(extra)} (guardrail/constants are not axes)")
            break
        if test_label is not None:
            want_t = test_label(r)
            if r.get("test", want_t) != want_t:
                errs.append(f"{name}: row {r.get('variant_id')} labelled test={r.get('test')!r}, expected {want_t!r}")
                break
    return errs


def check_all(test_a: Sequence[Mapping], test_b: Sequence[Mapping], formations: Sequence[Mapping] | None = None,
              options_baseline: Sequence | None = None, options_0dte: Sequence | None = None) -> list[str]:
    errs = check_test_grid("Test A", test_a) + check_test_grid("Test B", test_b)
    if formations is not None and len(formations) != FORMATIONS:
        errs.append(f"formations: {len(formations)} variants, SPEC requires {FORMATIONS}")
    if options_baseline is not None and len(options_baseline) != OPTIONS_BASELINE:
        errs.append(f"options baseline: {len(options_baseline)} != {OPTIONS_BASELINE}")
    if options_0dte is not None and len(options_0dte) != OPTIONS_0DTE:
        errs.append(f"0DTE: {len(options_0dte)} != {OPTIONS_0DTE}")
    return errs


def reference_v11(test: str) -> list[dict]:
    """Reference expansion used only to test this checker (not a substitute for grids.py)."""
    out = []
    for K, osc, rv, tf, tgt, kc in itertools.product((3, 5), ("rsi14_30_70", "stoch14_3_3_20_80"), (1.5, 2.0),
                                                      ("5m", "15m"), ("1R", "2R", "zone"), (0, 1, 2, 3)):
        out.append(dict(test=test, K=K, oscillator=osc, rvol_min=rv, tf=tf, target=tgt, k_confirm=kc,
                        k_cluster=K_CLUSTER_FIXED, variant_id=f"{test}-K{K}-{osc}-rv{rv}-{tf}-{tgt}-kc{kc}"))
    return out


def check_grids_module(mod) -> list[str]:
    """Full CP0 R6 check of Developer 2's grids module: exact axis value sets for EVERY grid (Test A/B, formations,
    options, 0DTE), N = 450, FIXED, PRIMARY_GUARDRAIL and COMPARISON_GUARDRAILS, and that GRID_SHA256 is the hash of a
    document that contains exactly these rows and constants. Any mutation is rejected."""
    errs = []
    need = ("TEST_A", "TEST_B", "FORMATIONS", "OPTIONS", "OPTIONS_0DTE", "FIXED", "PRIMARY_GUARDRAIL",
            "COMPARISON_GUARDRAILS", "N_TRIALS", "GRID_SHA256")
    miss = [k for k in need if not hasattr(mod, k)]
    if miss:
        return [f"grids module lacks {miss}"]
    errs += check_exact_grid("Test A", mod.TEST_A, SPEC_EQUITY_AXES, PER_TEST, lambda r: "A")
    errs += check_exact_grid("Test B", mod.TEST_B, SPEC_EQUITY_AXES, PER_TEST, lambda r: "B")
    errs += check_exact_grid("formations", mod.FORMATIONS, SPEC_FORMATION_AXES, FORMATIONS, lambda r: f"F_{r.get('kind')}")
    errs += check_exact_grid("options", mod.OPTIONS, SPEC_OPTIONS_AXES, OPTIONS_BASELINE)
    errs += check_exact_grid("options_0dte", mod.OPTIONS_0DTE, SPEC_0DTE_AXES, OPTIONS_0DTE)
    ids = [r.get("variant_id") for g in (mod.TEST_A, mod.TEST_B, mod.FORMATIONS, mod.OPTIONS, mod.OPTIONS_0DTE) for r in g]
    if len(set(ids)) != len(ids):
        errs.append("variant_id collides across grids")
    n = sum(len(getattr(mod, k)) for k in ("TEST_A", "TEST_B", "FORMATIONS", "OPTIONS", "OPTIONS_0DTE"))
    if n != N_TOTAL or int(mod.N_TRIALS) != N_TOTAL:
        errs.append(f"N: {n} rows, N_TRIALS {mod.N_TRIALS}; SPEC N = {N_TOTAL}")
    for k, v in SPEC_FIXED.items():
        if k not in mod.FIXED:
            errs.append(f"FIXED lacks {k!r}")
        elif json.dumps(mod.FIXED[k], sort_keys=True) != json.dumps(v, sort_keys=True):
            errs.append(f"FIXED[{k!r}] = {mod.FIXED[k]!r} != SPEC {v!r}")
    pg = dict(mod.PRIMARY_GUARDRAIL) if isinstance(mod.PRIMARY_GUARDRAIL, Mapping) else mod.PRIMARY_GUARDRAIL
    if pg != SPEC_PRIMARY_GUARDRAIL:
        errs.append(f"PRIMARY_GUARDRAIL {pg!r} != {SPEC_PRIMARY_GUARDRAIL} (SPEC v1.3.1 C3)")
    cg = [dict(g) for g in mod.COMPARISON_GUARDRAILS]
    if cg != SPEC_COMPARISON_GUARDRAILS:
        errs.append(f"COMPARISON_GUARDRAILS {cg!r} != {SPEC_COMPARISON_GUARDRAILS}")
    # the logged hash must be the hash of THESE rows and constants
    if not hasattr(mod, "grid_document") or not hasattr(mod, "grid_sha256"):
        errs.append("grids module lacks grid_document()/grid_sha256()")
    else:
        doc = mod.grid_document()
        live = {"test_a": list(mod.TEST_A), "test_b": list(mod.TEST_B), "formations": list(mod.FORMATIONS),
                "options": list(mod.OPTIONS), "options_0dte": list(mod.OPTIONS_0DTE), "fixed": mod.FIXED,
                "primary_guardrail": pg, "comparison_guardrails": cg}
        for k, v in live.items():
            if k not in doc:
                errs.append(f"{k} is not inside the hashed grid document (G1/G8/R5)")
            elif json.dumps(doc[k], sort_keys=True, default=str) != json.dumps(v, sort_keys=True, default=str):
                errs.append(f"hashed document {k!r} differs from the module's {k} (mutated after hashing?)")
        if mod.grid_sha256(doc) != mod.GRID_SHA256:
            errs.append("GRID_SHA256 is not the hash of the current grid document")
    return errs
