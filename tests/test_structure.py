from datetime import timedelta

from alpaca_options_credit.models import Side
from alpaca_options_credit.strategy.structure import (
    CHASE,
    DAILY_NOT_CONFIRMED,
    DAILY_STRUCTURE_BREAK,
    HYBRID_READY,
    RECONFIRM_FAILED,
    WAITING_1H_PULLBACK,
    confirm_and_zone,
    first_pullback,
    hybrid_entry,
    structure_broken,
    timing_reconfirm,
)
from tests.helpers import (
    bar,
    bullish_confirm_pullback_bars,
    dip_holds_bars,
    flat_daily_bars,
    hybrid_happy_daily_hourly,
    hourly_chase_extended,
    hourly_in_zone_no_reconfirm,
    hourly_waiting_no_tag,
    no_chase_extended_bars,
)


def test_strict_confirm_then_first_pullback():
    bars = bullish_confirm_pullback_bars()
    view = confirm_and_zone(bars, left=2, right=2, vp_bin=0.5, vp_lookback=25)
    assert view.confirmed, view.reason
    assert view.side is not None
    pulled, why = first_pullback(bars, view, no_chase_atr=0.5)
    assert pulled, why


def test_daily_structure_break_on_close_through_invalidation():
    daily, hourly = hybrid_happy_daily_hourly()
    view = confirm_and_zone(daily[:-1], left=2, right=2)
    assert view.confirmed
    broken = daily[:-1] + [bar(len(daily), 103.0, 98.5, 99.0, 1_000_000, step="day")]
    # Align the break bar after the last daily timestamp.
    broken[-1] = broken[-1].__class__(
        ts=daily[-1].ts + timedelta(days=1),
        open=99.0,
        high=103.0,
        low=98.5,
        close=99.0,
        volume=1_000_000,
    )
    assert structure_broken(view, broken[-1]) is True
    _, ready, why = hybrid_entry(broken, hourly, existing=view)
    assert ready is False
    assert why == DAILY_STRUCTURE_BREAK


def test_normal_dip_does_not_break_structure():
    bars = dip_holds_bars()
    view = confirm_and_zone(bars[:-1], left=2, right=2)
    assert view.confirmed
    assert structure_broken(view, bars[-1]) is False


def test_no_chase_when_extended():
    bars = no_chase_extended_bars()
    view = confirm_and_zone(bars, left=2, right=2)
    if not view.confirmed:
        base = confirm_and_zone(bullish_confirm_pullback_bars(), left=2, right=2)
        pulled, why = first_pullback(bars, base, no_chase_atr=0.5)
    else:
        pulled, why = first_pullback(bars, view, no_chase_atr=0.5)
    assert pulled is False
    assert "chase" in why or why in {"no_pullback_yet", "pullback_left_zone", "no_chase_extended"}


def test_hybrid_daily_confirm_1h_pullback_reconfirm():
    daily, hourly = hybrid_happy_daily_hourly()
    view, ready, why = hybrid_entry(daily, hourly)
    assert view.confirmed, view.reason
    assert view.side is Side.BULLISH
    assert ready, why
    assert why == HYBRID_READY


def test_hybrid_daily_not_confirmed():
    daily = flat_daily_bars()
    hourly = bullish_confirm_pullback_bars()
    view, ready, why = hybrid_entry(daily, hourly)
    assert view.confirmed is False
    assert ready is False
    assert why == DAILY_NOT_CONFIRMED


def test_hybrid_waiting_1h_pullback():
    daily, _ = hybrid_happy_daily_hourly()
    view = confirm_and_zone(daily, left=2, right=2)
    assert view.confirmed
    hourly = hourly_waiting_no_tag(view.confirm_ts)
    _, ready, why = hybrid_entry(daily, hourly)
    assert ready is False
    assert why == WAITING_1H_PULLBACK


def test_hybrid_1h_reconfirm_failed():
    daily, _ = hybrid_happy_daily_hourly()
    view = confirm_and_zone(daily, left=2, right=2)
    hourly = hourly_in_zone_no_reconfirm(view.confirm_ts)
    pulled, pb_why = first_pullback(hourly, view, no_chase_atr=0.5)
    assert pulled, pb_why
    ok, rwhy = timing_reconfirm(hourly, view.side)
    assert ok is False
    assert rwhy == RECONFIRM_FAILED
    _, ready, why = hybrid_entry(daily, hourly)
    assert ready is False
    assert why == RECONFIRM_FAILED


def test_hybrid_chase_when_1h_extended():
    daily, _ = hybrid_happy_daily_hourly()
    view = confirm_and_zone(daily, left=2, right=2)
    hourly = hourly_chase_extended(view.confirm_ts)
    _, ready, why = hybrid_entry(daily, hourly)
    assert ready is False
    assert why == CHASE


def test_hybrid_1h_dip_does_not_cancel_daily_arm():
    daily, hourly = hybrid_happy_daily_hourly()
    view, _, _ = hybrid_entry(daily, hourly)
    assert view.confirmed
    # 1H closes through daily invalidation; daily last close still holds.
    dipped = list(hourly)
    last = dipped[-1]
    dipped[-1] = last.__class__(
        ts=last.ts + timedelta(hours=1),
        open=101.0,
        high=103.0,
        low=98.5,
        close=99.0,
        volume=900_000,
    )
    assert structure_broken(view, dipped[-1]) is True
    assert structure_broken(view, daily[-1]) is False
    out, ready, why = hybrid_entry(daily, dipped, existing=view)
    assert why != DAILY_STRUCTURE_BREAK
    assert out.side is view.side
    assert ready is False


def test_same_series_legacy_fixture_still_confirms():
    """Observer fixture uses one geometry on both TFs; hybrid must still ready."""
    bars = bullish_confirm_pullback_bars()
    view, ready, why = hybrid_entry(bars, bars)
    assert ready, why
    assert view.side is Side.BULLISH
