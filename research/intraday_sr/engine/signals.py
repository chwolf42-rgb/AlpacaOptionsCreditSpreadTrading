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

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterator

import numpy as np
import pandas as pd

from research.intraday_sr.data.badprint import prices_as_of
from research.intraday_sr.engine.indicators import macd, rsi_wilder, rvol, stochastic
from research.intraday_sr.engine.levels import (
    _LevelTape,
    _atr_from_segments,
    _segments,
    timeframe_frame,
)
from research.intraday_sr.engine.tape import floor_15m, minute_of_day, session_day
from research.intraday_sr.engine.zone_cache import (
    cached_levels,
    cached_signals,
    cached_zones,
    level_cfg_token,
    prefix_digest,
    store_signals,
    tape_token,
    zone_cfg_token,
)
from research.intraday_sr.engine.zones import fast_zones
from research.intraday_sr.types import ET, BarSet, EngineCfg, Signal, SignalCfg, Zone, as_et


@dataclass
class SignalFunnel:
    """How many stack stages fired inside one ``signals`` call.

    ``touches``, ``holds``, and ``arms`` count bar/zone pairs on the entry
    timeframe. ``k_confirm_pass`` is how many of those arms cleared the
    optional-flag minimum. ``emits`` is the number of ``Signal`` objects
    returned. ``build_fail_no_ahead_zone`` and ``build_fail_zone_lt_1R``
    count skipped builds for ``target == "zone"`` only (SPEC v1.3.1 §5:
    skip when the zone target is missing or less than 1R away).
    """

    touches: int = 0
    holds: int = 0
    arms: int = 0
    k_confirm_pass: int = 0
    emits: int = 0
    build_fail_no_ahead_zone: int = 0
    build_fail_zone_lt_1R: int = 0


def signals_funnel(
    bars: BarSet,
    start: datetime,
    end: datetime,
    cfg: EngineCfg,
    sig: SignalCfg,
) -> SignalFunnel:
    """Run Test A and return the touch → hold → arm → emit counts."""
    funnel = SignalFunnel()
    list(signals(bars, start, end, cfg, sig, funnel=funnel))
    return funnel


def signals(
    bars: BarSet,
    start: datetime,
    end: datetime,
    cfg: EngineCfg,
    sig: SignalCfg,
    funnel: SignalFunnel | None = None,
) -> Iterator[Signal]:
    """Armed Test A entries whose ``available_at`` is inside ``[start, end]``."""
    if sig.test != "A":
        return iter(())
    visible = bars.visible(end)
    if visible.empty or "symbol" not in visible.columns:
        return iter(())
    # Clamped highs and lows feed levels and zones only. The stack's touch,
    # arm, trigger, and stop read the unclamped extremes. prices_as_of(end)
    # hides clamps that are not visible by the window end; each 15m stamp
    # hides any clamp whose visible_at is still after that stamp.
    clamped = prices_as_of(visible, end)
    raw = _entry_prices(visible)
    start_at = as_et(start, "start")
    end_at = as_et(end, "end")
    cache_key = None
    if funnel is None:
        cache_key = (
            tape_token(clamped),
            start_at,
            end_at,
            zone_cfg_token(cfg),
            sig.variant_id,
            sig.target,
            int(sig.k_confirm),
            sig.entry_tf,
            sig.oscillator,
            float(sig.rvol_min),
            sig.test,
        )
        hit = cached_signals(cache_key)
        if hit is not None:
            return iter(hit)
    found: list[Signal] = []
    raw_groups = {
        str(symbol): _five_minute(group)
        for symbol, group in raw.groupby("symbol", sort=True)
    }
    for symbol, group in clamped.groupby("symbol", sort=True):
        group = _five_minute(group)
        if group.empty:
            continue
        entry = raw_groups.get(str(symbol))
        if entry is None or entry.empty:
            continue
        found.extend(
            _symbol_signals(str(symbol), group, entry, start_at, end_at, cfg, sig, funnel)
        )
    found.sort(key=lambda item: (item.available_at, item.symbol, -item.zone.score, item.zone.zone_id))
    if funnel is not None:
        funnel.emits += len(found)
    elif cache_key is not None:
        store_signals(cache_key, found)
    return iter(found)


def _five_minute(group: pd.DataFrame) -> pd.DataFrame:
    group = group.sort_values("ts")
    if "tf" in group.columns:
        group = group.loc[group["tf"].astype(str) == "5m"]
    return group


def _entry_prices(frame: pd.DataFrame) -> pd.DataFrame:
    """High and low as printed. Levels and zones do not use this frame."""
    if frame.empty or "high_unclamped" not in frame.columns:
        return frame
    out = frame.copy()
    out["high"] = frame["high_unclamped"].to_numpy()
    out["low"] = frame["low_unclamped"].to_numpy()
    return out


def _symbol_signals(
    symbol: str,
    clamped: pd.DataFrame,
    raw: pd.DataFrame,
    start: datetime,
    end: datetime,
    cfg: EngineCfg,
    sig: SignalCfg,
    funnel: SignalFunnel | None,
) -> list[Signal]:
    tape = raw if sig.entry_tf == "5m" else timeframe_frame(raw, sig.entry_tf)
    if tape is None or tape.empty:
        return []
    tape = tape.sort_values("ts").reset_index(drop=True)
    tape = tape.loc[tape["available_at"] <= end]
    if tape.empty:
        return []
    base = clamped.reset_index(drop=True)
    # Pivots are confirmed on the clamped tape, and a pivot whose window
    # contains a not-yet-visible bad print is dated at that clamp's visible_at.
    # Each 15m stamp then keeps only clamps with visible_at <= stamp. Touch,
    # arm, trigger, and stop stay on the unclamped entry tape.
    plan = _LevelTape(symbol, base, cfg)
    segments = _segments(base)
    history = plan.hashes
    zones_key_cfg = zone_cfg_token(cfg)
    levels_key_cfg = level_cfg_token(cfg)
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
    cutoff_cursor = 0
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
            atr = plan.atr_by_day.get(current_day)
            if atr is None:
                atr = _atr_from_segments(segments, stamp_at, int(cfg.atr_length))
            if atr is None:
                snapshots[recompute] = []
            else:
                while cutoff_cursor < len(tape_avail) and tape_avail[cutoff_cursor] <= recompute:
                    cutoff_cursor += 1
                cutoff = cutoff_cursor
                if cutoff == 0:
                    snapshots[recompute] = []
                else:
                    origin = ordered_days[max(0, day_pos[current_day] - int(cfg.touch_sessions))]
                    begin = first_of_day[origin]
                    sl = slice(begin, cutoff)
                    ages = day_index[current_day] - session_ord[begin:cutoff]
                    digest = prefix_digest(history, cutoff - 1)
                    level_key = (digest, symbol, recompute, levels_key_cfg)
                    zone_key = (digest, symbol, sig.entry_tf, recompute, zones_key_cfg)

                    def build_zones(
                        symbol=symbol,
                        cutoff=cutoff,
                        recompute=recompute,
                        atr=atr,
                        sl=sl,
                        ages=ages,
                        level_key=level_key,
                        begin=begin,
                    ):
                        live = cached_levels(level_key, lambda: plan.levels_at(cutoff, recompute))
                        touch_low, touch_high = plan.asof_high_low(begin, cutoff, recompute)
                        return fast_zones(
                            live,
                            symbol=symbol,
                            lows=touch_low,
                            highs=touch_high,
                            opens=tape_open[sl],
                            closes=tape_close[sl],
                            volume=tape_volume[sl],
                            ages=ages,
                            last_close=float(tape_close[cutoff - 1]),
                            atr=float(atr),
                            stamp=recompute,
                            cfg=cfg,
                        )

                    snapshots[recompute] = cached_zones(zone_key, build_zones)
        zones = snapshots[recompute]
        if funnel is not None and zones:
            _tally(
                funnel,
                index,
                zones,
                opens,
                highs,
                lows,
                closes,
                osc,
                hist,
                _line,
                _signal,
                volume_ratio,
                oversold,
                overbought,
                cfg,
                sig,
            )
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
                funnel=funnel,
            )
        )
    return out


def _tally(
    funnel: SignalFunnel,
    index: int,
    zones: list[Zone],
    opens,
    highs,
    lows,
    closes,
    osc,
    hist,
    macd_line,
    macd_signal,
    volume_ratio,
    oversold: float,
    overbought: float,
    cfg: EngineCfg,
    sig: SignalCfg,
) -> None:
    window = int(cfg.touch_window_bars)
    for zone in zones:
        if not _is_touch(index, zone, lows, highs, closes):
            continue
        funnel.touches += 1
        rc = _hold_index(index, zone, opens, highs, lows, closes, window)
        if rc is None:
            continue
        funnel.holds += 1
        arm = _arm_index(rc, zone, highs, lows, closes, cfg)
        if arm is None:
            continue
        funnel.arms += 1
        flags = _optional_flags(
            index,
            rc,
            arm,
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
        if sum(flags.values()) >= int(sig.k_confirm):
            funnel.k_confirm_pass += 1


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
    funnel: SignalFunnel | None = None,
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
        signal, reason = _build(
            zone, rc_at, index, highs, lows, closes, available, factors, flags, zones, cfg, sig, width
        )
        if funnel is not None and reason == "no_ahead":
            funnel.build_fail_no_ahead_zone += 1
        elif funnel is not None and reason == "zone_lt_1r":
            funnel.build_fail_zone_lt_1R += 1
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


def _build(zone, rc, arm, highs, lows, closes, available, factors, flags, zones, cfg, sig, width):
    """Build one signal, or ``(None, reason)`` when the zone target has no room.

    SPEC v1.3.1 §5 skips a trade only when the chosen target is ``zone`` and
    that zone is missing or less than 1R from the trigger. ``1R`` and ``2R``
    are measured from the stop and always build when risk is positive.
    ``targets['zone']`` is stored when an opposite zone sits ahead, including
    when it is nearer than 1R on a fixed-R variant.
    """
    direction = 1 if zone.side == "support" else -1
    offset = _adjusted_offset(cfg, float(factors[rc]))
    if direction == 1:
        trigger = float(highs[rc]) + offset
        pullback = float(np.min(lows[rc + 1 : arm + 1]))
        natural = min(zone.low, pullback) - float(cfg.stop_buffer_atr) * zone.atr_d
        floor = trigger - float(cfg.stop_floor_atr) * zone.atr_d
        stop = min(natural, floor)
        risk = trigger - stop
        ahead = [item.low for item in zones if item.side == "resistance" and item.low > trigger]
        target_zone = min(ahead) if ahead else None
        one_r = trigger + risk
        two_r = trigger + 2.0 * risk
        room = None if target_zone is None else target_zone - trigger
    else:
        trigger = float(lows[rc]) - offset
        pullback = float(np.max(highs[rc + 1 : arm + 1]))
        natural = max(zone.high, pullback) + float(cfg.stop_buffer_atr) * zone.atr_d
        floor = trigger + float(cfg.stop_floor_atr) * zone.atr_d
        stop = max(natural, floor)
        risk = stop - trigger
        ahead = [item.high for item in zones if item.side == "support" and item.high < trigger]
        target_zone = max(ahead) if ahead else None
        one_r = trigger - risk
        two_r = trigger - 2.0 * risk
        room = None if target_zone is None else trigger - target_zone
    if risk <= 0.0:
        return None, None
    if sig.target == "zone":
        if target_zone is None:
            return None, "no_ahead"
        if room < risk:
            return None, "zone_lt_1r"
    targets = {"1R": one_r, "2R": two_r}
    if target_zone is not None:
        targets["zone"] = float(target_zone)
    count = int(sum(flags.values()))
    signal = Signal(
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
    return signal, None


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
