"""Stochastic RSI and MACD as entry-timing confluence.

Stochastic RSI is (14, 14, 3, 3): Wilder RSI(14), a 14-period stochastic of
that RSI, a 3-period SMA of the raw stochastic (%K), and a 3-period SMA of
%K (%D). Readings are 0–100.

MACD is the standard 12 / 26 / 9. The line is EMA(12) − EMA(26). The signal
is a 9-period EMA of the line, started on the first bar where the line
exists. The histogram is line minus signal.

A bull-put turn is %K rising while the prior %K is still below 20. A
bear-call turn is %K falling while the prior %K is still above 80. The
histogram has to be moving the same way. Daily trend context is the sign of
the daily MACD line: positive for a bull put, negative for a bear call.
A missing or exactly-zero reading fails closed.

Every value at index i uses closes at or before i. Building the series on
the whole tape does not leak, because the EMA and the Wilder average are
causal. The caller passes the closed timing bar and the last daily session
whose close is already known.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from alpaca_options_credit.models import Side
from alpaca_options_credit.replay.regime import ema_series

RSI_PERIOD = 14
STOCH_PERIOD = 14
K_SMOOTH = 3
D_SMOOTH = 3
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
OVERSOLD = 20.0
OVERBOUGHT = 80.0


@dataclass(frozen=True)
class OscGate:
    """One candidate the training window is allowed to choose.

    ``frame`` is where the Stochastic RSI turn and the MACD histogram are
    read: the closed hourly bar, or the last completed daily session.
    ``hist_bars`` is how many consecutive histogram steps must agree (1 or 2).
    ``daily_sign`` requires the daily MACD line to agree with the spread.
    ``kd_cross`` also requires %K to cross %D while leaving the extreme.
    """

    frame: str
    hist_bars: int = 1
    daily_sign: bool = False
    kd_cross: bool = False


@dataclass(frozen=True)
class OscFlags:
    """Side-specific readings at one decision. False means the signal is absent."""

    h_turn: bool = False
    h_cross: bool = False
    h_hist1: bool = False
    h_hist2: bool = False
    d_turn: bool = False
    d_cross: bool = False
    d_hist1: bool = False
    d_hist2: bool = False
    daily_sign: bool = False


@dataclass(frozen=True)
class OscSeries:
    k: list[Optional[float]]
    d: list[Optional[float]]
    macd: list[Optional[float]]
    hist: list[Optional[float]]

    @classmethod
    def from_closes(cls, closes: Sequence[float]) -> "OscSeries":
        k, d = stoch_rsi_kd(closes)
        macd, _signal, hist = macd_series(closes)
        return cls(k=k, d=d, macd=macd, hist=hist)


def gate_allows(gate: OscGate, flags: OscFlags) -> bool:
    """True when this setting's confluence is present. Missing data is a skip."""
    if gate.frame == "1h":
        turn = flags.h_cross if gate.kd_cross else flags.h_turn
        hist = flags.h_hist2 if gate.hist_bars >= 2 else flags.h_hist1
    elif gate.frame == "daily":
        turn = flags.d_cross if gate.kd_cross else flags.d_turn
        hist = flags.d_hist2 if gate.hist_bars >= 2 else flags.d_hist1
    else:
        return False
    if not turn or not hist:
        return False
    if gate.daily_sign and not flags.daily_sign:
        return False
    return True


def flags_at(
    hourly: OscSeries,
    daily: OscSeries,
    hour_index: int,
    daily_index: int,
    side: Side,
) -> OscFlags:
    """Read both series at the closed bars the entry is allowed to see."""
    return OscFlags(
        h_turn=_turn(hourly.k, hour_index, side),
        h_cross=_cross(hourly.k, hourly.d, hour_index, side),
        h_hist1=_hist_dir(hourly.hist, hour_index, 1, side),
        h_hist2=_hist_dir(hourly.hist, hour_index, 2, side),
        d_turn=_turn(daily.k, daily_index, side),
        d_cross=_cross(daily.k, daily.d, daily_index, side),
        d_hist1=_hist_dir(daily.hist, daily_index, 1, side),
        d_hist2=_hist_dir(daily.hist, daily_index, 2, side),
        daily_sign=_macd_sign(daily.macd, daily_index, side),
    )


def rsi_series(closes: Sequence[float], period: int = RSI_PERIOD) -> list[Optional[float]]:
    """Wilder RSI. The first value sits on the bar that completes ``period`` changes."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if period < 1 or n < period + 1:
        return out
    gain_sum = 0.0
    loss_sum = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gain_sum += max(change, 0.0)
        loss_sum += max(-change, 0.0)
    avg_gain = gain_sum / period
    avg_loss = loss_sum / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, n):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def stoch_rsi_kd(
    closes: Sequence[float],
    rsi_period: int = RSI_PERIOD,
    stoch_period: int = STOCH_PERIOD,
    k_smooth: int = K_SMOOTH,
    d_smooth: int = D_SMOOTH,
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    rsi = rsi_series(closes, rsi_period)
    raw: list[Optional[float]] = [None] * len(closes)
    for i in range(len(closes)):
        start = i - stoch_period + 1
        if start < 0:
            continue
        window = rsi[start : i + 1]
        if len(window) < stoch_period or any(value is None for value in window):
            continue
        lo = min(value for value in window if value is not None)
        hi = max(value for value in window if value is not None)
        last = window[-1]
        if last is None or hi == lo:
            # A flat RSI is not an extreme. 50 cannot pass the 20/80 gates.
            raw[i] = 50.0
        else:
            raw[i] = 100.0 * (last - lo) / (hi - lo)
    k = _sma(raw, k_smooth)
    d = _sma(k, d_smooth)
    return k, d


def macd_series(
    closes: Sequence[float],
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    fast_ema = ema_series(closes, fast)
    slow_ema = ema_series(closes, slow)
    line: list[Optional[float]] = [None] * len(closes)
    for i, (fast_value, slow_value) in enumerate(zip(fast_ema, slow_ema)):
        if fast_value is None or slow_value is None:
            continue
        line[i] = fast_value - slow_value
    valid = [i for i, value in enumerate(line) if value is not None]
    compact = [line[i] for i in valid]
    signal_compact = ema_series(compact, signal)
    signal_line: list[Optional[float]] = [None] * len(closes)
    hist: list[Optional[float]] = [None] * len(closes)
    for j, i in enumerate(valid):
        signal_value = signal_compact[j]
        signal_line[i] = signal_value
        if signal_value is not None and line[i] is not None:
            hist[i] = line[i] - signal_value
    return line, signal_line, hist


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0 and avg_gain == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _sma(values: Sequence[Optional[float]], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if period < 1:
        return out
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        if any(value is None for value in window):
            continue
        out[i] = sum(value for value in window if value is not None) / period
    return out


def _turn(k: Sequence[Optional[float]], index: int, side: Side) -> bool:
    if index < 1 or index >= len(k):
        return False
    prev = k[index - 1]
    cur = k[index]
    if prev is None or cur is None:
        return False
    if side is Side.BULLISH:
        return prev < OVERSOLD and cur > prev
    if side is Side.BEARISH:
        return prev > OVERBOUGHT and cur < prev
    return False


def _cross(
    k: Sequence[Optional[float]],
    d: Sequence[Optional[float]],
    index: int,
    side: Side,
) -> bool:
    """Level turn plus %K crossing %D. The cross is the stricter train setting."""
    if not _turn(k, index, side) or index >= len(d):
        return False
    k_prev, k_cur = k[index - 1], k[index]
    d_prev, d_cur = d[index - 1], d[index]
    if k_prev is None or k_cur is None or d_prev is None or d_cur is None:
        return False
    if side is Side.BULLISH:
        return k_prev <= d_prev and k_cur > d_cur
    if side is Side.BEARISH:
        return k_prev >= d_prev and k_cur < d_cur
    return False


def _hist_dir(hist: Sequence[Optional[float]], index: int, steps: int, side: Side) -> bool:
    if steps < 1 or index < steps or index >= len(hist):
        return False
    for step in range(steps):
        later = hist[index - step]
        earlier = hist[index - step - 1]
        if later is None or earlier is None:
            return False
        if side is Side.BULLISH and not later > earlier:
            return False
        if side is Side.BEARISH and not later < earlier:
            return False
    return side in (Side.BULLISH, Side.BEARISH)


def _macd_sign(macd: Sequence[Optional[float]], index: int, side: Side) -> bool:
    if index < 0 or index >= len(macd) or macd[index] is None:
        return False
    value = macd[index]
    if value is None:
        return False
    if side is Side.BULLISH:
        return value > 0.0
    if side is Side.BEARISH:
        return value < 0.0
    return False
