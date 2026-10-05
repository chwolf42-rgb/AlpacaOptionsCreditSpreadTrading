"""Test A signal stack.

The hold and the re-confirmation entry are mandatory. ``k_confirm`` is how
many of the three optional conditions (oscillator, MACD, RVOL) must also
hold. Test B and the formation-only tests stay empty until D2-4 fills
``formations_at``.

Stops are derived from the clamped zone and the pullback. Whether a later
bar trades through that stop is the harness's job, and it uses the
unclamped high and low.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Iterator

import numpy as np
import pandas as pd

from research.intraday_sr.data.badprint import prices_as_of
from research.intraday_sr.engine.indicators import macd, rsi_wilder, rvol, stochastic
from research.intraday_sr.engine.levels import _atr_from_segments, _segments, levels_at, timeframe_frame
from research.intraday_sr.engine.tape import floor_15m, minute_of_day, session_day
from research.intraday_sr.engine.zones import fast_zones
from research.intraday_sr.types import ET, BarSet, EngineCfg, Signal, SignalCfg, Zone, as_et


def signals(
    bars: BarSet,
    start: datetime,
    end: datetime,
    cfg: EngineCfg,
    sig: SignalCfg,
) -> Iterator[Signal]:
    """Armed Test A entries whose ``available_at`` is inside ``[start, end]``."""
    if sig.test != "A":
        return iter(())
    visible = prices_as_of(bars.visible(end), end)
    if visible.empty or "symbol" not in visible.columns:
        return iter(())
    start_at = as_et(start, "start")
    end_at = as_et(end, "end")
    found: list[Signal] = []
    for symbol, group in visible.groupby("symbol", sort=True):
        group = group.sort_values("ts")
        if "tf" in group.columns:
            group = group.loc[group["tf"].astype(str) == "5m"]
        if group.empty:
            continue
        found.extend(_symbol_signals(str(symbol), group, start_at, end_at, cfg, sig))
    found.sort(key=lambda item: (item.available_at, item.symbol, -item.zone.score, item.zone.zone_id))
    return iter(found)


def _symbol_signals(
    symbol: str,
    group: pd.DataFrame,
    start: datetime,
    end: datetime,
    cfg: EngineCfg,
    sig: SignalCfg,
) -> list[Signal]:
    tape = group if sig.entry_tf == "5m" else timeframe_frame(group, sig.entry_tf)
    if tape is None or tape.empty:
        return []
    tape = tape.sort_values("ts").reset_index(drop=True)
    tape = tape.loc[tape["available_at"] <= end]
    if tape.empty:
        return []
    base = group.reset_index(drop=True)
    levels = levels_at(BarSet(base), end, cfg)
    own_levels = [level for level in levels if level.symbol == symbol]
    segments = _segments(base)
    atr_by_day: dict = {}
    tape_low = base["low"].to_numpy(dtype=np.float64)
    tape_high = base["high"].to_numpy(dtype=np.float64)
    tape_open = base["open"].to_numpy(dtype=np.float64)
    tape_close = base["close"].to_numpy(dtype=np.float64)
    tape_volume = base["volume"].to_numpy(dtype=np.float64)
    tape_avail = [_as_dt(value) for value in base["available_at"]]
    tape_session = [session_day(value) for value in base["session"]]
    day_index = {day: pos for pos, day in enumerate(sorted(set(tape_session)))}
    first_of_day: dict = {}
    for pos, day in enumerate(tape_session):
        first_of_day.setdefault(day, pos)
    ordered_days = sorted(first_of_day)
    day_pos = {day: pos for pos, day in enumerate(ordered_days)}
    session_ord = np.array([day_index[day] for day in tape_session], dtype=np.float64)
    closes = tape["close"].to_numpy(dtype=np.float64)
    highs = tape["high"].to_numpy(dtype=np.float64)
    lows = tape["low"].to_numpy(dtype=np.float64)
    opens = tape["open"].to_numpy(dtype=np.float64)
    volume = tape["volume"].to_numpy(dtype=np.float64)
    if "adj_factor" in tape.columns:
        factors = tape["adj_factor"].to_numpy(dtype=np.float64)
    else:
        factors = np.ones(len(tape), dtype=np.float64)
    available = [_as_dt(value) for value in tape["available_at"]]
    sessions = [session_day(value) for value in tape["session"]] if "session" in tape.columns else [available[i].date() for i in range(len(tape))]
    osc = _oscillator(sig.oscillator, highs, lows, closes)
    _line, _signal, hist = macd(closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    slot = minute_of_day(tape["ts"])
    session_codes = pd.factorize(np.array(sessions, dtype=object), sort=False)[0]
    volume_ratio = rvol(volume, session_codes, slot, int(cfg.rvol_sessions))
    oversold, overbought = _thresholds(sig.oscillator)
    width = timedelta(minutes=5 if sig.entry_tf == "5m" else 15)
    snapshots: dict[datetime, list[Zone]] = {}
    # Per zone: touch index, rc index, or None once consumed.
    armed_until: dict[str, int] = {}
    out: list[Signal] = []
    warmup = datetime.fromisoformat(str(cfg.warmup_date)).date()
    for index in range(len(tape)):
        stamp_at = available[index]
        if stamp_at < start or stamp_at > end:
            continue
        if sessions[index] < warmup:
            continue
        if stamp_at.astimezone(ET).time() > time(15, 0):
            continue
        recompute = floor_15m(stamp_at, sessions[index])
        if recompute is None:
            continue
        if recompute not in snapshots:
            current_day = sessions[index]
            if current_day not in atr_by_day:
                atr_by_day[current_day] = _atr_from_segments(segments, stamp_at, int(cfg.atr_length))
            atr = atr_by_day[current_day]
            if atr is None:
                snapshots[recompute] = []
            else:
                cutoff = 0
                while cutoff < len(tape_avail) and tape_avail[cutoff] <= recompute:
                    cutoff += 1
                if cutoff == 0:
                    snapshots[recompute] = []
                else:
                    origin = ordered_days[max(0, day_pos[current_day] - int(cfg.touch_sessions))]
                    begin = first_of_day[origin]
                    sl = slice(begin, cutoff)
                    ages = day_index[current_day] - session_ord[begin:cutoff]
                    live = [level for level in own_levels if level.available_at <= recompute]
                    snapshots[recompute] = fast_zones(
                        live,
                        symbol=symbol,
                        lows=tape_low[sl],
                        highs=tape_high[sl],
                        opens=tape_open[sl],
                        closes=tape_close[sl],
                        volume=tape_volume[sl],
                        ages=ages,
                        last_close=float(tape_close[cutoff - 1]),
                        atr=float(atr),
                        stamp=recompute,
                        cfg=cfg,
                    )
        zones = snapshots[recompute]
        if not zones:
            continue
        out.extend(
            _step(
                index=index,
                zones=zones,
                opens=opens,
                highs=highs,
                lows=lows,
                closes=closes,
                available=available,
                osc=osc,
                hist=hist,
                macd_line=_line,
                macd_signal=_signal,
                volume_ratio=volume_ratio,
                factors=factors,
                oversold=oversold,
                overbought=overbought,
                cfg=cfg,
                sig=sig,
                width=width,
                armed_until=armed_until,
            )
        )
    return out


def _step(
    *,
    index: int,
    zones: list[Zone],
    opens,
    highs,
    lows,
    closes,
    available,
    osc,
    hist,
    macd_line,
    macd_signal,
    volume_ratio,
    factors,
    oversold: float,
    overbought: float,
    cfg: EngineCfg,
    sig: SignalCfg,
    width: timedelta,
    armed_until: dict[str, int],
) -> list[Signal]:
    found: list[Signal] = []
    window = int(cfg.touch_window_bars)
    lookback = window + int(cfg.cancel_bars) + 2
    for zone in zones:
        if zone.zone_id in armed_until and index <= armed_until[zone.zone_id]:
            continue
        touch_at = None
        rc_at = None
        for touch in range(index - 1, max(-1, index - lookback), -1):
            if not _is_touch(touch, zone, lows, highs, closes):
                continue
            rc = _hold_index(touch, zone, opens, highs, lows, closes, window)
            if rc is None or rc >= index:
                continue
            arm = _arm_index(rc, zone, highs, lows, closes, cfg)
            if arm == index:
                touch_at = touch
                rc_at = rc
                break
        if touch_at is None or rc_at is None:
            continue
        flags = _optional_flags(
            touch_at,
            rc_at,
            index,
            zone,
            osc,
            hist,
            macd_line,
            macd_signal,
            volume_ratio,
            oversold,
            overbought,
            sig.rvol_min,
        )
        if sum(flags.values()) < int(sig.k_confirm):
            continue
        signal = _build(
            zone, rc_at, index, highs, lows, closes, available, factors, flags, zones, cfg, sig, width
        )
        if signal is None:
            continue
        armed_until[zone.zone_id] = index + int(cfg.cancel_bars)
        found.append(signal)
    return found


def _is_touch(index: int, zone: Zone, lows, highs, closes) -> bool:
    if zone.side == "support":
        return bool(lows[index] <= zone.high and closes[index] >= zone.low)
    return bool(highs[index] >= zone.low and closes[index] <= zone.high)


def _hold_index(touch: int, zone: Zone, opens, highs, lows, closes, window: int) -> int | None:
    stop = min(len(closes), touch + window)
    for cursor in range(touch, stop):
        span = max(highs[cursor] - lows[cursor], 1e-12)
        if zone.side == "support":
            close_out = closes[cursor] > zone.high
            wick = (min(opens[cursor], closes[cursor]) - lows[cursor]) >= 0.5 * span
            upper_half = closes[cursor] >= 0.5 * (highs[cursor] + lows[cursor])
            if close_out or (cursor == touch and wick and upper_half):
                return cursor
        else:
            close_out = closes[cursor] < zone.low
            wick = (highs[cursor] - max(opens[cursor], closes[cursor])) >= 0.5 * span
            lower_half = closes[cursor] <= 0.5 * (highs[cursor] + lows[cursor])
            if close_out or (cursor == touch and wick and lower_half):
                return cursor
    return _reclaim(touch, zone, closes, window)


def _reclaim(touch: int, zone: Zone, closes, window: int) -> int | None:
    stop = min(len(closes), touch + window)
    broke = False
    for cursor in range(touch, stop):
        if zone.side == "support" and closes[cursor] < zone.low:
            broke = True
        elif zone.side == "resistance" and closes[cursor] > zone.high:
            broke = True
        elif broke and zone.side == "support" and closes[cursor] > zone.high:
            return cursor
        elif broke and zone.side == "resistance" and closes[cursor] < zone.low:
            return cursor
    return None


def _arm_index(rc: int, zone: Zone, highs, lows, closes, cfg: EngineCfg) -> int | None:
    limit = min(len(closes), rc + 1 + int(cfg.cancel_bars))
    for cursor in range(rc + 1, limit):
        if zone.side == "support" and closes[cursor] < zone.low:
            return None
        if zone.side == "resistance" and closes[cursor] > zone.high:
            return None
        pullback = highs[cursor] < highs[rc] if zone.side == "support" else lows[cursor] > lows[rc]
        near = (
            lows[cursor] <= zone.high + float(cfg.arm_atr) * zone.atr_d
            if zone.side == "support"
            else highs[cursor] >= zone.low - float(cfg.arm_atr) * zone.atr_d
        )
        if pullback or near:
            return cursor
    return None


def _optional_flags(touch, rc, knowable, zone, osc, hist, macd_line, macd_signal, volume_ratio, oversold, overbought, rvol_min):
    direction_level = oversold if zone.side == "support" else overbought
    osc_flag = 0.0
    for cursor in range(max(0, touch - 2), touch + 1):
        value = osc[cursor]
        if not np.isfinite(value):
            continue
        if zone.side == "support" and value <= direction_level:
            osc_flag = 1.0
        if zone.side == "resistance" and value >= direction_level:
            osc_flag = 1.0
    macd_flag = 0.0
    for cursor in range(max(0, touch - 3), min(len(hist), touch + 4, knowable + 1)):
        if cursor < 3 or not np.isfinite(hist[cursor]):
            continue
        turn = hist[cursor] > hist[cursor - 1] < hist[cursor - 2] < hist[cursor - 3]
        if zone.side == "resistance":
            turn = hist[cursor] < hist[cursor - 1] > hist[cursor - 2] > hist[cursor - 3]
        cross = (
            np.isfinite(macd_line[cursor])
            and np.isfinite(macd_signal[cursor])
            and np.isfinite(macd_line[cursor - 1])
            and np.isfinite(macd_signal[cursor - 1])
            and (
                (macd_line[cursor - 1] <= macd_signal[cursor - 1] and macd_line[cursor] > macd_signal[cursor])
                if zone.side == "support"
                else (macd_line[cursor - 1] >= macd_signal[cursor - 1] and macd_line[cursor] < macd_signal[cursor])
            )
        )
        if turn or cross:
            macd_flag = 1.0
            break
    rvol_flag = 0.0
    for cursor in (touch, rc):
        if 0 <= cursor < len(volume_ratio) and np.isfinite(volume_ratio[cursor]) and volume_ratio[cursor] >= rvol_min:
            rvol_flag = 1.0
    return {"oscillator": osc_flag, "macd": macd_flag, "rvol": rvol_flag}


def _adjusted_offset(cfg: EngineCfg, factor: float) -> float:
    """$0.01 is an as-traded tick. Prices in the signal are adjusted."""
    if not np.isfinite(factor) or factor <= 0.0:
        factor = 1.0
    return float(cfg.entry_offset) / factor


def _build(zone, rc, arm, highs, lows, closes, available, factors, flags, zones, cfg, sig, width) -> Signal | None:
    direction = 1 if zone.side == "support" else -1
    offset = _adjusted_offset(cfg, float(factors[rc]))
    if direction == 1:
        trigger = float(highs[rc]) + offset
        pullback = float(np.min(lows[rc + 1 : arm + 1]))
        natural = min(zone.low, pullback) - float(cfg.stop_buffer_atr) * zone.atr_d
        floor = trigger - float(cfg.stop_floor_atr) * zone.atr_d
        stop = min(natural, floor)
        risk = trigger - stop
        if risk <= 0.0:
            return None
        ahead = [item for item in zones if item.side == "resistance" and item.low > trigger]
        if not ahead:
            return None
        target_zone = min(item.low for item in ahead)
        one_r = trigger + risk
        if target_zone - trigger < risk:
            return None
        targets = {"1R": one_r, "2R": trigger + 2.0 * risk, "zone": target_zone}
    else:
        trigger = float(lows[rc]) - offset
        pullback = float(np.max(highs[rc + 1 : arm + 1]))
        natural = max(zone.high, pullback) + float(cfg.stop_buffer_atr) * zone.atr_d
        floor = trigger + float(cfg.stop_floor_atr) * zone.atr_d
        stop = max(natural, floor)
        risk = stop - trigger
        if risk <= 0.0:
            return None
        ahead = [item for item in zones if item.side == "support" and item.high < trigger]
        if not ahead:
            return None
        target_zone = max(item.high for item in ahead)
        one_r = trigger - risk
        if trigger - target_zone < risk:
            return None
        targets = {"1R": one_r, "2R": trigger - 2.0 * risk, "zone": target_zone}
    count = int(sum(flags.values()))
    return Signal(
        symbol=zone.symbol,
        tf=sig.entry_tf,
        direction=direction,
        test="A",
        zone=zone,
        formation=None,
        trigger=trigger,
        stop=stop,
        targets=targets,
        expires_at=available[arm] + int(cfg.cancel_bars) * width,
        components=flags,
        as_of_ts=available[arm],
        available_at=available[arm],
        variant_id=sig.variant_id,
        confluence=count,
    )


def _oscillator(name: str, highs, lows, closes) -> np.ndarray:
    if name.startswith("stoch"):
        k_line, _d_line = stochastic(highs, lows, closes)
        return k_line
    return rsi_wilder(closes)


def _thresholds(name: str) -> tuple[float, float]:
    if name.startswith("stoch"):
        return 20.0, 80.0
    return 30.0, 70.0


def _as_dt(value) -> datetime:
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value
