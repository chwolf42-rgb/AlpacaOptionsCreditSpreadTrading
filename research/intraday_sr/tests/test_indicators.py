"""Numba ports must match the pure-Python indicators to 1e-5 (spec §4.10)."""

from __future__ import annotations

import numpy as np

from research.intraday_sr.engine.indicators import (
    _NUMBA_MACD,
    _NUMBA_RSI,
    _NUMBA_STOCH,
    _macd_python,
    _rsi_python,
    _stoch_python,
    rvol,
    using_numba,
)


def _close(left: np.ndarray, right: np.ndarray) -> None:
    assert left.shape == right.shape
    both_nan = np.isnan(left) & np.isnan(right)
    assert np.allclose(left[~both_nan], right[~both_nan], atol=1e-5, rtol=0.0)
    assert not (np.isnan(left) ^ np.isnan(right)).any()


def test_numba_ports_match_python():
    assert using_numba()
    assert _NUMBA_RSI is not None and _NUMBA_STOCH is not None and _NUMBA_MACD is not None
    rng = np.random.default_rng(20261004)
    close = 100.0 + np.cumsum(rng.normal(0.0, 0.35, size=420))
    high = close + rng.uniform(0.02, 0.9, size=close.shape[0])
    low = close - rng.uniform(0.02, 0.9, size=close.shape[0])
    # A flat window must be 50, and a one-way run must not divide by zero.
    high[40:55] = 50.0
    low[40:55] = 50.0
    close[40:55] = 50.0
    close[80:100] = np.linspace(close[79], close[79] + 4.0, 20)

    _close(_rsi_python(close, 14), _NUMBA_RSI(close, 14))
    k_py, d_py = _stoch_python(high, low, close, 14, 3, 3)
    k_nb, d_nb = _NUMBA_STOCH(high, low, close, 14, 3, 3)
    _close(k_py, k_nb)
    _close(d_py, d_nb)
    line_py, signal_py, hist_py = _macd_python(close, 12, 26, 9)
    line_nb, signal_nb, hist_nb = _NUMBA_MACD(close, 12, 26, 9)
    _close(line_py, line_nb)
    _close(signal_py, signal_nb)
    _close(hist_py, hist_nb)

    short = close[:10]
    _close(_rsi_python(short, 14), _NUMBA_RSI(short, 14))
    _close(_macd_python(short, 12, 26, 9)[2], _NUMBA_MACD(short, 12, 26, 9)[2])


def test_rvol_uses_prior_sessions_only():
    # One slot, 25 sessions. Row 20's median is the first 20 volumes, not itself.
    volume = np.arange(1, 26, dtype=np.float64)
    session = np.arange(25)
    slot = np.zeros(25, dtype=np.int64)
    out = rvol(volume, session, slot, lookback=20)
    assert np.isnan(out[0])
    assert out[20] == volume[20] / np.median(volume[:20])
    assert out[24] == volume[24] / np.median(volume[4:24])
    assert out[20] != volume[20] / np.median(volume[:21])
