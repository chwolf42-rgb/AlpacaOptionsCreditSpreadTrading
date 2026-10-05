"""Isolated-spike clamp, its t+2 visibility, and the pinned adjustment factor."""

from __future__ import annotations

import ast
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.intraday_sr.data.adjust import as_traded, load_adj_factors
from research.intraday_sr.data.badprint import flag_summary, prices_as_of, repair_bad_prints
from research.intraday_sr.data.cache import iter_symbol_bars, normalize_bars
from research.intraday_sr.data.calendar import sessions_between
from research.intraday_sr.data.holdout import HoldoutLocked, HoldoutToken
from research.intraday_sr.engine import levels_at, zones_at
from research.intraday_sr.harness.walkforward import run_holdout
from research.intraday_sr.tests.fixtures.synthetic import session_opens
from research.intraday_sr.types import BarSet, EngineCfg

_PACKAGE = Path(__file__).resolve().parents[1]


def _session(day: date, closes: np.ndarray, *, spike_at: int | None = None) -> pd.DataFrame:
    opens = session_opens(day)
    closes = np.asarray(closes, dtype=np.float64)
    bar_open = np.empty(len(closes))
    bar_open[0] = closes[0]
    bar_open[1:] = closes[:-1]
    high = np.maximum(bar_open, closes) + 0.05
    low = np.minimum(bar_open, closes) - 0.05
    if spike_at is not None:
        high[spike_at] = closes[spike_at] + 20.0
        low[spike_at] = closes[spike_at] - 0.05
    return pd.DataFrame(
        {
            "symbol": "SPY",
            "tf": "5m",
            "ts": opens,
            "available_at": [ts + timedelta(minutes=5) for ts in opens],
            "open": bar_open,
            "high": high,
            "low": low,
            "close": closes,
            "volume": np.full(len(closes), 1000.0),
            "vwap": closes,
            "trades": np.full(len(closes), 10),
            "session": [day] * len(closes),
            "adj_factor": np.ones(len(closes)),
        }
    )


def test_spike_clamps_at_t_plus_2_and_pivots_at_t_plus_1_stay_raw():
    """A peak whose window contains the spike is blocked until the clamp is visible.

    N=3 confirms a pivot at index 8 on the close of bar 11, which is t+1 of the
    spike at index 10. The spike's own pivot would confirm only at bar 13, after
    the clamp is already visible, so the lookahead check is the neighbouring peak.
    """
    day = date(2024, 6, 12)
    closes = np.full(78, 100.0)
    raw = _session(day, closes, spike_at=10)
    peak = 8
    peak_high = 100.15
    raw.loc[peak, "high"] = peak_high
    raw_high = float(raw["high"].iloc[10])
    repaired, count = repair_bad_prints(raw, {("SPY", day): 2.0})
    assert count == 1
    assert bool(repaired["bad_print"].iloc[10])
    assert float(repaired["high_unclamped"].iloc[10]) == pytest.approx(raw_high)
    assert float(repaired["high"].iloc[10]) < raw_high
    assert float(repaired["high"].iloc[peak]) == pytest.approx(peak_high)
    visible_at = pd.Timestamp(repaired["bad_print_visible_at"].iloc[10])
    t1 = pd.Timestamp(raw["available_at"].iloc[11])
    seen = prices_as_of(repaired, t1.to_pydatetime())
    assert float(seen["high"].iloc[10]) == pytest.approx(raw_high)
    assert bool(seen["bad_print"].iloc[10]) is False
    later = prices_as_of(repaired, visible_at.to_pydatetime())
    assert float(later["high"].iloc[10]) == pytest.approx(float(repaired["high"].iloc[10]))

    history = []
    for prior in sessions_between(date(2024, 5, 13), date(2024, 6, 11)):
        prior_frame = _session(prior, np.full(78, 100.0))
        prior_frame["high_unclamped"] = prior_frame["high"]
        prior_frame["low_unclamped"] = prior_frame["low"]
        prior_frame["bad_print"] = False
        prior_frame["bad_print_visible_at"] = prior_frame["available_at"]
        history.append(prior_frame)
    tape = pd.concat(history + [repaired], ignore_index=True).sort_values("ts").reset_index(drop=True)
    bars = BarSet(tape)
    cfg = EngineCfg()
    # Zones recompute on 15m closes. 10:30 is t+1 (raw). The clamp is visible
    # at 10:35, so the next zone set that can see it is the 10:45 close.
    zone_late = pd.Timestamp(raw["available_at"].iloc[14])
    early = levels_at(bars, t1.to_pydatetime(), cfg)
    late = levels_at(bars, visible_at.to_pydatetime(), cfg)
    early_zones = zones_at(bars, t1.to_pydatetime(), cfg)
    late_zones = zones_at(bars, zone_late.to_pydatetime(), cfg)

    def _near(prices, target: float) -> bool:
        return any(abs(price - target) < 1e-3 for price in prices)

    early_prices = [level.price for level in early]
    late_prices = [level.price for level in late]
    assert not _near(early_prices, peak_high)
    assert not _near(early_prices, raw_high)
    assert _near(late_prices, peak_high)
    assert not _near(late_prices, raw_high)
    early_centers = [0.5 * (zone.low + zone.high) for zone in early_zones]
    late_centers = [0.5 * (zone.low + zone.high) for zone in late_zones]
    assert not _near(early_centers, peak_high)
    assert not _near(early_centers, raw_high)
    assert _near(late_centers, peak_high)
    assert not _near(late_centers, raw_high)


def test_normalize_flags_a_spike_from_wilder_atr():
    days = sessions_between(date(2024, 5, 13), date(2024, 6, 12))
    frames = []
    for day in days:
        frame = _session(day, np.full(78, 100.0), spike_at=10 if day == days[-1] else None)
        frame["ts"] = [ts.replace(tzinfo=None) for ts in frame["ts"]]
        frames.append(frame)
    raw = pd.concat(frames, ignore_index=True)
    factors = pd.Series(
        1.0,
        index=pd.MultiIndex.from_product([["SPY"], days], names=["symbol", "session"]),
    )
    out, report = normalize_bars(raw, factors=factors, start=days[0], end=days[-1])
    assert report.bad_prints == 1
    assert report.review is False
    spike_day = days[-1].year * 10000 + days[-1].month * 100 + days[-1].day
    flagged = out.loc[out["bad_print"].to_numpy(dtype=bool)]
    assert int(flagged["session"].iloc[0]) == spike_day
    assert float(flagged["high_unclamped"].iloc[0]) == pytest.approx(120.0)
    assert float(flagged["high"].iloc[0]) == pytest.approx(100.0)


def test_session_end_bars_are_unchecked_and_unclamped():
    day = date(2024, 6, 12)
    raw = _session(day, np.full(78, 100.0), spike_at=77)
    repaired, count = repair_bad_prints(raw, {("SPY", day): 2.0})
    assert count == 0
    assert repaired.attrs["session_end_unchecked"] == 2
    assert not bool(repaired["bad_print"].iloc[-2:].any())
    assert float(repaired["high"].iloc[-1]) == float(raw["high"].iloc[-1])


def test_a_real_move_that_does_not_revert_is_left_alone():
    day = date(2024, 6, 12)
    closes = np.full(78, 100.0)
    closes[10:] = 130.0
    raw = _session(day, closes, spike_at=10)
    repaired, count = repair_bad_prints(raw, {("SPY", day): 2.0})
    assert count == 0
    assert float(repaired["high"].iloc[10]) == pytest.approx(float(raw["high"].iloc[10]))


def test_malformed_bars_are_dropped_and_counted():
    day = date(2024, 6, 12)
    frame = _session(day, np.full(78, 100.0))
    frame.loc[3, "high"] = 1.0
    frame.loc[3, "low"] = 2.0
    frame.loc[4, "close"] = -1.0
    factors = pd.Series({("SPY", day): 1.0})
    out, report = normalize_bars(frame, factors=factors, start=day, end=day)
    assert report.malformed == 2
    assert len(out) == 76
    assert str(out["symbol"].dtype) == "category"
    assert str(out["tf"].dtype) == "category"
    assert str(out["session"].dtype) == "int32"
    assert int(out["session"].iloc[0]) == 20240612


def test_holdout_default_refuses_april_and_the_token_is_single_sourced(tmp_path):
    day = date(2026, 4, 2)
    frame = _session(day, np.full(78, 100.0))
    factors = pd.Series({("SPY", day): 1.0})
    with pytest.raises(HoldoutLocked):
        normalize_bars(frame, factors=factors, start=day, end=day)
    with pytest.raises(HoldoutLocked):
        token_type = HoldoutToken
        token_type(tmp_path / "FREEZE.md")
    freeze = tmp_path / "FREEZE.md"
    freeze.write_text("frozen\n", encoding="utf-8")
    token = run_holdout(freeze)
    out, _report = normalize_bars(frame, factors=factors, start=day, end=day, token=token)
    assert len(out) == 78
    hits = []
    for path in _PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in {"HoldoutToken", "_from_freeze"} and path.name != "walkforward.py":
                hits.append(f"{path.name}:{name}")
    assert hits == []


def test_aapl_factor_is_the_inverse_of_trading(tmp_path):
    # Trading stores adj/raw. 37.44/157.92 inverted is about 4.22.
    trading = 37.44 / 157.92
    frame = pd.DataFrame(
        {"symbol": ["AAPL"], "date": ["2019-01-02"], "adj_factor": [trading]}
    )
    path = tmp_path / "adj_factors.parquet"
    frame.to_parquet(path, index=False)
    factors = load_adj_factors(path)
    value = float(factors.loc[("AAPL", date(2019, 1, 2))])
    assert value == pytest.approx(157.92 / 37.44, rel=1e-6)
    assert value == pytest.approx(4.22, abs=0.005)
    assert as_traded(37.44, value) == pytest.approx(157.92, rel=1e-6)


def test_iterator_does_not_require_every_symbol_up_front(tmp_path):
    day = date(2024, 6, 12)
    factors = pd.Series({("SPY", day): 1.0, ("QQQ", day): 1.0})
    for part, (symbol, level) in enumerate((("SPY", 100.0), ("QQQ", 200.0))):
        frame = _session(day, np.full(78, level))
        frame["symbol"] = symbol
        frame["ts"] = [ts.replace(tzinfo=None) for ts in frame["ts"]]
        frame.to_parquet(tmp_path / f"part_{part:04d}_{symbol}.parquet", index=False)
    seen = []
    for symbol, frame, report in iter_symbol_bars(tmp_path, factors=factors, symbols=("SPY", "QQQ"), start=day, end=day):
        seen.append(symbol)
        assert report.kept == 78
        assert len(frame) == 78
    assert seen == ["SPY", "QQQ"]


_CACHE = Path("/workspace/research2/data/alpaca_intraday/m5rth_fixed33")
_REGRESSION = (
    ("SPY", date(2019, 1, 30)),
    ("SPY", date(2019, 8, 1)),
    ("NVDA", date(2021, 9, 13)),
)


def _one_symbol(symbol: str) -> tuple[pd.DataFrame, object]:
    path = _CACHE / f"{symbol}.parquet"
    raw = pd.read_parquet(path)
    if "symbol" not in raw.columns:
        raw = raw.copy()
        raw["symbol"] = symbol
    days = pd.to_datetime(raw["ts"]).dt.date
    factors = pd.Series(1.0, index=pd.MultiIndex.from_arrays([raw["symbol"].astype(str), days], names=["symbol", "session"]))
    factors = factors.groupby(level=[0, 1]).last()
    start = min(days)
    end = max(days)
    if end >= date(2026, 4, 1):
        end = date(2026, 3, 31)
    return normalize_bars(raw, factors=factors, start=start, end=end, symbol=symbol)


@pytest.mark.parametrize("symbol,day", _REGRESSION)
def test_named_sessions_keep_every_bar_unflagged(symbol: str, day: date):
    path = _CACHE / f"{symbol}.parquet"
    if not path.is_file():
        pytest.skip(
            f"{symbol}.parquet is not in {_CACHE}; {symbol} {day.isoformat()} was not checked here"
        )
    frame, report = _one_symbol(symbol)
    session = pd.to_datetime(frame["session"]).dt.date
    kept = frame.loc[session == day]
    raw = pd.read_parquet(path)
    raw_days = pd.to_datetime(raw["ts"]).dt.date
    assert len(kept) == int((raw_days == day).sum())
    assert int(kept["bad_print"].sum()) == 0
    assert report.kept > 0


def test_flag_census_for_amzn_spy_iwm():
    missing = [name for name in ("AMZN", "SPY", "IWM") if not (_CACHE / f"{name}.parquet").is_file()]
    if missing:
        pytest.skip(f"flag census not computed; missing {missing} under {_CACHE}")
    rows = []
    for symbol in ("AMZN", "SPY", "IWM"):
        frame, _report = _one_symbol(symbol)
        rows.extend(flag_summary(frame))
    assert rows
