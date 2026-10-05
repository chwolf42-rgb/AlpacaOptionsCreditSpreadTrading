"""Statistics (SPEC sections 6 and 8): day-block and month-block bootstraps (5,000 resamples, seed 20260925,
95% percentile intervals), Sharpe, deflated Sharpe ratio (Bailey & Lopez de Prado 2014)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, norm, skew

SEED = 20260925
RESAMPLES = 5000
ALPHA = 0.05
EULER_GAMMA = 0.5772156649015329


def _ci(samples: np.ndarray, point: float, n_blocks: int, alpha: float = ALPHA) -> dict:
    lo, hi = np.quantile(samples, [alpha / 2, 1 - alpha / 2]) if samples.size else (np.nan, np.nan)
    return {"mean": float(point), "lo": float(lo), "hi": float(hi), "n_blocks": int(n_blocks),
            "resamples": int(samples.size), "seed": SEED, "level": 1 - alpha}


def day_block_mean_r(r: np.ndarray, day: np.ndarray, resamples: int = RESAMPLES, seed: int = SEED,
                     alpha: float = ALPHA) -> dict:
    """Mean R per trade; blocks = trading days (trades within a day stay together). Days drawn iid with
    replacement; the statistic is sum(R)/count over the drawn days' trades."""
    r = np.asarray(r, float)
    if r.size == 0:
        return {"mean": None, "lo": None, "hi": None, "n_blocks": 0, "resamples": 0, "seed": seed, "level": 1 - alpha}
    codes, uniq = pd.factorize(pd.Series(day))
    nb = len(uniq)
    sums = np.bincount(codes, weights=r, minlength=nb)
    cnts = np.bincount(codes, minlength=nb).astype(float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, nb, size=(resamples, nb))
    s = sums[idx].sum(axis=1)
    c = cnts[idx].sum(axis=1)
    return _ci(s / c, r.mean(), nb, alpha)


def block_mean(values: np.ndarray, resamples: int = RESAMPLES, seed: int = SEED, alpha: float = ALPHA) -> dict:
    """Mean of per-block values (e.g. monthly returns, blocks = months), blocks drawn iid with replacement."""
    v = np.asarray(values, float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return {"mean": None, "lo": None, "hi": None, "n_blocks": 0, "resamples": 0, "seed": seed, "level": 1 - alpha}
    rng = np.random.default_rng(seed)
    m = v[rng.integers(0, v.size, size=(resamples, v.size))].mean(axis=1)
    return _ci(m, v.mean(), v.size, alpha)


def monthly_returns(daily: pd.Series) -> pd.Series:
    if daily.empty:
        return daily
    idx = pd.DatetimeIndex(pd.to_datetime(daily.index))
    return (1 + pd.Series(daily.to_numpy(), index=idx)).groupby(idx.to_period("M")).prod() - 1


def max_drawdown(daily: pd.Series) -> float:
    if daily.empty:
        return float("nan")
    eq = np.r_[1.0, (1 + daily.to_numpy()).cumprod()]
    return float(np.max(1 - eq / np.maximum.accumulate(eq)))


def sharpe(daily: pd.Series) -> float:
    x = np.asarray(daily, float)
    if x.size < 2 or x.std(ddof=1) == 0:
        return float("nan")
    return float(x.mean() / x.std(ddof=1))


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    if n_trials <= 1:
        return 0.0
    return math.sqrt(var_sr) * ((1 - EULER_GAMMA) * norm.ppf(1 - 1 / n_trials)
                                + EULER_GAMMA * norm.ppf(1 - 1 / (n_trials * math.e)))


def deflated_sharpe(daily: pd.Series, n_trials: int, var_sr_trials: float | None = None) -> dict:
    """DSR on daily returns (non-annualized SR). var_sr_trials = variance of the trials' daily Sharpes (from the
    trial log); if absent, the null variance 1/T is used (documented as a fallback)."""
    x = np.asarray(daily, float)
    T = x.size
    sr = sharpe(daily)
    if not np.isfinite(sr) or T < 3:
        return {"sr_daily": sr, "dsr": None, "n_trials": n_trials, "T": T}
    v = var_sr_trials if (var_sr_trials is not None and np.isfinite(var_sr_trials) and var_sr_trials > 0) else 1.0 / T
    sr0 = expected_max_sharpe(n_trials, v)
    g3 = float(skew(x))
    g4 = float(kurtosis(x, fisher=False))
    denom = math.sqrt(max(1 - g3 * sr + (g4 - 1) / 4 * sr * sr, 1e-12))
    z = (sr - sr0) * math.sqrt(T - 1) / denom
    return {"sr_daily": sr, "sr0": sr0, "dsr": float(norm.cdf(z)), "n_trials": n_trials, "T": T,
            "skew": g3, "kurt": g4, "var_sr_source": "trial log" if var_sr_trials else "null 1/T fallback"}


def trade_summary(r: np.ndarray, pnl: np.ndarray, day: np.ndarray, n_sessions: int, daily: pd.Series) -> dict:
    r = np.asarray(r, float)
    pnl = np.asarray(pnl, float)
    wins, losses = r[pnl > 0], r[pnl <= 0]
    m = monthly_returns(daily)
    gp, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    return {
        "trades": int(r.size),
        "trades_per_day": float(r.size / max(n_sessions, 1)),
        "trades_per_month": float(r.size / max(len(m), 1)),
        "win_rate": float((pnl > 0).mean()) if r.size else None,
        "avg_win_r": float(wins.mean()) if wins.size else None,
        "avg_loss_r": float(losses.mean()) if losses.size else None,
        "mean_r_ci": day_block_mean_r(r, day),
        "profit_factor": float(gp / gl) if gl > 0 else None,
        "monthly_ci": block_mean(m.to_numpy()),
        "months": int(len(m)),
        "max_drawdown": max_drawdown(daily),
        "sharpe_daily": sharpe(daily),
        "worst_day": float(daily.min()) if len(daily) else None,
    }
