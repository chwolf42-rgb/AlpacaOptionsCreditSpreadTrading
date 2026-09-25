"""Pure close-debit helpers: quote/bar selection and broker fill parsing."""

from datetime import datetime, timedelta, timezone

import pytest

from alpaca_options_credit.close_prices import (
    bar_closes_from_prints,
    classify_close_order,
    close_order_view_from_broker_order,
    leg_price_at,
    quote_mid,
    quote_mids_from_prints,
    select_price_at,
    spread_debit,
)


AT = datetime(2026, 9, 21, 18, 4, tzinfo=timezone.utc)


def test_select_price_at_uses_latest_print_not_after_the_close():
    points = [
        (AT - timedelta(minutes=40), 1.00),
        (AT - timedelta(minutes=5), 1.25),
        (AT + timedelta(minutes=1), 9.00),
    ]
    assert select_price_at(points, AT, max_age=timedelta(minutes=30)) == 1.25
    assert select_price_at(points, AT, max_age=timedelta(minutes=2)) is None


def test_leg_price_prefers_quote_mid_then_bar_close():
    quotes = [(AT - timedelta(minutes=2), 0.80)]
    bars = [(AT - timedelta(minutes=2), 0.40)]
    assert leg_price_at(quotes=quotes, bars=bars, at=AT) == 0.80
    assert leg_price_at(quotes=[], bars=bars, at=AT) == 0.40
    stale = [(AT - timedelta(hours=20), 0.20)]
    assert leg_price_at(quotes=[], bars=stale, at=AT) is None


def test_raw_quote_and_bar_prints_become_leg_prices():
    quotes = quote_mids_from_prints(
        [
            {"t": "2026-09-21T18:02:00Z", "bp": 1.20, "ap": 1.40},
            {"t": "2026-09-21T18:03:00Z", "bp": 0, "ap": 1.10},
        ]
    )
    assert len(quotes) == 1
    assert quotes[0][1] == pytest.approx(1.30)
    bars = bar_closes_from_prints(
        [{"timestamp": AT - timedelta(minutes=1), "close": 0.80}, {"close": 0}]
    )
    assert bars == [(AT - timedelta(minutes=1), 0.80)]
    assert leg_price_at(quotes=quotes, bars=bars, at=AT) == pytest.approx(1.30)


def test_spread_debit_is_short_minus_long():
    assert quote_mid(1.20, 1.40) == pytest.approx(1.30)
    assert quote_mid(1.40, 1.20) is None
    assert quote_mid(0.0, 0.10) is None
    assert spread_debit(1.30, 0.40) == 0.90
    assert spread_debit(None, 0.40) is None


def test_close_order_view_uses_parent_net_and_leg_fallback():
    class Status:
        value = "filled"

    parent = type(
        "Order",
        (),
        {
            "id": "ord-1",
            "status": Status(),
            "filled_qty": "2",
            "qty": "2",
            "filled_avg_price": "1.55",
            "filled_at": AT,
            "legs": [],
        },
    )()
    view = close_order_view_from_broker_order(parent)
    assert view.state == "filled"
    assert view.filled_qty == 2
    assert view.net_debit == 1.55
    assert view.filled_at == AT.isoformat()

    class Open:
        value = "partially_filled"

    working = type(
        "Order",
        (),
        {
            "id": "ord-2",
            "status": Open(),
            "filled_qty": "1",
            "qty": "2",
            "filled_avg_price": "1.10",
            "filled_at": None,
            "legs": [],
        },
    )()
    assert close_order_view_from_broker_order(working).state == "open"

    dead = type(
        "Order",
        (),
        {
            "id": "ord-3",
            "status": "canceled",
            "filled_qty": "0",
            "qty": "2",
            "filled_avg_price": None,
            "filled_at": None,
            "legs": [],
        },
    )()
    assert close_order_view_from_broker_order(dead).state == "dead"

    legs = type(
        "Order",
        (),
        {
            "id": "ord-4",
            "status": "expired",
            "filled_qty": "1",
            "qty": "2",
            "filled_avg_price": None,
            "filled_at": None,
            "updated_at": AT,
            "legs": [
                {"position_intent": "buy_to_close", "side": "buy", "filled_avg_price": "2.00"},
                {"position_intent": "sell_to_close", "side": "sell", "filled_avg_price": "0.40"},
            ],
        },
    )()
    partial = close_order_view_from_broker_order(legs)
    assert partial.state == "partial"
    assert partial.net_debit == 1.60
    assert partial.filled_at == AT.isoformat()
    assert classify_close_order("filled", 0, 2) == "filled"
