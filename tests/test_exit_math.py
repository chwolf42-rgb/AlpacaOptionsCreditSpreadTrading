import pytest

from alpaca_options_credit.strategy.spreads import (
    captured_frac,
    debit_to_close,
    stop_hit,
    take_profit_hit,
)


def test_tp_at_50pct_of_credit():
    credit = 1.20
    # remaining debit 0.60 → captured 0.60 = 50%
    assert take_profit_hit(credit, 0.60, 0.50) is True
    assert take_profit_hit(credit, 0.61, 0.50) is False
    assert captured_frac(credit, 0.60) == 0.5


def test_stop_at_2x_credit():
    credit = 1.20
    assert stop_hit(credit, 2.40, 2.0) is True
    assert stop_hit(credit, 2.39, 2.0) is False


def test_debit_to_close_is_short_minus_long():
    assert debit_to_close(1.10, 0.40) == pytest.approx(0.70)
