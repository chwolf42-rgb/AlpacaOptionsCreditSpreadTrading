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
PRIMARY_GUARDRAIL = (2, 5)          # G1: RiskCfg(max_losses_day=2, max_losses_week=5) as grids.PRIMARY_GUARDRAIL
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
    for K, osc, rv, tf, tgt, kc in itertools.product((3, 5), ("rsi14_30_70", "stoch_14_3_3_20_80"), (1.5, 2.0),
                                                      ("5m", "15m"), ("1R", "2R", "zone"), (0, 1, 2, 3)):
        out.append(dict(test=test, K=K, oscillator=osc, rvol_min=rv, tf=tf, target=tgt, k_confirm=kc,
                        k_cluster=K_CLUSTER_FIXED, variant_id=f"{test}-K{K}-{osc}-rv{rv}-{tf}-{tgt}-kc{kc}"))
    return out


def _guardrail_tuple(g) -> tuple | None:
    if g is None:
        return None
    if isinstance(g, Mapping):
        return (g.get("max_losses_day"), g.get("max_losses_week"))
    if isinstance(g, (tuple, list)):
        return tuple(g)
    return (getattr(g, "max_losses_day", None), getattr(g, "max_losses_week", None))


def check_grids_module(mod) -> list[str]:
    """Check Developer 2's grids module (TEST_A, TEST_B, FORMATIONS, OPTIONS, OPTIONS_0DTE, PRIMARY_GUARDRAIL)."""
    errs = check_all(mod.TEST_A, mod.TEST_B, getattr(mod, "FORMATIONS", None), getattr(mod, "OPTIONS", None),
                     getattr(mod, "OPTIONS_0DTE", None))
    n = sum(len(getattr(mod, k, ())) for k in ("TEST_A", "TEST_B", "FORMATIONS", "OPTIONS", "OPTIONS_0DTE"))
    if n != N_TOTAL:
        errs.append(f"total variants {n} != N = {N_TOTAL}")
    g = _guardrail_tuple(getattr(mod, "PRIMARY_GUARDRAIL", None))
    if g != PRIMARY_GUARDRAIL:
        errs.append(f"PRIMARY_GUARDRAIL {g} != {PRIMARY_GUARDRAIL} (SPEC v1.3 G1: must be a grids.py constant)")
    doc = mod.grid_document() if hasattr(mod, "grid_document") else {}
    if not any("guardrail" in str(k).lower() for k in doc) and \
            "max_losses_day" not in json.dumps(doc, default=str):
        errs.append("PRIMARY_GUARDRAIL is not inside the hashed grid document (G1/G8: grid hash must include it)")
    return errs
