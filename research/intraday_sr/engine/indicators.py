"""RSI, Stochastic, MACD, and RVOL.

The pure-Python functions are the reference. When numba is importable the
same recurrences run under ``njit`` and the engine uses those arrays.
``tests/test_indicators.py`` checks the two agree to 1e-5.
"""

from __future__ import annotations

import numpy as np

try:
    from numba import njit
except ImportError:  # pragma: no cover - the sandbox has numba
    njit = None


def rsi_wilder(close: np.ndarray, length: int = 14) -> np.ndarray:
    """Wilder RSI. Index ``length`` is the first finite value."""
    values = np.asarray(close, dtype=np.float64)
    if _NUMBA_RSI is not None:
        return _NUMBA_RSI(values, int(length))
    return _rsi_python(values, int(length))


def stochastic(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    k_length: int = 14,
    smooth: int = 3,
    d_length: int = 3,
) -> tuple[np.ndarray, np.ndarray]:
    """Smoothed %K and %D. A flat window is 50, not a divide-by-zero."""
    highs = np.asarray(high, dtype=np.float64)
    lows = np.asarray(low, dtype=np.float64)
    closes = np.asarray(close, dtype=np.float64)
    if _NUMBA_STOCH is not None:
        return _NUMBA_STOCH(highs, lows, closes, int(k_length), int(smooth), int(d_length))
    return _stoch_python(highs, lows, closes, int(k_length), int(smooth), int(d_length))


def macd(
    close: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """MACD line, signal line, histogram. EMAs are seeded with an SMA."""
    values = np.asarray(close, dtype=np.float64)
    if _NUMBA_MACD is not None:
        return _NUMBA_MACD(values, int(fast), int(slow), int(signal))
    return _macd_python(values, int(fast), int(slow), int(signal))


def rvol(volume: np.ndarray, session: np.ndarray, slot: np.ndarray, lookback: int = 20) -> np.ndarray:
    """Volume divided by the median of the same slot over the prior sessions.

    ``session`` and ``slot`` are integer codes. The current session is not
    in its own median. One row per session per slot is the expected shape.
    """
    vols = np.asarray(volume, dtype=np.float64)
    sessions = np.asarray(session)
    slots = np.asarray(slot)
    out = np.full(len(vols), np.nan, dtype=np.float64)
    if len(vols) == 0:
        return out
    for slot_value in np.unique(slots):
        index = np.flatnonzero(slots == slot_value)
        prior = vols[index]
        for position, row in enumerate(index):
            window = prior[max(0, position - lookback) : position]
            if window.size == 0:
                continue
            median = float(np.median(window))
            if median > 0.0 and np.isfinite(median):
                out[row] = prior[position] / median
    return out


def _rsi_python(close: np.ndarray, length: int) -> np.ndarray:
    out = np.full(close.shape[0], np.nan, dtype=np.float64)
    if close.shape[0] <= length or length < 1:
        return out
    delta = np.diff(close)
    gain = np.maximum(delta, 0.0)
    loss = np.maximum(-delta, 0.0)
    avg_gain = float(gain[:length].mean())
    avg_loss = float(loss[:length].mean())
    out[length] = _rsi_value(avg_gain, avg_loss)
    for index in range(length, gain.shape[0]):
        avg_gain = (avg_gain * (length - 1) + gain[index]) / length
        avg_loss = (avg_loss * (length - 1) + loss[index]) / length
        out[index + 1] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _sma_finite(values: np.ndarray, length: int) -> np.ndarray:
    out = np.full(values.shape[0], np.nan, dtype=np.float64)
    if length < 1:
        return out
    for index in range(length - 1, values.shape[0]):
        window = values[index - length + 1 : index + 1]
        if np.isfinite(window).all():
            out[index] = float(window.mean())
    return out


def _stoch_python(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    k_length: int,
    smooth: int,
    d_length: int,
) -> tuple[np.ndarray, np.ndarray]:
    raw = np.full(close.shape[0], np.nan, dtype=np.float64)
    for index in range(k_length - 1, close.shape[0]):
        highest = float(high[index - k_length + 1 : index + 1].max())
        lowest = float(low[index - k_length + 1 : index + 1].min())
        if highest == lowest:
            raw[index] = 50.0
        else:
            raw[index] = (close[index] - lowest) / (highest - lowest) * 100.0
    k_line = _sma_finite(raw, smooth)
    d_line = _sma_finite(k_line, d_length)
    return k_line, d_line


def _ema(values: np.ndarray, length: int) -> np.ndarray:
    out = np.full(values.shape[0], np.nan, dtype=np.float64)
    if values.shape[0] < length or length < 1:
        return out
    alpha = 2.0 / (length + 1.0)
    seed = float(values[:length].mean())
    out[length - 1] = seed
    previous = seed
    for index in range(length, values.shape[0]):
        previous = alpha * values[index] + (1.0 - alpha) * previous
        out[index] = previous
    return out


def _macd_python(
    close: np.ndarray, fast: int, slow: int, signal: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    fast_ema = _ema(close, fast)
    slow_ema = _ema(close, slow)
    line = fast_ema - slow_ema
    # Signal EMA is seeded on the first `signal` finite MACD values, which
    # start at index slow-1. Walk only the finite tail.
    signal_line = np.full(close.shape[0], np.nan, dtype=np.float64)
    finite = np.flatnonzero(np.isfinite(line))
    if finite.size >= signal:
        seed_at = finite[:signal]
        seed = float(line[seed_at].mean())
        start = int(seed_at[-1])
        signal_line[start] = seed
        alpha = 2.0 / (signal + 1.0)
        previous = seed
        for index in range(start + 1, close.shape[0]):
            if not np.isfinite(line[index]):
                continue
            previous = alpha * line[index] + (1.0 - alpha) * previous
            signal_line[index] = previous
    hist = line - signal_line
    return line, signal_line, hist


def _compile_numba():
    if njit is None:
        return None, None, None

    @njit(cache=True)
    def rsi_numba(close, length):
        out = np.empty(close.shape[0], dtype=np.float64)
        for index in range(close.shape[0]):
            out[index] = np.nan
        if close.shape[0] <= length or length < 1:
            return out
        gain_sum = 0.0
        loss_sum = 0.0
        for index in range(1, length + 1):
            delta = close[index] - close[index - 1]
            if delta >= 0.0:
                gain_sum += delta
            else:
                loss_sum -= delta
        avg_gain = gain_sum / length
        avg_loss = loss_sum / length
        if avg_loss == 0.0:
            out[length] = 100.0 if avg_gain > 0.0 else 50.0
        else:
            rs = avg_gain / avg_loss
            out[length] = 100.0 - 100.0 / (1.0 + rs)
        for index in range(length + 1, close.shape[0]):
            delta = close[index] - close[index - 1]
            gain = delta if delta > 0.0 else 0.0
            loss = -delta if delta < 0.0 else 0.0
            avg_gain = (avg_gain * (length - 1) + gain) / length
            avg_loss = (avg_loss * (length - 1) + loss) / length
            if avg_loss == 0.0:
                out[index] = 100.0 if avg_gain > 0.0 else 50.0
            else:
                rs = avg_gain / avg_loss
                out[index] = 100.0 - 100.0 / (1.0 + rs)
        return out

    @njit(cache=True)
    def stoch_numba(high, low, close, k_length, smooth, d_length):
        raw = np.empty(close.shape[0], dtype=np.float64)
        k_line = np.empty(close.shape[0], dtype=np.float64)
        d_line = np.empty(close.shape[0], dtype=np.float64)
        for index in range(close.shape[0]):
            raw[index] = np.nan
            k_line[index] = np.nan
            d_line[index] = np.nan
        for index in range(k_length - 1, close.shape[0]):
            highest = high[index]
            lowest = low[index]
            for cursor in range(index - k_length + 1, index + 1):
                if high[cursor] > highest:
                    highest = high[cursor]
                if low[cursor] < lowest:
                    lowest = low[cursor]
            if highest == lowest:
                raw[index] = 50.0
            else:
                raw[index] = (close[index] - lowest) / (highest - lowest) * 100.0
        for index in range(close.shape[0]):
            if index < smooth - 1:
                continue
            total = 0.0
            ok = True
            for cursor in range(index - smooth + 1, index + 1):
                if not np.isfinite(raw[cursor]):
                    ok = False
                    break
                total += raw[cursor]
            if ok:
                k_line[index] = total / smooth
        for index in range(close.shape[0]):
            if index < d_length - 1:
                continue
            total = 0.0
            ok = True
            for cursor in range(index - d_length + 1, index + 1):
                if not np.isfinite(k_line[cursor]):
                    ok = False
                    break
                total += k_line[cursor]
            if ok:
                d_line[index] = total / d_length
        return k_line, d_line

    @njit(cache=True)
    def macd_numba(close, fast, slow, signal):
        n = close.shape[0]
        fast_ema = np.empty(n, dtype=np.float64)
        slow_ema = np.empty(n, dtype=np.float64)
        line = np.empty(n, dtype=np.float64)
        signal_line = np.empty(n, dtype=np.float64)
        hist = np.empty(n, dtype=np.float64)
        for index in range(n):
            fast_ema[index] = np.nan
            slow_ema[index] = np.nan
            line[index] = np.nan
            signal_line[index] = np.nan
            hist[index] = np.nan
        if n >= fast and fast >= 1:
            seed = 0.0
            for index in range(fast):
                seed += close[index]
            seed /= fast
            fast_ema[fast - 1] = seed
            alpha = 2.0 / (fast + 1.0)
            previous = seed
            for index in range(fast, n):
                previous = alpha * close[index] + (1.0 - alpha) * previous
                fast_ema[index] = previous
        if n >= slow and slow >= 1:
            seed = 0.0
            for index in range(slow):
                seed += close[index]
            seed /= slow
            slow_ema[slow - 1] = seed
            alpha = 2.0 / (slow + 1.0)
            previous = seed
            for index in range(slow, n):
                previous = alpha * close[index] + (1.0 - alpha) * previous
                slow_ema[index] = previous
        for index in range(n):
            if np.isfinite(fast_ema[index]) and np.isfinite(slow_ema[index]):
                line[index] = fast_ema[index] - slow_ema[index]
        seen = 0
        seed = 0.0
        start = -1
        for index in range(n):
            if not np.isfinite(line[index]):
                continue
            if seen < signal:
                seed += line[index]
                seen += 1
                if seen == signal:
                    seed /= signal
                    signal_line[index] = seed
                    start = index
                    break
        if start >= 0:
            alpha = 2.0 / (signal + 1.0)
            previous = signal_line[start]
            for index in range(start + 1, n):
                if not np.isfinite(line[index]):
                    continue
                previous = alpha * line[index] + (1.0 - alpha) * previous
                signal_line[index] = previous
        for index in range(n):
            if np.isfinite(line[index]) and np.isfinite(signal_line[index]):
                hist[index] = line[index] - signal_line[index]
        return line, signal_line, hist

    return rsi_numba, stoch_numba, macd_numba


_NUMBA_RSI, _NUMBA_STOCH, _NUMBA_MACD = _compile_numba()


def using_numba() -> bool:
    """True when the njit ports are the implementation the engine calls."""
    return _NUMBA_RSI is not None
