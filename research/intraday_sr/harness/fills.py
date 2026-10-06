"""Bar-level fill rules (SPEC section 4.1-4.2, section 7 limit-target rule). Pure functions, hand-checkable.

Prices are ADJUSTED; `tick` is the as-traded $0.01 expressed in adjusted units (0.01 / adj_factor).
direction: +1 long, -1 short.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

STOP, TARGET = "stop", "target"


def stop_entry_fill(direction: int, trigger: float, o: float, h: float, l: float) -> Optional[float]:
    """Buy-stop fills at max(trigger, open) when the bar trades through the trigger (mirror for sells)."""
    if direction > 0:
        return max(trigger, o) if h >= trigger else None
    return min(trigger, o) if l <= trigger else None


@dataclass(frozen=True)
class ExitHit:
    price: float
    kind: str              # stop | target
    gap: bool              # filled at the bar open
    ambiguous: bool        # stop and target both touched in this bar (stop assumed)


def exit_on_bar(direction: int, stop: float, target: float, o: float, h: float, l: float, tick: float,
                entry_bar: bool, legacy_target_gap: bool = False) -> Optional[ExitHit]:
    """Stop/target for one bar. Gap beyond the stop -> open. An open beyond the target by ANY amount (even less
    than one tick) is a gap and fills at the (better) open; a target touched from inside the bar (open on the near
    side) needs a trade-through by one tick and fills at the target. Both touched -> stop. On the entry bar only the
    stop is checked (intrabar order unknown), but an also-touched target is flagged ambiguous.

    Every fill lies inside [l, h] (given an entry-bar stop on the loss side of an entry fill inside the bar). Before
    the A1b fix the target gap needed `d*(o-target) >= tick`, so a bar opening past the target by less than a tick
    with the whole bar past it filled at `target`, outside the bar (FillOutsideBar; 51 errored variants in A1).
    legacy_target_gap=True restores that pre-A1b rule; attribution checks only (harness --legacy-target-gap)."""
    d = direction
    if not entry_bar:
        if d * (o - stop) <= 0:
            return ExitHit(o, STOP, True, False)
        if (d * (o - target) >= tick) if legacy_target_gap else (d * (o - target) > 0):
            return ExitHit(o, TARGET, True, False)
    if d > 0:
        hit_stop = l <= stop
        hit_tgt = h >= target + tick
    else:
        hit_stop = h >= stop
        hit_tgt = l <= target - tick
    if hit_stop:
        return ExitHit(stop, STOP, False, bool(hit_tgt))
    if hit_tgt and not entry_bar:
        return ExitHit(target, TARGET, False, False)
    return None


def widen_stop(direction: int, fill: float, stop: float, atr_d: float, min_atr: float) -> float:
    """Stop at least `min_atr` x ATR_d from the actual fill (SPEC section 5)."""
    min_dist = min_atr * atr_d
    if direction > 0:
        return min(stop, fill - min_dist)
    return max(stop, fill + min_dist)
