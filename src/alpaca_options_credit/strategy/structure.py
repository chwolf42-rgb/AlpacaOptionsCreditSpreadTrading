"""Daily confirm → arm → 1H first pullback + 1H reconfirm.

Locked hybrid: daily bars own trend bias (strict HH/HL or LL/LH), VP shelf,
and S/R / invalidation. 1Hour bars only time entry into that daily zone.
1H dips do not cancel an arm; only a daily close through invalidation does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from alpaca_options_credit.models import Bar, Side
from alpaca_options_credit.strategy.volume_profile import hvn_shelves, nearest_shelf

# Journal / log reasons for the hybrid path (locked vocabulary).
DAILY_NOT_CONFIRMED = "daily_not_confirmed"
WAITING_1H_PULLBACK = "waiting_1h_pullback"
RECONFIRM_FAILED = "1h_reconfirm_failed"
CHASE = "chase"
DAILY_STRUCTURE_BREAK = "daily_structure_break"
HYBRID_READY = "first_pullback_1h_reconfirmed"


@dataclass
class Swing:
    index: int
    price: float


@dataclass
class StructureView:
    side: Optional[Side]
    confirmed: bool
    invalidation: Optional[float]
    zone_low: Optional[float]
    zone_high: Optional[float]
    confirm_index: int
    reason: str
    last_close: float
    atr: float
    confirm_ts: Optional[datetime] = None


def atr(bars: list[Bar], period: int = 14) -> float:
    if not bars:
        return 0.0
    trs: list[float] = []
    for i, bar in enumerate(bars):
        if i == 0:
            trs.append(bar.high - bar.low)
            continue
        prev = bars[i - 1].close
        trs.append(max(bar.high - bar.low, abs(bar.high - prev), abs(bar.low - prev)))
    window = trs[-period:]
    return sum(window) / len(window)


def swing_points(bars: list[Bar], left: int = 2, right: int = 2) -> tuple[list[Swing], list[Swing]]:
    highs: list[Swing] = []
    lows: list[Swing] = []
    n = len(bars)
    if n < left + right + 1:
        return highs, lows
    for i in range(left, n - right):
        window = bars[i - left : i + right + 1]
        if bars[i].high >= max(b.high for b in window) and bars[i].high == max(
            bars[j].high for j in range(i - left, i + right + 1)
        ):
            # unique-ish local high
            if all(j == i or bars[j].high < bars[i].high for j in range(i - left, i + right + 1)):
                highs.append(Swing(i, bars[i].high))
        if all(j == i or bars[j].low > bars[i].low for j in range(i - left, i + right + 1)):
            lows.append(Swing(i, bars[i].low))
    return highs, lows


def confirm_and_zone(
    bars: list[Bar],
    *,
    left: int = 2,
    right: int = 2,
    atr_period: int = 14,
    vp_lookback: int = 25,
    vp_bin: float = 0.5,
    vp_percentile: float = 0.70,
) -> StructureView:
    """Strict confirm on the *structure* series (daily): HH+HL or LL+LH.

    Last close through the break level. VP shelf + S/R become the pullback zone.
    """
    empty = StructureView(
        side=None,
        confirmed=False,
        invalidation=None,
        zone_low=None,
        zone_high=None,
        confirm_index=max(0, len(bars) - 1),
        reason="insufficient_bars",
        last_close=bars[-1].close if bars else 0.0,
        atr=atr(bars, atr_period),
        confirm_ts=bars[-1].ts if bars else None,
    )
    if len(bars) < left + right + 8:
        return empty

    highs, lows = swing_points(bars, left, right)
    last = bars[-1]
    vol = atr(bars, atr_period)
    look = bars[-min(len(bars), vp_lookback) :]
    shelves = hvn_shelves(look, bin_size=vp_bin, percentile=vp_percentile)

    bull = _bull_confirm(bars, highs, lows, last)
    bear = _bear_confirm(bars, highs, lows, last)

    # Strict: one side only. If both fire, prefer the one matching last impulse.
    chosen: Optional[StructureView] = None
    if bull and bear:
        chosen = bull if last.close >= bars[-3].close else bear
        chosen = StructureView(**{**chosen.__dict__, "reason": chosen.reason + "|conflict_resolved"})
    elif bull:
        chosen = bull
    elif bear:
        chosen = bear
    else:
        return StructureView(
            side=None,
            confirmed=False,
            invalidation=None,
            zone_low=None,
            zone_high=None,
            confirm_index=len(bars) - 1,
            reason="no_strict_confirm",
            last_close=last.close,
            atr=vol,
            confirm_ts=last.ts,
        )

    # Pullback zone: VP shelf nearest the broken S/R, unioned with that S/R
    # if they sit within ~1 ATR. Intersection is too tight (degenerate HVN bins).
    sr_lo, sr_hi = chosen.zone_low, chosen.zone_high
    sr_mid = ((sr_lo or last.close) + (sr_hi or last.close)) / 2.0
    shelf = nearest_shelf(shelves, sr_mid)
    if sr_lo is None or sr_hi is None:
        pad = max(vol, 0.01)
        zone_low, zone_high = last.close - pad, last.close + pad
    else:
        zone_low, zone_high = sr_lo, sr_hi
        if shelf:
            gap = max(0.0, shelf[0] - sr_hi, sr_lo - shelf[1])
            if gap <= max(vol, 0.5):
                zone_low = min(sr_lo, shelf[0])
                zone_high = max(sr_hi, shelf[1])

    confirm_ts = (
        bars[chosen.confirm_index].ts
        if 0 <= chosen.confirm_index < len(bars)
        else last.ts
    )
    return StructureView(
        side=chosen.side,
        confirmed=True,
        invalidation=chosen.invalidation,
        zone_low=zone_low,
        zone_high=zone_high,
        confirm_index=chosen.confirm_index,
        reason=chosen.reason,
        last_close=last.close,
        atr=vol,
        confirm_ts=confirm_ts,
    )


def _bull_confirm(
    bars: list[Bar], highs: list[Swing], lows: list[Swing], last: Bar
) -> Optional[StructureView]:
    if len(highs) < 2 or len(lows) < 2:
        return None
    h1, h2 = highs[-2], highs[-1]
    l1, l2 = lows[-2], lows[-1]
    hh = h2.price > h1.price
    hl = l2.price > l1.price
    if not (hh and hl):
        return None
    # Confirm: at least one close above the broken swing high (h1) after the HL.
    # The last bar may already be pulling back into that high (now support).
    confirm_from = max(h2.index, l2.index)
    if not any(bars[i].close >= h1.price for i in range(confirm_from, len(bars))):
        return None
    if last.close < l2.price:
        return None
    invalidation = l2.price
    # S/R: broken swing high is now support — a band around h1, not down to the stop.
    pad = max(0.75, (h2.price - h1.price) * 0.15)
    zone_low, zone_high = h1.price - pad, h1.price + pad
    return StructureView(
        side=Side.BULLISH,
        confirmed=True,
        invalidation=invalidation,
        zone_low=min(zone_low, zone_high),
        zone_high=max(zone_low, zone_high),
        confirm_index=max(h2.index, l2.index),
        reason="hh_hl_close_above_broken_high",
        last_close=last.close,
        atr=0.0,
        confirm_ts=bars[max(h2.index, l2.index)].ts,
    )


def _bear_confirm(
    bars: list[Bar], highs: list[Swing], lows: list[Swing], last: Bar
) -> Optional[StructureView]:
    if len(highs) < 2 or len(lows) < 2:
        return None
    h1, h2 = highs[-2], highs[-1]
    l1, l2 = lows[-2], lows[-1]
    ll = l2.price < l1.price
    lh = h2.price < h1.price
    if not (ll and lh):
        return None
    confirm_from = max(h2.index, l2.index)
    if not any(bars[i].close <= l1.price for i in range(confirm_from, len(bars))):
        return None
    if last.close > h2.price:
        return None
    invalidation = h2.price
    pad = max(0.75, (h1.price - h2.price) * 0.15)
    zone_low, zone_high = l1.price - pad, l1.price + pad
    return StructureView(
        side=Side.BEARISH,
        confirmed=True,
        invalidation=invalidation,
        zone_low=min(zone_low, zone_high),
        zone_high=max(zone_low, zone_high),
        confirm_index=max(h2.index, l2.index),
        reason="ll_lh_close_below_broken_low",
        last_close=last.close,
        atr=0.0,
        confirm_ts=bars[max(h2.index, l2.index)].ts,
    )


def structure_broken(view: StructureView, last: Bar) -> bool:
    """Close through invalidation. Wicks/dips that hold the close do NOT cancel."""
    if view.invalidation is None or view.side is None:
        return False
    return daily_close_through_invalidation(view.side, view.invalidation, last.close)


def daily_close_through_invalidation(side: Side, invalidation: float, last_close: float) -> bool:
    """True when a daily close is already through structure invalidation.

    Bullish (bull put): close < invalidation. Bearish (bear call): close > invalidation.
    A close exactly on the level still holds. This is the same comparison as an
    arm-cancel / structure-break close; entry uses it on the last *completed*
    daily bar so an aged arm cannot open underwater.
    """
    if side is Side.BULLISH:
        return last_close < invalidation
    if side is Side.BEARISH:
        return last_close > invalidation
    return True


def first_pullback(
    bars: list[Bar],
    view: StructureView,
    *,
    no_chase_atr: float = 0.5,
) -> tuple[bool, str]:
    """True when the first retrace tags the daily VP/S-R shelf while in-zone.

    `bars` are the *timing* series (1H). The zone/invalidation come from daily.
    No chase: last close still inside/near the zone, not extended in trend.

    Does not treat a 1H close through invalidation as a cancel — that is a
    daily-only decision (see hybrid_entry / structure_broken on daily bars).
    """
    if not view.confirmed or view.side is None or view.zone_low is None or view.zone_high is None:
        return False, "not_confirmed"

    last = bars[-1] if bars else None
    if last is None:
        return False, "no_pullback_yet"

    after = _bars_after_confirm(bars, view)
    tagged = any(_overlaps_zone(bar, view.zone_low, view.zone_high) for bar in after)
    if not tagged:
        return False, "no_pullback_yet"

    if not _overlaps_zone(last, view.zone_low, view.zone_high):
        # Extended away from the shelf — do not chase.
        pad = no_chase_atr * (view.atr or 0.0)
        if view.side is Side.BULLISH and last.close > view.zone_high + pad:
            return False, "no_chase_extended"
        if view.side is Side.BEARISH and last.close < view.zone_low - pad:
            return False, "no_chase_extended"
        return False, "pullback_left_zone"

    # First tag is enough; subsequent tags still allow entry while in zone.
    return True, "first_pullback_into_shelf"


def timing_reconfirm(
    timing_bars: list[Bar],
    side: Side,
    *,
    left: int = 2,
    right: int = 2,
) -> tuple[bool, str]:
    """1H must reprint the daily side (HH+HL or LL+LH) and turn with it.

    Last-close-through-HL/LH is *not* required — that would reject a pullback
    into the daily zone. A still-dumping (or still-ripping) last 1H print vs
    the prior close fails so we do not chase the first tag.
    """
    if len(timing_bars) < left + right + 8:
        return False, RECONFIRM_FAILED
    highs, lows = swing_points(timing_bars, left, right)
    if len(highs) < 2 or len(lows) < 2:
        return False, RECONFIRM_FAILED
    h1, h2 = highs[-2], highs[-1]
    l1, l2 = lows[-2], lows[-1]
    last = timing_bars[-1]
    prev = timing_bars[-2]
    if side is Side.BULLISH:
        if not (h2.price > h1.price and l2.price > l1.price):
            return False, RECONFIRM_FAILED
        if last.close < prev.close:
            return False, RECONFIRM_FAILED
        return True, "1h_reconfirmed"
    if not (l2.price < l1.price and h2.price < h1.price):
        return False, RECONFIRM_FAILED
    if last.close > prev.close:
        return False, RECONFIRM_FAILED
    return True, "1h_reconfirmed"


def hybrid_entry(
    structure_bars: list[Bar],
    timing_bars: list[Bar],
    *,
    left: int = 2,
    right: int = 2,
    atr_period: int = 14,
    vp_lookback: int = 25,
    vp_bin: float = 0.5,
    vp_percentile: float = 0.70,
    no_chase_atr: float = 0.5,
    existing: Optional[StructureView] = None,
) -> tuple[StructureView, bool, str]:
    """Daily confirm + 1H first pullback into the daily zone + 1H reconfirm.

    `existing` is an already-armed daily view: 1H dips do not disarm it, and
    daily does not need to re-print a fresh HH/HL every tick. Only a daily
    close through invalidation returns ``daily_structure_break``.
    """
    if existing is not None and existing.side is not None:
        view = existing
        if structure_bars:
            view = StructureView(
                side=existing.side,
                confirmed=True,
                invalidation=existing.invalidation,
                zone_low=existing.zone_low,
                zone_high=existing.zone_high,
                confirm_index=existing.confirm_index,
                reason=existing.reason,
                last_close=structure_bars[-1].close,
                atr=existing.atr or atr(structure_bars, atr_period),
                confirm_ts=existing.confirm_ts or _confirm_ts(structure_bars, existing.confirm_index),
            )
    else:
        view = confirm_and_zone(
            structure_bars,
            left=left,
            right=right,
            atr_period=atr_period,
            vp_lookback=vp_lookback,
            vp_bin=vp_bin,
            vp_percentile=vp_percentile,
        )
        if not view.confirmed or view.side is None:
            return view, False, DAILY_NOT_CONFIRMED

    if structure_bars and structure_broken(view, structure_bars[-1]):
        return view, False, DAILY_STRUCTURE_BREAK

    pulled, why = first_pullback(timing_bars, view, no_chase_atr=no_chase_atr)
    if not pulled:
        return view, False, _remap_pullback_reason(why)

    ok, _ = timing_reconfirm(timing_bars, view.side, left=left, right=right)
    if not ok:
        return view, False, RECONFIRM_FAILED
    return view, True, HYBRID_READY


def _remap_pullback_reason(why: str) -> str:
    if why == "not_confirmed":
        return DAILY_NOT_CONFIRMED
    if why == "no_pullback_yet":
        return WAITING_1H_PULLBACK
    if why in {"no_chase_extended", "pullback_left_zone"}:
        return CHASE
    if why == "structure_break":
        return DAILY_STRUCTURE_BREAK
    return why


def _confirm_ts(bars: list[Bar], index: int) -> Optional[datetime]:
    if 0 <= index < len(bars):
        return bars[index].ts
    return None


def _bars_after_confirm(bars: list[Bar], view: StructureView) -> list[Bar]:
    if view.confirm_ts is not None:
        return [b for b in bars if b.ts > view.confirm_ts]
    if 0 <= view.confirm_index < len(bars):
        return list(bars[view.confirm_index + 1 :])
    return list(bars)


def _overlaps_zone(bar: Bar, zone_low: float, zone_high: float) -> bool:
    return bar.low <= zone_high and bar.high >= zone_low
