"""Higher-TF confirm → arm → first pullback. Structure break cancels; dips do not.

Default timeframe is 1Hour (config). 30Min is supported via the same swing logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from alpaca_options_credit.models import Bar, Side
from alpaca_options_credit.strategy.volume_profile import hvn_shelves, nearest_shelf


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
    vp_lookback: int = 80,
    vp_bin: float = 0.5,
    vp_percentile: float = 0.70,
) -> StructureView:
    """Strict confirm: HH+HL (bull) or LL+LH (bear), last close through the break level."""
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
    )


def structure_broken(view: StructureView, last: Bar) -> bool:
    """Close through invalidation. Wicks/dips that hold the close do NOT cancel."""
    if view.invalidation is None or view.side is None:
        return False
    if view.side is Side.BULLISH:
        return last.close < view.invalidation
    return last.close > view.invalidation


def first_pullback(
    bars: list[Bar],
    view: StructureView,
    *,
    no_chase_atr: float = 0.5,
) -> tuple[bool, str]:
    """True when the first retrace tags the VP/S-R shelf while structure holds.

    No chase: last close still inside/near the zone, not extended in trend.
    """
    if not view.confirmed or view.side is None or view.zone_low is None or view.zone_high is None:
        return False, "not_confirmed"
    last = bars[-1]
    if structure_broken(view, last):
        return False, "structure_break"

    tagged = False
    after = view.confirm_index
    for bar in bars[after + 1 :]:
        if _overlaps_zone(bar, view.zone_low, view.zone_high):
            tagged = True
            break
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


def _overlaps_zone(bar: Bar, zone_low: float, zone_high: float) -> bool:
    return bar.low <= zone_high and bar.high >= zone_low
