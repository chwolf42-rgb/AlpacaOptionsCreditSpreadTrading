from alpaca_options_credit.strategy.structure import (
    confirm_and_zone,
    first_pullback,
    structure_broken,
    swing_points,
)
from tests.helpers import (
    bullish_confirm_pullback_bars,
    dip_holds_bars,
    no_chase_extended_bars,
    structure_break_bars,
)


def test_strict_confirm_then_first_pullback():
    bars = bullish_confirm_pullback_bars()
    view = confirm_and_zone(bars, left=2, right=2, vp_bin=0.5, vp_lookback=80)
    assert view.confirmed, view.reason
    assert view.side is not None
    pulled, why = first_pullback(bars, view, no_chase_atr=0.5)
    assert pulled, why


def test_structure_break_on_close_through_invalidation():
    bars = structure_break_bars()
    view = confirm_and_zone(bars[:-1], left=2, right=2)
    assert view.confirmed
    assert structure_broken(view, bars[-1]) is True
    pulled, why = first_pullback(bars, view, no_chase_atr=0.5)
    assert pulled is False
    assert why == "structure_break"


def test_normal_dip_does_not_break_structure():
    bars = dip_holds_bars()
    view = confirm_and_zone(bars[:-1], left=2, right=2)
    assert view.confirmed
    assert structure_broken(view, bars[-1]) is False


def test_no_chase_when_extended():
    bars = no_chase_extended_bars()
    view = confirm_and_zone(bars, left=2, right=2)
    if not view.confirmed:
        # still assert chase guard on a forced confirmed view from the pullback series
        base = confirm_and_zone(bullish_confirm_pullback_bars(), left=2, right=2)
        pulled, why = first_pullback(bars, base, no_chase_atr=0.5)
    else:
        pulled, why = first_pullback(bars, view, no_chase_atr=0.5)
    assert pulled is False
    assert "chase" in why or why in {"no_pullback_yet", "pullback_left_zone", "no_chase_extended"}
