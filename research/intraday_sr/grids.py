"""Frozen §5 grids (spec v1.1 A1–A2, unchanged in v1.3).

Caps are enforced at import. The sha256 is of the canonical JSON of the
equity grids, both options grids, the fixed constants, the locked
universe, and ``PRIMARY_GUARDRAIL``. Trial count N is 450. v1.3 adds the
guardrail to the hash and adds no trials. The universe is read only from
``universe_fixed33.json`` in this package.

``k_confirm`` is how many of the three optional stack conditions a variant
requires (at least that many). ``Signal.confluence`` is how many held.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Mapping

# Fixed engine / portfolio constants. Not axes.
# v1.3 primary loss guardrail: max losing trades per day, then per week.
# Not a grid axis. Comparison configs stay on the harness side.
PRIMARY_GUARDRAIL = (2, 5)
K_CLUSTER = 0.25
MAX_ENTRIES_PER_DAY = 12
MAX_CONCURRENT = 4
MAX_PER_SYMBOL = 1
DAILY_LOSS_STOP = -0.015
PIVOT_N = {"5m": 3, "15m": 3, "1h": 2, "1d": 2}
SCORE_WEIGHTS = {
    "touches": 0.30,
    "rejections": 0.30,
    "recency": 0.20,
    "volume": 0.20,
}

# Locked list lives in universe_fixed33.json (verbatim). Cache files store BRK.B as BRK-B.
_UNIVERSE_PATH = Path(__file__).resolve().parent / "universe_fixed33.json"

_K = (3, 5)
_OSCILLATORS = ("rsi14_30_70", "stoch14_3_3_20_80")
_RVOL = (1.5, 2.0)
_ENTRY_TF = ("5m", "15m")
_TARGETS = ("1R", "2R", "next_zone")
_K_CONFIRM = (0, 1, 2, 3)
_FORMATION_KINDS = ("W", "IHS", "M", "HS")
_PIVOT_TOL = (0.15, 0.25)
_OPTION_STRUCTURES = ("long_atm", "long_otm_1", "debit_vertical")
_OPTION_DTE = ("0-1", "2-4", "5-7")
_ZERO_DTE_STOPS = (-30, -40, -50)
_ZERO_DTE_TP = (50, 65, 80)
_ZERO_DTE_TIME_EXIT = "15:45"

CAP_TEST_A = 192
CAP_TEST_B = 192
CAP_FORMATIONS = 48
CAP_OPTIONS = 9
CAP_OPTIONS_0DTE = 9
CAP_OPTIONS_TOTAL = 18
N_TRIALS = CAP_TEST_A + CAP_TEST_B + CAP_FORMATIONS + CAP_OPTIONS_TOTAL


def universe_path() -> Path:
    """Path of the committed fixed-33 universe file."""
    return _UNIVERSE_PATH


def universe_sha256(path: Path | None = None) -> str:
    """sha256 of the universe file bytes. This is the value for the FREEZE manifest."""
    target = _UNIVERSE_PATH if path is None else Path(path)
    return hashlib.sha256(target.read_bytes()).hexdigest()


def load_universe_symbols(path: Path | None = None) -> tuple[str, ...]:
    """Symbol list from ``universe_fixed33.json``. No other universe file is read."""
    target = _UNIVERSE_PATH if path is None else Path(path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    symbols = tuple(payload["symbols"])
    if len(symbols) != 33 or len(set(symbols)) != 33:
        raise RuntimeError("universe file must list 33 distinct symbols")
    if symbols[:3] != ("SPY", "QQQ", "IWM"):
        raise RuntimeError("universe must start with SPY QQQ IWM")
    return symbols


UNIVERSE: tuple[str, ...] = load_universe_symbols()

FIXED: dict = {
    "k_cluster_atr": K_CLUSTER,
    "entries_per_day": MAX_ENTRIES_PER_DAY,
    "max_concurrent": MAX_CONCURRENT,
    "max_per_symbol": MAX_PER_SYMBOL,
    "daily_loss_stop": DAILY_LOSS_STOP,
    "pivot_n": dict(PIVOT_N),
    "score_weights": dict(SCORE_WEIGHTS),
}


def _equity_grid(test: str) -> tuple[dict, ...]:
    rows: list[dict] = []
    for k_zones in _K:
        for oscillator in _OSCILLATORS:
            for rvol in _RVOL:
                for entry_tf in _ENTRY_TF:
                    for target in _TARGETS:
                        for k_confirm in _K_CONFIRM:
                            rvol_label = f"{rvol:.1f}"
                            variant_id = (
                                f"{test}-K{k_zones}-{oscillator}-rvol{rvol_label}"
                                f"-{entry_tf}-{target}-k{k_confirm}"
                            )
                            rows.append(
                                {
                                    "test": test,
                                    "K": k_zones,
                                    "oscillator": oscillator,
                                    "rvol_min": rvol,
                                    "entry_tf": entry_tf,
                                    "target": target,
                                    "k_confirm": k_confirm,
                                    "variant_id": variant_id,
                                }
                            )
    rows.sort(key=lambda row: row["variant_id"])
    return tuple(rows)


def _formation_grid() -> tuple[dict, ...]:
    rows: list[dict] = []
    for kind in _FORMATION_KINDS:
        for entry_tf in _ENTRY_TF:
            for target in _TARGETS:
                for tol in _PIVOT_TOL:
                    test = f"F_{kind}"
                    variant_id = f"{test}-{entry_tf}-{target}-tol{tol:.2f}"
                    rows.append(
                        {
                            "test": test,
                            "kind": kind,
                            "entry_tf": entry_tf,
                            "target": target,
                            "pivot_tol_atr": tol,
                            "variant_id": variant_id,
                        }
                    )
    rows.sort(key=lambda row: row["variant_id"])
    return tuple(rows)


def _options_grid() -> tuple[dict, ...]:
    rows: list[dict] = []
    for structure in _OPTION_STRUCTURES:
        for dte in _OPTION_DTE:
            variant_id = f"OPT-{structure}-dte{dte}"
            rows.append(
                {
                    "grid": "options",
                    "structure": structure,
                    "dte_bucket": dte,
                    "variant_id": variant_id,
                }
            )
    rows.sort(key=lambda row: row["variant_id"])
    return tuple(rows)


def _options_0dte_grid() -> tuple[dict, ...]:
    rows: list[dict] = []
    for stop in _ZERO_DTE_STOPS:
        for take_profit in _ZERO_DTE_TP:
            variant_id = f"OPT0DTE-stop{stop}-tp{take_profit}-exit{_ZERO_DTE_TIME_EXIT}"
            rows.append(
                {
                    "grid": "options_0dte",
                    "stop_pct": stop,
                    "take_profit_pct": take_profit,
                    "time_exit_et": _ZERO_DTE_TIME_EXIT,
                    "premium_dollars": 2000,
                    "variant_id": variant_id,
                }
            )
    rows.sort(key=lambda row: row["variant_id"])
    return tuple(rows)


TEST_A: tuple[dict, ...] = _equity_grid("A")
TEST_B: tuple[dict, ...] = _equity_grid("B")
FORMATIONS: tuple[dict, ...] = _formation_grid()
OPTIONS: tuple[dict, ...] = _options_grid()
OPTIONS_0DTE: tuple[dict, ...] = _options_0dte_grid()


def _enforce_caps() -> None:
    if len(TEST_A) != CAP_TEST_A:
        raise RuntimeError(f"Test A has {len(TEST_A)} variants; cap is {CAP_TEST_A}")
    if len(TEST_B) != CAP_TEST_B:
        raise RuntimeError(f"Test B has {len(TEST_B)} variants; cap is {CAP_TEST_B}")
    if len(FORMATIONS) != CAP_FORMATIONS:
        raise RuntimeError(f"formations grid has {len(FORMATIONS)}; cap is {CAP_FORMATIONS}")
    if len(OPTIONS) != CAP_OPTIONS:
        raise RuntimeError(f"options grid has {len(OPTIONS)}; cap is {CAP_OPTIONS}")
    if len(OPTIONS_0DTE) != CAP_OPTIONS_0DTE:
        raise RuntimeError(f"0DTE grid has {len(OPTIONS_0DTE)}; cap is {CAP_OPTIONS_0DTE}")
    if len(OPTIONS) + len(OPTIONS_0DTE) != CAP_OPTIONS_TOTAL:
        raise RuntimeError("options variants must total 18")
    n_trials = len(TEST_A) + len(TEST_B) + len(FORMATIONS) + len(OPTIONS) + len(OPTIONS_0DTE)
    if n_trials != N_TRIALS or N_TRIALS != 450:
        raise RuntimeError(f"trial count N must be 450, got {n_trials}")
    if len(UNIVERSE) != 33 or len(set(UNIVERSE)) != 33:
        raise RuntimeError("universe must be 33 distinct symbols")
    if K_CLUSTER != 0.25:
        raise RuntimeError("k_cluster is fixed at 0.25")
    if PRIMARY_GUARDRAIL != (2, 5):
        raise RuntimeError("PRIMARY_GUARDRAIL must be (2, 5)")
    for row in TEST_A + TEST_B:
        if "k_cluster" in row:
            raise RuntimeError("k_cluster must not be a grid axis")
        if row["k_confirm"] not in _K_CONFIRM:
            raise RuntimeError("k_confirm outside {0, 1, 2, 3}")


def grid_document() -> dict:
    """JSON-ready document. Key order does not matter; ``canonical_json`` sorts."""
    return {
        "fixed": FIXED,
        "formations": list(FORMATIONS),
        "options": list(OPTIONS),
        "options_0dte": list(OPTIONS_0DTE),
        "test_a": list(TEST_A),
        "test_b": list(TEST_B),
        "universe": list(UNIVERSE),
        "primary_guardrail": [int(PRIMARY_GUARDRAIL[0]), int(PRIMARY_GUARDRAIL[1])],
    }


def canonical_json(document: Mapping | None = None) -> str:
    payload = grid_document() if document is None else document
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def grid_sha256(document: Mapping | None = None) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


_enforce_caps()
GRID_SHA256 = grid_sha256()


def iter_variants(rows: Iterable[Mapping]) -> Iterable[Mapping]:
    return iter(rows)


def format_grid_summary() -> str:
    """Printable CP0 summary of both equity and options grids."""
    lines = [
        "intraday-sr grids (spec v1.3; axes unchanged from v1.1 A1-A2)",
        f"sha256 {GRID_SHA256}",
        f"universe_sha256 {universe_sha256()}",
        f"N {N_TRIALS}",
        f"primary_guardrail max_losses_day={PRIMARY_GUARDRAIL[0]} max_losses_week={PRIMARY_GUARDRAIL[1]}",
        f"test_a {len(TEST_A)}  (cap {CAP_TEST_A})",
        f"test_b {len(TEST_B)}  (cap {CAP_TEST_B})",
        (
            "test_a/b axes: K{3,5} x oscillator{rsi14_30_70, stoch14_3_3_20_80} "
            "x rvol_min{1.5,2.0} x entry_tf{5m,15m} x target{1R,2R,next_zone} "
            "x k_confirm{0,1,2,3}"
        ),
        f"formations {len(FORMATIONS)}  (cap {CAP_FORMATIONS})",
        "formations axes: kind{W,IHS,M,HS} x entry_tf{5m,15m} x target{1R,2R,next_zone} x pivot_tol_atr{0.15,0.25}",
        f"options {len(OPTIONS)}  structure{{long_atm, long_otm_1, debit_vertical}} x dte{{0-1, 2-4, 5-7}}",
        (
            f"options_0dte {len(OPTIONS_0DTE)}  stop_pct{{-30,-40,-50}} x "
            f"take_profit_pct{{50,65,80}} x time_exit {_ZERO_DTE_TIME_EXIT} ET "
            "(premium $2,000 fixed, not an axis)"
        ),
        f"options_total {len(OPTIONS) + len(OPTIONS_0DTE)}",
        (
            f"fixed: k_cluster {K_CLUSTER} ATR_d, entries/day {MAX_ENTRIES_PER_DAY}, "
            f"concurrent {MAX_CONCURRENT}, per_symbol {MAX_PER_SYMBOL}, "
            f"daily_loss_stop {DAILY_LOSS_STOP}"
        ),
        f"pivot_n {PIVOT_N}",
        f"score_weights {SCORE_WEIGHTS}",
        f"universe {len(UNIVERSE)}: {' '.join(UNIVERSE)}",
        f"sample A {TEST_A[0]['variant_id']}",
        f"sample B {TEST_B[0]['variant_id']}",
        f"sample F {FORMATIONS[0]['variant_id']}",
        f"sample OPT {OPTIONS[0]['variant_id']}",
        f"sample 0DTE {OPTIONS_0DTE[0]['variant_id']}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_grid_summary())
