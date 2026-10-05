"""Frozen grids and constants (spec v1.3.1).

``grids.py`` is the single source for every frozen constant. Trial count
N is 450. The primary loss guardrail and the two comparison guardrails
are constants, not axes, so no grid row carries a guardrail key.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Mapping

from research.intraday_sr.io import read_bytes, read_text

SPEC_VERSION = "v1.3.1"

# v1.3.1 C3. The harness builds RiskCfg(max_losses_day, max_losses_week) from this.
PRIMARY_GUARDRAIL = {"daily_losses": 2, "weekly_losses": 5}
COMPARISON_GUARDRAILS = (
    {"name": "none"},
    {"name": "d2+w6", "daily_losses": 2, "weekly_losses": 6},
)

K_CLUSTER = 0.25
MAX_ENTRIES_PER_DAY = 12
MAX_CONCURRENT = 4
MAX_PER_SYMBOL = 1
DAILY_LOSS_STOP = -0.015
NO_NEW_ENTRIES_AFTER_ET = "15:00"
FORCED_EXIT_BAR_OPEN_ET = "15:55"
RISK_FRACTION = 0.005
MODEL_EQUITY = 100_000
NOTIONAL_CAP_POSITION = 1.0
NOTIONAL_CAP_TOTAL = 3.0
OPENING_RANGE_START_ET = "09:30"
OPENING_RANGE_END_ET = "10:00"
WARMUP_DATE = "2019-02-01"
DEV_START = "2019-01-02"
DEV_END = "2026-03-31"
HOLDOUT_START = "2026-04-01"
HOLDOUT_END = "2026-09-30"
STOP_BUFFER_ATR = 0.05
STOP_FLOOR_ATR = 0.10
ARM_ATR = 0.10
CANCEL_BARS = 6
ENTRY_OFFSET = 0.01
RVOL_SESSIONS = 20
RSI_LENGTH = 14
STOCH_K = 14
STOCH_D = 3
STOCH_SMOOTH = 3
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
INDICATOR_WARMUP_BARS = 100
ATR_LENGTH = 14
PROFILE_SESSIONS = 5
PROFILE_PERCENTILE = 0.70
PROFILE_BIN_ATR = 0.05
ZONE_PAD_ATR = 0.05
# SPEC v1.3.3 lock 2. Fixed, not a grid axis. It is not part of the
# canonical grid document, so GRID_SHA256 stays on the v1.3.1 axes.
MAX_ZONE_WIDTH_ATR = 1.0
CANDIDATE_BAND_ATR = 2.0
TOUCH_SESSIONS = 20
RECENCY_HALF_LIFE_SESSIONS = 5.0
ROUND_STEP_UNDER_50 = 1.0
ROUND_STEP_UNDER_250 = 5.0
ROUND_STEP_UNDER_1000 = 10.0
ROUND_STEP_ELSE = 50.0
BOOTSTRAP_SEED = 20260925
BOOTSTRAP_RESAMPLES = 5000
TOUCH_WINDOW_BARS = 3
FORMATION_PIVOT_GAP_MIN = 5
FORMATION_PIVOT_GAP_MAX = 60
SHOULDER_ATR = 0.10
HVN_REJECTION_WICK = 0.50
PIVOT_N = {"5m": 3, "15m": 3, "1h": 2, "1d": 2}
SCORE_WEIGHTS = {
    "touches": 0.30,
    "rejections": 0.30,
    "recency": 0.20,
    "volume": 0.20,
}

_UNIVERSE_PATH = Path(__file__).resolve().parent / "universe_fixed33.json"
_ROW_GUARD_KEYS = (
    "daily_losses",
    "weekly_losses",
    "guardrail",
    "max_losses_day",
    "max_losses_week",
    "k_cluster",
)

_K = (3, 5)
_OSCILLATORS = ("rsi14_30_70", "stoch14_3_3_20_80")
_RVOL = (1.5, 2.0)
_ENTRY_TF = ("5m", "15m")
_TARGETS = ("1R", "2R", "zone")
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
    """sha256 of the universe file bytes."""
    target = _UNIVERSE_PATH if path is None else Path(path)
    return hashlib.sha256(read_bytes(target)).hexdigest()


def load_universe_symbols(path: Path | None = None) -> tuple[str, ...]:
    """Symbol list from ``universe_fixed33.json``. No other universe file is read."""
    target = _UNIVERSE_PATH if path is None else Path(path)
    payload = json.loads(read_text(target))
    symbols = tuple(payload["symbols"])
    if len(symbols) != 33 or len(set(symbols)) != 33:
        raise RuntimeError("universe file must list 33 distinct symbols")
    if symbols[:3] != ("SPY", "QQQ", "IWM"):
        raise RuntimeError("universe must start with SPY QQQ IWM")
    return symbols


def frozen_engine_values() -> dict:
    """Frozen ``EngineCfg`` fields. ``k_zones`` is the only variant field."""
    return {
        "k_cluster": K_CLUSTER,
        "n_5m": PIVOT_N["5m"],
        "n_15m": PIVOT_N["15m"],
        "n_1h": PIVOT_N["1h"],
        "n_1d": PIVOT_N["1d"],
        "atr_length": ATR_LENGTH,
        "profile_sessions": PROFILE_SESSIONS,
        "profile_percentile": PROFILE_PERCENTILE,
        "profile_bin_atr": PROFILE_BIN_ATR,
        "zone_pad_atr": ZONE_PAD_ATR,
        "candidate_band_atr": CANDIDATE_BAND_ATR,
        "touch_sessions": TOUCH_SESSIONS,
        "recency_half_life_sessions": RECENCY_HALF_LIFE_SESSIONS,
        "score_touches": SCORE_WEIGHTS["touches"],
        "score_rejections": SCORE_WEIGHTS["rejections"],
        "score_recency": SCORE_WEIGHTS["recency"],
        "score_volume": SCORE_WEIGHTS["volume"],
        "opening_range_start_et": OPENING_RANGE_START_ET,
        "opening_range_end_et": OPENING_RANGE_END_ET,
        "warmup_date": WARMUP_DATE,
        "dev_start": DEV_START,
        "dev_end": DEV_END,
        "holdout_start": HOLDOUT_START,
        "holdout_end": HOLDOUT_END,
        "no_new_entries_after_et": NO_NEW_ENTRIES_AFTER_ET,
        "forced_exit_bar_open_et": FORCED_EXIT_BAR_OPEN_ET,
        "stop_buffer_atr": STOP_BUFFER_ATR,
        "stop_floor_atr": STOP_FLOOR_ATR,
        "arm_atr": ARM_ATR,
        "cancel_bars": CANCEL_BARS,
        "entry_offset": ENTRY_OFFSET,
        "risk_fraction": RISK_FRACTION,
        "notional_cap_position": NOTIONAL_CAP_POSITION,
        "notional_cap_total": NOTIONAL_CAP_TOTAL,
        "model_equity": MODEL_EQUITY,
        "max_entries_per_day": MAX_ENTRIES_PER_DAY,
        "max_concurrent": MAX_CONCURRENT,
        "max_per_symbol": MAX_PER_SYMBOL,
        "daily_loss_stop": DAILY_LOSS_STOP,
        "max_losses_day": PRIMARY_GUARDRAIL["daily_losses"],
        "max_losses_week": PRIMARY_GUARDRAIL["weekly_losses"],
        "rvol_sessions": RVOL_SESSIONS,
        "rsi_length": RSI_LENGTH,
        "stoch_k": STOCH_K,
        "stoch_d": STOCH_D,
        "stoch_smooth": STOCH_SMOOTH,
        "macd_fast": MACD_FAST,
        "macd_slow": MACD_SLOW,
        "macd_signal": MACD_SIGNAL,
        "indicator_warmup_bars": INDICATOR_WARMUP_BARS,
        "round_step_under_50": ROUND_STEP_UNDER_50,
        "round_step_under_250": ROUND_STEP_UNDER_250,
        "round_step_under_1000": ROUND_STEP_UNDER_1000,
        "round_step_else": ROUND_STEP_ELSE,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
        "touch_window_bars": TOUCH_WINDOW_BARS,
        "formation_pivot_gap_min": FORMATION_PIVOT_GAP_MIN,
        "formation_pivot_gap_max": FORMATION_PIVOT_GAP_MAX,
        "shoulder_atr": SHOULDER_ATR,
        "hvn_rejection_wick": HVN_REJECTION_WICK,
    }


UNIVERSE: tuple[str, ...] = load_universe_symbols()


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

FIXED: dict = {
    "primary_guardrail": dict(PRIMARY_GUARDRAIL),
    "k_cluster_atr": K_CLUSTER,
    "entries_per_day": MAX_ENTRIES_PER_DAY,
    "max_concurrent": MAX_CONCURRENT,
    "max_per_symbol": MAX_PER_SYMBOL,
    "daily_loss_stop": DAILY_LOSS_STOP,
    "no_new_entries_after_et": NO_NEW_ENTRIES_AFTER_ET,
    "forced_exit_bar_open_et": FORCED_EXIT_BAR_OPEN_ET,
    "risk_fraction": RISK_FRACTION,
    "model_equity": MODEL_EQUITY,
    "pivot_n": dict(PIVOT_N),
    "score_weights": dict(SCORE_WEIGHTS),
    "engine": frozen_engine_values(),
}


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
    if PRIMARY_GUARDRAIL != {"daily_losses": 2, "weekly_losses": 5}:
        raise RuntimeError("PRIMARY_GUARDRAIL must be daily_losses 2 and weekly_losses 5")
    if COMPARISON_GUARDRAILS != (
        {"name": "none"},
        {"name": "d2+w6", "daily_losses": 2, "weekly_losses": 6},
    ):
        raise RuntimeError("COMPARISON_GUARDRAILS drifted")
    if SPEC_VERSION != "v1.3.1":
        raise RuntimeError("spec_version must be v1.3.1")
    rows = TEST_A + TEST_B + FORMATIONS + OPTIONS + OPTIONS_0DTE
    for row in rows:
        for key in _ROW_GUARD_KEYS:
            if key in row:
                raise RuntimeError(f"{key} must not be a grid-row key")
        if "k_confirm" in row and row["k_confirm"] not in _K_CONFIRM:
            raise RuntimeError("k_confirm outside {0, 1, 2, 3}")


def grid_document() -> dict:
    """Canonical document. Includes the default engine config and the universe hash."""
    from dataclasses import asdict

    from research.intraday_sr.types import EngineCfg

    return {
        "spec_version": SPEC_VERSION,
        "universe_sha256": universe_sha256(),
        "primary_guardrail": dict(PRIMARY_GUARDRAIL),
        "comparison_guardrails": [dict(row) for row in COMPARISON_GUARDRAILS],
        "fixed": FIXED,
        "engine_cfg": asdict(EngineCfg()),
        "formations": list(FORMATIONS),
        "options": list(OPTIONS),
        "options_0dte": list(OPTIONS_0DTE),
        "test_a": list(TEST_A),
        "test_b": list(TEST_B),
        "universe": list(UNIVERSE),
    }


def canonical_json(document: Mapping | None = None) -> str:
    payload = grid_document() if document is None else document
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def grid_sha256(document: Mapping | None = None) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def __getattr__(name: str):
    if name == "GRID_SHA256":
        return grid_sha256()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_enforce_caps()


def iter_variants(rows: Iterable[Mapping]) -> Iterable[Mapping]:
    return iter(rows)


def format_grid_summary() -> str:
    """Printable summary of the frozen grids."""
    digest = grid_sha256()
    lines = [
        "intraday-sr grids (spec v1.3.1; axes unchanged from v1.1 A1-A2)",
        f"sha256 {digest}",
        f"spec_version {SPEC_VERSION}",
        f"universe_sha256 {universe_sha256()}",
        f"N {N_TRIALS}",
        (
            "primary_guardrail "
            f"daily_losses={PRIMARY_GUARDRAIL['daily_losses']} "
            f"weekly_losses={PRIMARY_GUARDRAIL['weekly_losses']}"
        ),
        "comparison_guardrails none, d2+w6",
        f"test_a {len(TEST_A)}  (cap {CAP_TEST_A})",
        f"test_b {len(TEST_B)}  (cap {CAP_TEST_B})",
        (
            "test_a/b axes: K{3,5} x oscillator{rsi14_30_70, stoch14_3_3_20_80} "
            "x rvol_min{1.5,2.0} x entry_tf{5m,15m} x target{1R,2R,zone} "
            "x k_confirm{0,1,2,3}"
        ),
        f"formations {len(FORMATIONS)}  (cap {CAP_FORMATIONS})",
        "formations axes: kind{W,IHS,M,HS} x entry_tf{5m,15m} x target{1R,2R,zone} x pivot_tol_atr{0.15,0.25}",
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
