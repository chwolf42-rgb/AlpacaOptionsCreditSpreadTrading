"""TEST-ONLY stand-in for Developer 2's grids.py until PR #20 lands (loaded by harness/s0grids.py only when
INTRADAY_SR_GRIDS_STANDIN=1, which research/intraday_sr/conftest.py sets for the test run). It mirrors #20 with the
CP0 rulings applied (target label "zone", PRIMARY_GUARDRAIL dict, COMPARISON_GUARDRAILS in the hash). Production
code never loads it: a real grids.py always wins, and without one the shim raises. Delete when #20 is merged."""
from __future__ import annotations

import hashlib
import json

PRIMARY_GUARDRAIL = {"daily_losses": 2, "weekly_losses": 5}
COMPARISON_GUARDRAILS = ({"name": "none"}, {"name": "d2+w6", "daily_losses": 2, "weekly_losses": 6})
FIXED: dict = {"k_cluster_atr": 0.25, "entries_per_day": 12, "max_concurrent": 4, "max_per_symbol": 1,
               "daily_loss_stop": -0.015, "pivot_n": {"5m": 3, "15m": 3, "1h": 2, "1d": 2},
               "score_weights": {"touches": 0.30, "rejections": 0.30, "recency": 0.20, "volume": 0.20}}


def _equity(test):
    rows = [{"test": test, "K": k, "oscillator": o, "rvol_min": r, "entry_tf": tf, "target": t, "k_confirm": kc,
             "variant_id": f"{test}-K{k}-{o}-rvol{r}-{tf}-{t}-k{kc}"}
            for k in (3, 5) for o in ("rsi14_30_70", "stoch14_3_3_20_80") for r in (1.5, 2.0) for tf in ("5m", "15m")
            for t in ("1R", "2R", "zone") for kc in (0, 1, 2, 3)]
    return tuple(sorted(rows, key=lambda x: x["variant_id"]))


TEST_A, TEST_B = _equity("A"), _equity("B")
FORMATIONS = tuple(sorted(({"test": f"F_{k}", "kind": k, "entry_tf": tf, "target": t, "pivot_tol_atr": tol,
                            "variant_id": f"F_{k}-{tf}-{t}-tol{tol:.2f}"}
                           for k in ("W", "IHS", "M", "HS") for tf in ("5m", "15m") for t in ("1R", "2R", "zone")
                           for tol in (0.15, 0.25)), key=lambda x: x["variant_id"]))
OPTIONS = tuple(sorted(({"grid": "options", "structure": s, "dte_bucket": d, "variant_id": f"OPT-{s}-dte{d}"}
                        for s in ("long_atm", "long_otm_1", "debit_vertical") for d in ("0-1", "2-4", "5-7")),
                       key=lambda x: x["variant_id"]))
OPTIONS_0DTE = tuple(sorted(({"grid": "options_0dte", "stop_pct": s, "take_profit_pct": tp, "time_exit_et": "15:45",
                              "premium_dollars": 2000, "variant_id": f"OPT0DTE-stop{s}-tp{tp}-exit15:45"}
                             for s in (-30, -40, -50) for tp in (50, 65, 80)), key=lambda x: x["variant_id"]))
N_TRIALS = 450


def grid_document() -> dict:
    return {"fixed": FIXED, "formations": list(FORMATIONS), "options": list(OPTIONS), "options_0dte": list(OPTIONS_0DTE),
            "test_a": list(TEST_A), "test_b": list(TEST_B), "primary_guardrail": PRIMARY_GUARDRAIL,
            "comparison_guardrails": list(COMPARISON_GUARDRAILS), "spec_version": "v1.3.1"}


def canonical_json(document=None) -> str:
    return json.dumps(grid_document() if document is None else document, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def grid_sha256(document=None) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


GRID_SHA256 = grid_sha256()
