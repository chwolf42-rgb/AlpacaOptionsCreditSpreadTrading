"""Frozen research protocol.

Dates, grids, and selection rules were written down before any backtest
result was inspected. Do not add a grid point after seeing holdout P&L.
"""

from __future__ import annotations

from datetime import date

# Warmup bars feed indicators only. No entries fill before IS_START.
WARMUP_START = date(2016, 1, 4)
IS_START = date(2018, 1, 2)
IS_END = date(2022, 12, 30)

# Walk-forward trade window. Each row refits on the trailing window and
# then trades the next quarter with those parameters frozen.
# fit_start, fit_end, trade_start, trade_end
FOLDS: tuple[tuple[date, date, date, date], ...] = (
    (date(2021, 1, 4), date(2022, 12, 30), date(2023, 1, 3), date(2023, 3, 31)),
    (date(2021, 4, 1), date(2023, 3, 31), date(2023, 4, 3), date(2023, 6, 30)),
    (date(2021, 7, 1), date(2023, 6, 30), date(2023, 7, 3), date(2023, 9, 29)),
    (date(2021, 10, 3), date(2023, 9, 29), date(2023, 10, 2), date(2023, 12, 29)),
    (date(2022, 1, 3), date(2023, 12, 29), date(2024, 1, 2), date(2024, 3, 28)),
    (date(2022, 4, 1), date(2024, 3, 28), date(2024, 4, 1), date(2024, 6, 28)),
    (date(2022, 7, 1), date(2024, 6, 28), date(2024, 7, 1), date(2024, 9, 30)),
    (date(2022, 10, 3), date(2024, 9, 30), date(2024, 10, 1), date(2024, 12, 31)),
    (date(2023, 1, 3), date(2024, 12, 31), date(2025, 1, 2), date(2025, 3, 31)),
    (date(2023, 4, 3), date(2025, 3, 31), date(2025, 4, 1), date(2025, 6, 30)),
    (date(2023, 7, 3), date(2025, 6, 30), date(2025, 7, 1), date(2025, 9, 30)),
)

OOS_START = date(2023, 1, 3)
OOS_END = date(2025, 9, 30)

# One look. Parameters come from the trailing 24 months that end the
# session before this window. Nothing inside the window is used to pick.
HOLDOUT_FIT_START = date(2023, 10, 2)
HOLDOUT_FIT_END = date(2025, 9, 30)
HOLDOUT_START = date(2025, 10, 1)
HOLDOUT_END = date(2026, 9, 30)

ACCOUNT = 100_000.0
RISK_PCT = 0.005
MAX_GROSS = 99_750.0
MAX_SPREADS = 5
MAX_OPEN_RISK_PCT = 0.10
# About $0.02 per contract per side. A 2-leg spread pays this on each
# leg at entry and again at exit.
FEE_PER_CONTRACT_SIDE = 0.02
MULTIPLIER = 100
# The width model is a one-lot quote. Past this size the book walks, so
# the sizer will not pretend a 1-lot fill price is available in size.
# This does not raise the 0.5% risk budget; it only leaves risk unused
# when one-to-ten lots cannot spend the whole budget.
MAX_CONTRACTS = 10

# 3-month circular block bootstrap of monthly account returns.
BOOTSTRAP_BLOCK = 3
BOOTSTRAP_RESAMPLES = 5000
BOOTSTRAP_SEED = 20261004

# A fit window must produce this many closed trades or the textbook
# default (grid index 0) is kept. Stops a one-trade sample from winning.
MIN_FIT_TRADES = 8

UNDERLYINGS = (
    "SPY",
    "QQQ",
    "IWM",
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "META",
    "GOOGL",
    "TSLA",
)
INDEX_ETFS = ("SPY", "QQQ", "IWM")

# Constant dividend yields. Not fitted. Used in Black-Scholes-Merton so
# the spot series can stay split-adjusted only (not dividend-adjusted).
DIVIDENDS = {
    "SPY": 0.013,
    "QQQ": 0.006,
    "IWM": 0.012,
    "AAPL": 0.005,
    "MSFT": 0.008,
    "NVDA": 0.001,
    "AMZN": 0.000,
    "META": 0.003,
    "GOOGL": 0.003,
    "TSLA": 0.000,
}

# reason: debit spreads overpay when implied vol is rich. One extra
# pre-declared filter, not a search invented after the results.
DEBIT_GRID: tuple[dict, ...] = (
    {"sma": 50, "mom": 20, "long_delta": 0.55, "short_delta": 0.30, "dte": 30, "cheap_iv": False},
    {"sma": 50, "mom": 10, "long_delta": 0.55, "short_delta": 0.30, "dte": 30, "cheap_iv": False},
    {"sma": 20, "mom": 10, "long_delta": 0.50, "short_delta": 0.25, "dte": 21, "cheap_iv": False},
    {"sma": 50, "mom": 20, "long_delta": 0.50, "short_delta": 0.25, "dte": 42, "cheap_iv": False},
    {"sma": 50, "mom": 20, "long_delta": 0.55, "short_delta": 0.30, "dte": 30, "cheap_iv": True},
)

SHORT_DATED_GRID: tuple[dict, ...] = (
    {"lookback": 3, "thresh": 0.003, "sma": 20, "long_delta": 0.50, "short_delta": 0.30, "dte": 5},
    {"lookback": 5, "thresh": 0.005, "sma": 20, "long_delta": 0.50, "short_delta": 0.30, "dte": 5},
    {"lookback": 3, "thresh": 0.003, "sma": 20, "long_delta": 0.50, "short_delta": 0.30, "dte": 7},
    {"lookback": 5, "thresh": 0.005, "sma": 20, "long_delta": 0.50, "short_delta": 0.30, "dte": 7},
)

CONDOR_GRID: tuple[dict, ...] = (
    {"short_delta": 0.16, "dte": 35, "width": 5.0, "iv_mode": "vix_gt_sma20"},
    {"short_delta": 0.16, "dte": 45, "width": 5.0, "iv_mode": "vix_pct_60"},
    {"short_delta": 0.25, "dte": 35, "width": 5.0, "iv_mode": "vix_gt_sma20"},
    {"short_delta": 0.20, "dte": 30, "width": 5.0, "iv_mode": "contango"},
)

BUTTERFLY_GRID: tuple[dict, ...] = (
    {"width": 5.0, "dte": 21, "rv_filter": "rv10_lt_rv60"},
    {"width": 5.0, "dte": 35, "rv_filter": "rv10_lt_rv60"},
    {"width": 10.0, "dte": 21, "rv_filter": "near_sma"},
    {"width": 5.0, "dte": 21, "rv_filter": "vix_pct_lt_40"},
)

CALENDAR_GRID: tuple[dict, ...] = (
    {"front_dte": 14, "back_dte": 45, "ratio": 1.05, "band": 0.01},
    {"front_dte": 7, "back_dte": 30, "ratio": 1.08, "band": 0.015},
    {"front_dte": 14, "back_dte": 60, "ratio": 1.02, "band": 0.02},
    {"front_dte": 21, "back_dte": 45, "ratio": 1.05, "band": 0.01},
)

DIAGONAL_GRID: tuple[dict, ...] = (
    {"sma": 50, "front_delta": 0.30, "back_delta": 0.60, "front_dte": 14, "back_dte": 45},
    {"sma": 20, "front_delta": 0.25, "back_delta": 0.55, "front_dte": 7, "back_dte": 35},
    {"sma": 50, "front_delta": 0.35, "back_delta": 0.50, "front_dte": 14, "back_dte": 45},
)

GRIDS = {
    "debit_momentum": DEBIT_GRID,
    "short_dated": SHORT_DATED_GRID,
    "condor": CONDOR_GRID,
    "butterfly": BUTTERFLY_GRID,
    "calendar": CALENDAR_GRID,
    "diagonal": DIAGONAL_GRID,
}

UNIVERSE = {
    "debit_momentum": UNDERLYINGS,
    "short_dated": INDEX_ETFS,
    "condor": INDEX_ETFS,
    "butterfly": INDEX_ETFS,
    "calendar": INDEX_ETFS,
    "diagonal": INDEX_ETFS,
}

# Take-profit and stop fractions are textbook defaults, not a grid.
# Debit vertical: take 50% of max profit, stop at a loss of half the debit.
# Credit wing: take 50% of the credit, stop when the loss equals the credit
# (debit-to-close = 2x credit).
# Calendar: +25% / -50% of the debit. Diagonal: +50% / -50% of the debit.
EXIT_DTE = {
    "debit_momentum": 7,
    "short_dated": 1,
    "condor": 21,
    "butterfly": 7,
    "calendar": 2,
    "diagonal": 2,
}


def params_id(strategy: str, params: dict) -> str:
    parts = [strategy]
    for key in sorted(params):
        parts.append(f"{key}={params[key]}")
    return "|".join(parts)
