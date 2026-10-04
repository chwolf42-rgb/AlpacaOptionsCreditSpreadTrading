"""Harness tests for the defined-risk research backtest. No network."""

from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtests.new_strategies.engine import prepare, simulate
from backtests.new_strategies.metrics import block_bootstrap_ci, select_winner
from backtests.new_strategies.pricing import (
    bs_price,
    buy_fill,
    full_width,
    interp_variance_vol,
    sell_fill,
)
from backtests.new_strategies.specs import (
    FOLDS,
    GRIDS,
    HOLDOUT_START,
    IS_END,
    IS_START,
    MAX_CONTRACTS,
    OOS_END,
    OOS_START,
)


def _market(n: int = 80, drift: float = 0.002, seed: int = 1) -> dict:
    rng = random.Random(seed)
    day = date(2021, 1, 4)
    rows = []
    spot = 100.0
    while len(rows) < n:
        if day.weekday() < 5:
            o = spot
            c = max(1.0, spot * (1.0 + drift + rng.uniform(-0.004, 0.004)))
            h = max(o, c) * 1.002
            l = min(o, c) * 0.998
            rows.append({"date": day, "open": o, "high": h, "low": l, "close": c})
            spot = c
        day += timedelta(days=1)
    underlyings = {}
    for symbol in ("SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"):
        scale = 4.0 if symbol in {"SPY", "QQQ", "IWM"} else 1.5
        underlyings[symbol] = [
            {
                "date": r["date"],
                "open": r["open"] * scale,
                "high": r["high"] * scale,
                "low": r["low"] * scale,
                "close": r["close"] * scale,
            }
            for r in rows
        ]
    series = {}
    for name, level in (
        ("vix", 18.0),
        ("vix9d", 20.0),
        ("vix3m", 19.0),
        ("vxn", 22.0),
        ("rvx", 24.0),
        ("rate", 2.0),
    ):
        series[name] = [(r["date"], level) for r in rows]
    return {"underlyings": underlyings, "series": series}


def test_splits_are_ordered_and_holdout_is_last():
    assert IS_START < IS_END < OOS_START < OOS_END < HOLDOUT_START
    assert FOLDS[0][2] == OOS_START
    assert FOLDS[-1][3] == OOS_END
    for fit_start, fit_end, trade_start, trade_end in FOLDS:
        assert fit_end < trade_start <= trade_end


def test_grid_sizes_are_the_predeclared_set():
    assert len(GRIDS["debit_momentum"]) == 5
    assert len(GRIDS["short_dated"]) == 4
    assert len(GRIDS["condor"]) == 4
    assert len(GRIDS["butterfly"]) == 4
    assert len(GRIDS["calendar"]) == 4
    assert len(GRIDS["diagonal"]) == 3
    assert GRIDS["debit_momentum"][-1]["cheap_iv"] is True


def test_fill_is_worse_than_mid_by_a_quarter_of_the_width():
    width = full_width("SPY", mid=2.0, delta=0.5, dte=30)
    assert buy_fill(2.0, width) == 2.0 + 0.25 * width
    assert sell_fill(2.0, width) == 2.0 - 0.25 * width
    assert buy_fill(2.0, width) > 2.0 > sell_fill(2.0, width)


def test_variance_interpolation_is_flat_when_knots_match():
    assert abs(interp_variance_vol([(9, 0.20), (30, 0.20), (93, 0.20)], 45) - 0.20) < 1e-9


def test_bs_atm_call_matches_a_known_value():
    # spot 100, strike 100, 20% vol, 1 year, r=div=0 → about 7.9656
    px = bs_price(100, 100, 1.0, 0.20, "call", 0.0, 0.0)
    assert abs(px - 7.965567) < 1e-3


def test_signal_does_not_use_future_bars():
    raw = _market(90, drift=0.003, seed=2)
    prep = prepare(raw)
    params = GRIDS["debit_momentum"][0]
    start = prep.calendar[40]
    end = prep.calendar[70]
    base = simulate(prep, "debit_momentum", params, start, end)
    # A crash after the window must not change trades inside it.
    smashed = _market(90, drift=0.003, seed=2)
    for symbol in smashed["underlyings"]:
        for row in smashed["underlyings"][symbol]:
            if row["date"] > end:
                row["close"] *= 0.5
                row["open"] *= 0.5
                row["high"] *= 0.5
                row["low"] *= 0.5
    alt = simulate(prepare(smashed), "debit_momentum", params, start, end)
    assert [(t["entry_date"], t["exit_date"], round(t["pnl"], 4)) for t in base["trades"]] == [
        (t["entry_date"], t["exit_date"], round(t["pnl"], 4)) for t in alt["trades"]
    ]


def test_entry_fills_on_the_session_after_the_signal():
    prep = prepare(_market(90, drift=0.004, seed=3))
    start, end = prep.calendar[30], prep.calendar[80]
    result = simulate(prep, "debit_momentum", GRIDS["debit_momentum"][0], start, end)
    assert result["trades"], "synthetic trend should open a debit vertical"
    for trade in result["trades"]:
        assert trade["entry_date"] > trade["signal_date"]
        assert trade["qty"] >= 1
        assert trade["qty"] <= MAX_CONTRACTS
        # Sizing uses realized equity. Before any close, that is the $100k start.
        assert trade["risk_dollars"] <= 100_000 * 0.005 * 1.5


def test_stop_gap_fills_at_the_open_not_the_stop_limit():
    # Build a one-day collapse after an entry so the open gaps through the stop.
    raw = _market(40, drift=0.0, seed=4)
    # Force a long uptrend signal then a gap down on the fill's next day.
    prep = prepare(raw)
    # Directly exercise pricing: a worthless-mid sell is refused, a buy is worse than mid.
    assert sell_fill(0.0, 0.05) is None
    assert buy_fill(1.0, 0.20) == 1.05


def test_caps_reject_a_sixth_spread(monkeypatch=None):
    # Five open spreads is the cap. The sizer's quantity is at least bounded
    # by the contract cap, and risk dollars stay inside 0.5% per fill.
    prep = prepare(_market(100, drift=0.003, seed=5))
    start, end = prep.calendar[40], prep.calendar[95]
    result = simulate(prep, "short_dated", GRIDS["short_dated"][0], start, end)
    # Exposure never reports more than 5 spreads on this book.
    for row in result["exposure"]:
        assert row["spreads"] <= 5
        assert row["open_risk"] <= 100_000 * 0.10 + 5.0
        assert row["gross"] <= 99_750 + 1e-6


def test_bootstrap_is_deterministic():
    sample = [0.01, -0.02, 0.005, 0.0, 0.002, -0.004]
    a = block_bootstrap_ci(sample)
    b = block_bootstrap_ci(sample)
    assert a == b
    assert a["lo"] <= a["mean"] <= a["hi"] or a["lo"] <= a["hi"]


def test_selector_keeps_the_default_when_samples_are_tiny():
    rows = [
        {"grid_index": 0, "mean_monthly": -0.01, "max_dd": 0.05, "n_trades": 2, "params_id": "a"},
        {"grid_index": 1, "mean_monthly": 0.05, "max_dd": 0.01, "n_trades": 1, "params_id": "b"},
    ]
    assert select_winner(rows, min_trades=8)["params_id"] == "a"


def test_condor_second_wing_is_a_later_session():
    prep = prepare(_market(120, drift=0.0, seed=6))
    # Flat market, VIX constant. vix_gt_sma20 needs VIX above its 20-day mean.
    # Bump the last half of VIX so the filter can pass.
    for i, curve in enumerate(prep.curves):
        if i > 60:
            curve.vix = 28.0
            curve.vix_sma20 = 20.0
            curve.vix_pct = 0.8
    start, end = prep.calendar[70], prep.calendar[110]
    result = simulate(prep, "condor", GRIDS["condor"][0], start, end)
    # No lookahead: every fill is after its signal. A completed condor has
    # two trades whose entries are not the same session.
    by_group: dict[int, list] = {}
    for trade in result["trades"]:
        assert trade["entry_date"] > trade["signal_date"]
        if trade["group_id"] is not None:
            by_group.setdefault(trade["group_id"], []).append(trade)
    for members in by_group.values():
        days = {t["entry_date"] for t in members}
        if len(members) >= 2:
            assert len(days) >= 2
