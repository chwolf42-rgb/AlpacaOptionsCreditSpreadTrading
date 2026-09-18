from alpaca_options_credit.strategy.spreads import pick_short_strike


def test_bull_put_short_strike_at_or_just_beyond_invalidation():
    listed = [180.0, 182.0, 182.5, 183.0, 185.0]
    # invalidation 182.40 → nearest 182.5 is slightly inside; step to 182.0 (beyond/down)
    assert pick_short_strike(182.40, listed, adverse="down") == 182.0
    # exactly on a listed strike → use it (at)
    assert pick_short_strike(182.50, listed, adverse="down") == 182.5
    # already beyond
    assert pick_short_strike(182.00, listed, adverse="down") == 182.0


def test_bear_call_short_strike_at_or_just_beyond_invalidation():
    listed = [190.0, 192.0, 192.5, 193.0, 195.0]
    # 192.40 → nearest 192.5 is just beyond/up (at-or-beyond)
    assert pick_short_strike(192.40, listed, adverse="up") == 192.5
    assert pick_short_strike(192.50, listed, adverse="up") == 192.5
    # nearest 192.0 is inside (below); step up to 192.5
    assert pick_short_strike(192.10, [190.0, 192.0, 192.5], adverse="up") == 192.5
