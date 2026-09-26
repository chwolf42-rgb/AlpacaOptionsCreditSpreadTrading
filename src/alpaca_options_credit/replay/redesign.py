"""Out-of-sample redesign of the credit-spread sleeve.

The search is fixed in ``designs`` before any result is read. A design is
eligible on the train window only when it has at least 50 trades and both
the dollar expectancy and the expectancy per unit of max risk have a 95%
bootstrap interval sitting entirely above zero. The eligible design with
the highest expectancy per unit of risk is the only one judged on the test
window. It is adopted only when that test interval is also entirely above
zero, both halves of the test window have a positive point estimate when
they have enough trades, the Jul–Sep 2026 slice does not sit entirely below
zero, and the 5-spread book is not a loss.

Looking at every design's test result and then picking the best one is not
a pass. Those rows are reported so a positive test that lost the train
selection is visible, and they are not implemented.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from alpaca_options_credit.config import load_config
from alpaca_options_credit.replay.alt_entry import AltSpec, replay_alt_universe
from alpaca_options_credit.replay.credit import ReplayLimits
from alpaca_options_credit.replay.data import load_bars
from alpaca_options_credit.replay.engine import replay_universe
from alpaca_options_credit.replay.oscillators import OscGate, gate_allows
from alpaca_options_credit.replay.regime import build_regime, max_drawdown, spy_buy_hold
from alpaca_options_credit.replay.stats import (
    BookStats,
    Interval,
    ReplayTrade,
    select_risk_book,
)
from alpaca_options_credit.replay.study import structure_from_config, week_span
from alpaca_options_credit.rth import as_et

TRAIN_START = date(2024, 1, 2)
TRAIN_END = date(2025, 6, 30)
TEST_START = date(2025, 7, 1)
TEST_END = date(2026, 9, 25)
RECENT_START = date(2026, 7, 6)
RECENT_END = date(2026, 9, 25)
LONG_START = date(2024, 1, 2)
LONG_END = date(2026, 9, 25)
H2_2025_END = date(2025, 12, 31)
Y2026_START = date(2026, 1, 2)

MIN_TRAIN = 50
MIN_TEST = 30
MIN_FOLD = 15
# Oscillator settings are rarer than the unfiltered book. The train pick
# needs this many trades. It still has to clear MIN_TRAIN and a positive
# interval before the global rule can adopt it.
MIN_OSC = 30
N_BOOT = 5000
BOOT_SEED = 20260925

# Index, sector, and the other ETFs on the full-A list. Single stocks are the rest.
ETFS = frozenset(
    {
        "SPY",
        "QQQ",
        "IWM",
        "DIA",
        "XLF",
        "XLE",
        "XLK",
        "XLU",
        "XOP",
        "XBI",
        "SMH",
        "GLD",
        "SLV",
        "TLT",
        "HYG",
        "EEM",
        "KRE",
    }
)

FOLDS = (
    (date(2024, 1, 2), date(2024, 6, 30), date(2024, 7, 1), date(2024, 12, 31)),
    (date(2024, 1, 2), date(2024, 12, 31), date(2025, 1, 2), date(2025, 6, 30)),
    (date(2024, 1, 2), date(2025, 6, 30), date(2025, 7, 1), date(2025, 12, 31)),
    (date(2024, 1, 2), date(2025, 12, 31), date(2026, 1, 2), date(2026, 9, 25)),
)


@dataclass(frozen=True)
class Design:
    name: str
    blurb: str
    family: str
    entry: str
    limits: ReplayLimits
    etf_only: bool = False
    stock_only: bool = False
    trend: bool = False
    iv_pct_min: Optional[float] = None
    iv_over_rv: bool = False
    vix_pct_min: Optional[float] = None
    vix_rich: bool = False
    searchable: bool = True
    osc: Optional[OscGate] = None
    control: Optional[str] = None


def _limits(**kwargs) -> ReplayLimits:
    base = dict(
        width=5.0,
        dte_min=30,
        dte_max=45,
        dte_target=37,
        min_credit_pct=0.10,
        max_credit_pct=1.0,
        min_short_inv_gap=1.0,
        tp_frac=0.50,
        stop_mult=1.5,
        stop_check="none",
        target_abs_delta=0.16,
        delta_tol=0.08,
        close_dte=None,
        spot_stop="invalidation",
        fill_mode="natural",
        width_pct=None,
        earnings_blackout_days=3,
    )
    base.update(kwargs)
    return ReplayLimits(**base)


def _managed(**kwargs) -> ReplayLimits:
    """50% take-profit, close at 21 DTE, 2× credit stop on the close, shelf stop."""
    fields = dict(
        tp_frac=0.50,
        close_dte=21,
        stop_check="close",
        stop_mult=2.0,
        spot_stop="shelf",
    )
    fields.update(kwargs)
    return _limits(**fields)


def designs() -> list[Design]:
    """Every row the study is allowed to see. Order is the report order."""
    rows: list[Design] = [
        Design(
            "baseline",
            "Live book: confirm entry, nearest short, 20% of width, $5, 50% take-profit, no price stop, structure break. Earnings calendar empty, matching the book that was studied in PR #10.",
            "baseline",
            "confirm",
            _limits(
                min_credit_pct=0.20,
                target_abs_delta=None,
                delta_tol=None,
                earnings_blackout_days=0,
            ),
        ),
        Design(
            "baseline_earn",
            "Same live book, plus a 3-day earnings blackout around entry and expiration.",
            "baseline",
            "confirm",
            _limits(min_credit_pct=0.20, target_abs_delta=None, delta_tol=None),
        ),
    ]
    for delta, name in ((0.10, "d10"), (0.16, "d16"), (0.20, "d20"), (0.25, "d25"), (0.30, "d30")):
        rows.append(
            Design(
                name,
                f"Confirm entry, short nearest {int(delta*100)} delta beyond invalidation, $5 wide, minimum credit 10% of width, live exits.",
                "strike",
                "confirm",
                _limits(target_abs_delta=delta),
            )
        )
    rows.extend(
        [
            Design("d16_c20", "16 delta, $5, minimum credit raised to 20% of width, live exits.", "strike", "confirm", _limits(min_credit_pct=0.20)),
            Design("d16_c33", "16 delta, $5, minimum credit 33% of width, live exits.", "strike", "confirm", _limits(min_credit_pct=0.33)),
            Design("d20_c20", "20 delta, $5, minimum credit 20% of width, live exits.", "strike", "confirm", _limits(target_abs_delta=0.20, min_credit_pct=0.20)),
            Design("d16_w10", "16 delta, $10 wide, minimum credit 10%, live exits.", "strike", "confirm", _limits(width=10.0)),
            Design("d20_w10", "20 delta, $10 wide, minimum credit 10%, live exits.", "strike", "confirm", _limits(target_abs_delta=0.20, width=10.0)),
            Design("d16_w10_c20", "16 delta, $10 wide, minimum credit 20%, live exits.", "strike", "confirm", _limits(width=10.0, min_credit_pct=0.20)),
            Design("d16_pct2", "16 delta, width 2% of price snapped to the strike step, minimum credit 10%, live exits.", "strike", "confirm", _limits(width_pct=0.02)),
            Design("d20_pct1", "20 delta, width 1% of price, minimum credit 10%, live exits.", "strike", "confirm", _limits(target_abs_delta=0.20, width_pct=0.01)),
            Design("d16_iv50", "16 delta live book, sell only when the IV proxy percentile is at least 50.", "iv", "confirm", _limits(), iv_pct_min=0.50),
            Design("d16_iv70", "16 delta live book, IV proxy percentile at least 70.", "iv", "confirm", _limits(), iv_pct_min=0.70),
            Design("d16_iv_rv", "16 delta live book, sell only when the IV proxy is above 60-day realized vol.", "iv", "confirm", _limits(), iv_over_rv=True),
            Design("d16_iv50_rv", "16 delta live book, IV percentile at least 50 and IV proxy above 60-day realized.", "iv", "confirm", _limits(), iv_pct_min=0.50, iv_over_rv=True),
            Design("d16_vix50", "16 delta live book, VIX percentile at least 50 and VIX above SPY 20-day realized vol.", "iv", "confirm", _limits(), vix_pct_min=0.50, vix_rich=True),
            Design("d16_tp25", "16 delta, take-profit at 25% of credit, no price stop, structure break.", "management", "confirm", _limits(tp_frac=0.25)),
            Design("d16_tp35", "16 delta, take-profit at 35% of credit, no price stop, structure break.", "management", "confirm", _limits(tp_frac=0.35)),
            Design("d16_dte21", "16 delta, live exits plus a close when 21 calendar days remain.", "management", "confirm", _limits(close_dte=21)),
            Design("d16_spot_short", "16 delta, no price stop, close on a daily close through the short strike.", "management", "confirm", _limits(spot_stop="short")),
            Design("d16_spot_shelf", "16 delta, no price stop, close on a daily close through the HVN shelf.", "management", "confirm", _limits(spot_stop="shelf")),
            Design("d16_cat2", "16 delta, 2× credit catastrophe stop judged on the hourly close, structure break kept.", "management", "confirm", _limits(stop_check="close", stop_mult=2.0)),
            Design("d16_cat3", "16 delta, 3× credit catastrophe stop on the hourly close, structure break kept.", "management", "confirm", _limits(stop_check="close", stop_mult=3.0)),
            Design("d16_managed", "16 delta, 50% take-profit, close at 21 DTE, 2× close stop, daily close through the shelf.", "management", "confirm", _managed()),
            Design("d16_ema", "16 delta live exits, bull puts only above the daily EMA50 and bear calls only below it.", "structure", "confirm", _limits(), trend=True),
            Design("base_ema", "Live nearest-short book with the EMA50 trend filter and the earnings blackout.", "structure", "confirm", _limits(min_credit_pct=0.20, target_abs_delta=None, delta_tol=None), trend=True),
            Design("etf_d16", "16 delta live exits, index and ETF names only.", "structure", "confirm", _limits(), etf_only=True),
            Design("stock_d16", "16 delta live exits, single stocks only.", "structure", "confirm", _limits(), stock_only=True),
            Design("etf_base", "Live nearest-short book restricted to index and ETF names, with the earnings blackout.", "structure", "confirm", _limits(min_credit_pct=0.20, target_abs_delta=None, delta_tol=None), etf_only=True),
            Design("ext_d16", "Sell the 16-delta spread into an HVN shelf after a one-ATR extension, live exits.", "extension", "extension", _limits()),
            Design("ext_d16_managed", "Extension entry, 16 delta, managed exits (50%, 21 DTE, 2× close, shelf).", "extension", "extension", _managed()),
            Design("ext_d20_managed", "Extension entry, 20 delta, managed exits.", "extension", "extension", _managed(target_abs_delta=0.20)),
            Design("ext_d16_ema", "Extension entry, 16 delta, EMA50 trend filter, managed exits.", "extension", "extension", _managed(), trend=True),
            Design(
                "ext_d16_iv_ema",
                "Extension entry, 16 delta, IV percentile 50, IV above 60-day realized, EMA50, managed exits. All names.",
                "extension",
                "extension",
                _managed(),
                trend=True,
                iv_pct_min=0.50,
                iv_over_rv=True,
            ),
            Design(
                "ext_etf_h1",
                "ETFs only. Extension into the shelf, 16 delta, $5, 10% minimum credit, IV percentile 50, IV above 60-day realized, EMA50, managed exits.",
                "stack",
                "extension",
                _managed(),
                etf_only=True,
                trend=True,
                iv_pct_min=0.50,
                iv_over_rv=True,
            ),
            Design(
                "ext_etf_d20",
                "Same ETF extension stack at 20 delta.",
                "stack",
                "extension",
                _managed(target_abs_delta=0.20),
                etf_only=True,
                trend=True,
                iv_pct_min=0.50,
                iv_over_rv=True,
            ),
            Design(
                "ext_etf_d10_w10",
                "ETF extension stack at 10 delta and $10 wide.",
                "stack",
                "extension",
                _managed(target_abs_delta=0.10, width=10.0),
                etf_only=True,
                trend=True,
                iv_pct_min=0.50,
                iv_over_rv=True,
            ),
            Design(
                "ext_etf_d30",
                "ETF extension stack at 30 delta.",
                "stack",
                "extension",
                _managed(target_abs_delta=0.30),
                etf_only=True,
                trend=True,
                iv_pct_min=0.50,
                iv_over_rv=True,
            ),
            Design(
                "ext_etf_tp25",
                "ETF extension stack with the take-profit lowered to 25% of credit.",
                "stack",
                "extension",
                _managed(tp_frac=0.25),
                etf_only=True,
                trend=True,
                iv_pct_min=0.50,
                iv_over_rv=True,
            ),
            Design(
                "ext_etf_c20",
                "ETF extension stack with the minimum credit raised to 20% of width.",
                "stack",
                "extension",
                _managed(min_credit_pct=0.20),
                etf_only=True,
                trend=True,
                iv_pct_min=0.50,
                iv_over_rv=True,
            ),
            Design(
                "ext_etf_pct2",
                "ETF extension stack with the width set to 2% of price.",
                "stack",
                "extension",
                _managed(width_pct=0.02),
                etf_only=True,
                trend=True,
                iv_pct_min=0.50,
                iv_over_rv=True,
            ),
            Design(
                "ext_etf_cat3",
                "ETF extension stack with a 35% take-profit and a 3× catastrophe stop.",
                "stack",
                "extension",
                _managed(tp_frac=0.35, stop_mult=3.0),
                etf_only=True,
                trend=True,
                iv_pct_min=0.50,
                iv_over_rv=True,
            ),
            Design(
                "ext_stock_h1",
                "Single stocks only. Same extension stack as the ETF version.",
                "stack",
                "extension",
                _managed(),
                stock_only=True,
                trend=True,
                iv_pct_min=0.50,
                iv_over_rv=True,
            ),
            Design(
                "conf_etf_h4",
                "Confirm entry (not extension), ETFs, 16 delta, IV percentile 50, IV above realized, EMA50, managed exits.",
                "stack",
                "confirm",
                _managed(),
                etf_only=True,
                trend=True,
                iv_pct_min=0.50,
                iv_over_rv=True,
            ),
            Design(
                "tasty_etf",
                "ETFs, confirm entry, 16 delta, IV percentile 50, 50% take-profit, close at 21 DTE, 2× close stop, no underlying stop.",
                "stack",
                "confirm",
                _limits(close_dte=21, stop_check="close", stop_mult=2.0, spot_stop="none"),
                etf_only=True,
                iv_pct_min=0.50,
            ),
            Design(
                "tasty_all",
                "Same 16-delta managed premium sale on the full list, IV percentile 50, no trend filter, no underlying stop.",
                "stack",
                "confirm",
                _limits(close_dte=21, stop_check="close", stop_mult=2.0, spot_stop="none"),
                iv_pct_min=0.50,
            ),
            Design(
                "condor_etf_iv",
                "Iron condor on ETFs when price is mid-range and within one ATR of the EMA50. 16 delta each side, IV percentile 50, managed exits, shorts beyond the 20-day range.",
                "condor",
                "condor",
                _managed(),
                etf_only=True,
                iv_pct_min=0.50,
            ),
            Design(
                "condor_etf",
                "Same ETF iron condor with no IV filter.",
                "condor",
                "condor",
                _managed(),
                etf_only=True,
            ),
            Design(
                "condor_all_iv",
                "Iron condor on the full list, IV percentile 50, managed exits.",
                "condor",
                "condor",
                _managed(),
                iv_pct_min=0.50,
            ),
        ]
    )
    rows.extend(_oscillator_designs())
    return rows


def _oscillator_designs() -> list[Design]:
    """Train-only grid. MACD is 12/26/9 and the Stochastic RSI extremes stay 20/80.

    The searched settings are the bar the oscillator is read on, whether the
    histogram must rise for one bar or two, whether the daily MACD line has
    to agree, and whether %K must also cross %D. Each row is the same shelf
    entry as its control, plus that gate.
    """
    live = _limits(
        min_credit_pct=0.20,
        target_abs_delta=None,
        delta_tol=None,
        earnings_blackout_days=0,
    )
    hour = OscGate(frame="1h", hist_bars=1, daily_sign=False)
    hour_daily = OscGate(frame="1h", hist_bars=1, daily_sign=True)
    hour_two = OscGate(frame="1h", hist_bars=2, daily_sign=True)
    day = OscGate(frame="daily", hist_bars=1, daily_sign=True)
    cross = OscGate(frame="1h", hist_bars=1, daily_sign=True, kd_cross=True)
    specs = (
        (
            "base_osc",
            "baseline",
            live,
            "confirm",
            hour,
            "Live confirm book plus the hourly Stochastic RSI turn and a rising hourly MACD histogram. No daily MACD sign.",
        ),
        (
            "base_osc_d",
            "baseline",
            live,
            "confirm",
            hour_daily,
            "Live confirm book, hourly Stochastic RSI turn, hourly MACD histogram, and the daily MACD line on the same side.",
        ),
        (
            "base_osc_d2",
            "baseline",
            live,
            "confirm",
            hour_two,
            "Live confirm book, hourly Stochastic RSI turn, two hourly histogram steps, and the daily MACD line.",
        ),
        (
            "base_osc_day",
            "baseline",
            live,
            "confirm",
            day,
            "Live confirm book. The Stochastic RSI turn, the MACD histogram, and the MACD line are all read on the last completed daily bar.",
        ),
        (
            "d16_osc",
            "d16",
            _limits(),
            "confirm",
            hour,
            "16-delta confirm book plus the hourly Stochastic RSI turn and a rising hourly MACD histogram.",
        ),
        (
            "d16_osc_d",
            "d16",
            _limits(),
            "confirm",
            hour_daily,
            "16-delta confirm book, hourly Stochastic RSI turn, hourly MACD histogram, and the daily MACD line.",
        ),
        (
            "d16_osc_d2",
            "d16",
            _limits(),
            "confirm",
            hour_two,
            "16-delta confirm book, hourly Stochastic RSI turn, two hourly histogram steps, and the daily MACD line.",
        ),
        (
            "d16_osc_day",
            "d16",
            _limits(),
            "confirm",
            day,
            "16-delta confirm book with the Stochastic RSI turn, histogram, and MACD line all on the last completed daily bar.",
        ),
        (
            "d16_osc_x",
            "d16",
            _limits(),
            "confirm",
            cross,
            "16-delta confirm book. Hourly %K must cross %D while leaving 20 or 80, the hourly histogram must agree, and the daily MACD line must agree.",
        ),
        (
            "ext_osc",
            "ext_d16",
            _limits(),
            "extension",
            hour,
            "16-delta extension into the HVN shelf, plus the hourly Stochastic RSI turn and a rising hourly MACD histogram.",
        ),
        (
            "ext_osc_d",
            "ext_d16",
            _limits(),
            "extension",
            hour_daily,
            "16-delta extension into the shelf, hourly Stochastic RSI turn, hourly MACD histogram, and the daily MACD line.",
        ),
        (
            "ext_osc_day",
            "ext_d16",
            _limits(),
            "extension",
            day,
            "16-delta extension into the shelf. Stochastic RSI, histogram, and MACD line are read on the last completed daily bar.",
        ),
    )
    out = []
    for name, control, limits, entry, gate, blurb in specs:
        out.append(
            Design(
                name,
                blurb,
                "oscillator",
                entry,
                limits,
                osc=gate,
                control=control,
            )
        )
    return out


def allow_confirm(design: Design):
    def _allow(features) -> bool:
        if design.etf_only and features.symbol not in ETFS:
            return False
        if design.stock_only and features.symbol in ETFS:
            return False
        if design.trend and not features.ema_daily_ok:
            return False
        if design.iv_pct_min is not None and (features.iv_pct is None or features.iv_pct < design.iv_pct_min):
            return False
        if design.iv_over_rv and (features.iv is None or features.rv60 is None or not (features.iv > features.rv60)):
            return False
        if design.vix_pct_min is not None and (features.vix_pct is None or features.vix_pct < design.vix_pct_min):
            return False
        if design.vix_rich and (
            features.vix is None or features.spy_rv20 is None or features.vix / 100.0 <= features.spy_rv20
        ):
            return False
        if design.osc is not None and not gate_allows(design.osc, features.osc):
            return False
        return True

    return _allow


def to_alt(design: Design) -> AltSpec:
    return AltSpec(
        name=design.name,
        kind=design.entry,
        limits=design.limits,
        etf_only=design.etf_only,
        stock_only=design.stock_only,
        trend=design.trend,
        iv_pct_min=design.iv_pct_min,
        iv_over_rv=design.iv_over_rv,
        vix_pct_min=design.vix_pct_min,
        vix_rich=design.vix_rich,
        osc=design.osc,
    )


def in_dates(trades: Sequence[ReplayTrade], start: date, end: date) -> list[ReplayTrade]:
    return [t for t in trades if start <= as_et(t.entry_time).date() <= end]


def _interval(values: Sequence[float], *, n_boot: int = N_BOOT, seed: int = BOOT_SEED) -> Optional[Interval]:
    if not values:
        return None
    arr = np.asarray(values, dtype=float)
    point = float(arr.mean())
    if arr.size == 1:
        return Interval(point, point, point)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, arr.size, size=(n_boot, arr.size))
    means = np.sort(arr[idx].mean(axis=1))
    low = float(means[int(0.025 * n_boot)])
    high = float(means[min(n_boot - 1, int(0.975 * n_boot))])
    return Interval(point, low, high)


def summarize_fast(trades: Sequence[ReplayTrade], label: str, start: date, end: date) -> BookStats:
    weeks = week_span(start, end)
    pnls = [t.pnl for t in trades]
    rs = [t.r_multiple for t in trades]
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    flags = [1.0 if t.pnl > 0 else 0.0 for t in trades]
    counts: dict[str, int] = {}
    for trade in trades:
        counts[trade.exit_reason] = counts.get(trade.exit_reason, 0) + 1
    return BookStats(
        label=label,
        n=len(trades),
        wins=len(wins),
        losses=len(losses),
        open_mtm=sum(1 for t in trades if t.exit_reason == "open_mtm"),
        trades_per_week=(len(trades) / weeks) if weeks else 0.0,
        weeks=weeks,
        win_rate=_interval(flags),
        expectancy=_interval(pnls),
        expectancy_r=_interval(rs),
        avg_win=(sum(wins) / len(wins)) if wins else None,
        avg_loss=(sum(losses) / len(losses)) if losses else None,
        exit_counts=counts,
    )


def passes(stats: BookStats, min_n: int) -> bool:
    """True when the sample is large enough and both expectancy intervals sit above zero."""
    if stats.n < min_n or stats.expectancy is None or stats.expectancy_r is None:
        return False
    return stats.expectancy.low > 0 and stats.expectancy_r.low > 0


def select_name(rows: Sequence[tuple[str, BookStats]], min_n: int = MIN_TRAIN) -> Optional[str]:
    """Highest expectancy per unit of risk among rows that clear ``passes``."""
    best_name: Optional[str] = None
    best_point: Optional[float] = None
    for name, stats in rows:
        if not passes(stats, min_n) or stats.expectancy_r is None:
            continue
        if best_point is None or stats.expectancy_r.point > best_point:
            best_point = stats.expectancy_r.point
            best_name = name
    return best_name


def select_osc_name(rows: Sequence[tuple[str, BookStats]], min_n: int = MIN_OSC) -> Optional[str]:
    """Train pick for the oscillator grid. The interval does not have to clear zero.

    The setting with the highest expectancy per unit of risk wins. A tie goes
    to the larger sample, then to the name, so the pick does not depend on
    row order. Fewer than ``min_n`` trades is not a setting.
    """
    best_name: Optional[str] = None
    best_point: Optional[float] = None
    best_n = -1
    for name, stats in rows:
        if stats.n < min_n or stats.expectancy_r is None:
            continue
        point = stats.expectancy_r.point
        if (
            best_point is None
            or point > best_point
            or (point == best_point and stats.n > best_n)
            or (point == best_point and stats.n == best_n and best_name is not None and name < best_name)
        ):
            best_point = point
            best_n = stats.n
            best_name = name
    return best_name


def mean_diff_interval(
    left: Sequence[float],
    right: Sequence[float],
    *,
    n_boot: int = N_BOOT,
    seed: int = BOOT_SEED,
) -> Optional[Interval]:
    """Bootstrap of mean(left) − mean(right). The two samples are resampled independently."""
    if not left or not right:
        return None
    a = np.asarray(left, dtype=float)
    b = np.asarray(right, dtype=float)
    point = float(a.mean() - b.mean())
    if a.size == 1 and b.size == 1:
        return Interval(point, point, point)
    rng = np.random.default_rng(seed)
    ia = rng.integers(0, a.size, size=(n_boot, a.size))
    ib = rng.integers(0, b.size, size=(n_boot, b.size))
    diffs = np.sort(a[ia].mean(axis=1) - b[ib].mean(axis=1))
    low = float(diffs[int(0.025 * n_boot)])
    high = float(diffs[min(n_boot - 1, int(0.975 * n_boot))])
    return Interval(point, low, high)


def oscillator_verdict(
    name: Optional[str],
    test_n: int,
    diff_usd: Optional[Interval],
    diff_r: Optional[Interval],
    test_stats: Optional[BookStats],
) -> str:
    """Whether the train-chosen oscillator adds value beyond noise on the test window.

    Written before the replay. Adding value means the test difference versus
    the matched control, in dollars and as a share of max risk, has a 95%
    interval entirely above zero, on at least 30 test trades. A positive
    difference that leaves the book itself losing is not a reason to unpause.
    """
    if name is None:
        return (
            "No oscillator setting produced 30 train trades. The signal is too rare "
            "to pick. It does not add a usable edge."
        )
    if test_n < MIN_TEST or diff_usd is None or diff_r is None:
        return (
            f"Train picked `{name}`. The test sample is {test_n} trades, under {MIN_TEST}, "
            "so the out-of-sample interval is not a claim. MACD and Stochastic RSI do not "
            "clear sampling noise."
        )
    usd_up = diff_usd.low > 0
    r_up = diff_r.low > 0
    usd_down = diff_usd.high < 0
    r_down = diff_r.high < 0
    if usd_down and r_down:
        return (
            f"`{name}` was the train setting. On the untouched test window it is worse than "
            "its matched shelf entry, beyond sampling noise, in dollars and as a share of max risk. "
            "MACD and Stochastic RSI do not add value."
        )
    if not (usd_up and r_up):
        return (
            f"`{name}` was the train setting. On the untouched test window the difference versus "
            "its matched shelf entry has a 95% interval that includes zero, or the two units disagree. "
            "MACD and Stochastic RSI do not add value beyond sampling noise."
        )
    book_positive = (
        test_stats is not None
        and test_stats.expectancy is not None
        and test_stats.expectancy_r is not None
        and test_stats.expectancy.low > 0
        and test_stats.expectancy_r.low > 0
    )
    if book_positive:
        return (
            f"`{name}` improves its matched shelf entry beyond sampling noise, and the filtered "
            "book itself has a positive test expectancy beyond noise."
        )
    return (
        f"`{name}` improves its matched shelf entry beyond sampling noise, but the filtered book "
        "is still not a positive-expectancy sleeve beyond noise. Do not unpause on this filter."
    )


def _oscillator_section(osc_rows, grouped, train, test, recent, long) -> tuple[list[str], str]:
    """Train grid, the one train-chosen setting, and its out-of-sample difference."""
    del grouped  # trades already live on the window dicts
    lines = [
        "Stochastic RSI is Wilder RSI(14), then a 14-period stochastic of that RSI, "
        "smoothed with a 3-period average (%K) and a 3-period average of %K (%D). "
        "A bull put needs %K to turn up from below 20. A bear call needs %K to turn down from above 80. "
        "MACD is 12/26/9. The histogram has to move with the turn: up for a bull put, down for a bear call. "
        "Daily trend context is the sign of the daily MACD line, positive for a bull put and negative for a bear call. "
        "A missing reading fails closed.",
        "",
        "The volume-profile shelf entry is unchanged. The oscillator is an extra gate on that same bar. "
        "Hourly readings use the closed timing bar. Daily readings use the last session whose 16:00 close "
        "is already known. The indicators are causal, so a later bar does not move an earlier reading.",
        "",
        "Locked, and not searched: the 14/14/3/3 Stochastic RSI, the 20 and 80 extremes, MACD 12/26/9, "
        "and the requirement that the turn and the histogram agree. Searched on the train window only: "
        "hourly versus daily frame, one histogram step versus two, daily MACD sign on or off, and a stricter "
        "%K cross of %D. The pick is the highest train expectancy per unit of risk among oscillator rows "
        f"with at least {MIN_OSC} trades. Fills in this section are the same natural bid/ask as the rest of the study.",
        "",
        "### Train grid",
        "",
        TABLE_HEADER,
    ]
    for row in osc_rows:
        stats, chosen = train[row.name]
        lines.append(stats_line(row.name, stats, chosen))
    picked = select_osc_name([(row.name, train[row.name][0]) for row in osc_rows])
    by_name = {row.name: row for row in osc_rows}
    if picked is None or by_name[picked].control is None:
        verdict = oscillator_verdict(None, 0, None, None, None)
        lines.extend(["", verdict])
        return lines, verdict
    control = by_name[picked].control
    tw = train[picked][0]
    lines.extend(
        [
            "",
            f"Train pick: `{picked}`, matched to `{control}`. "
            f"Train expectancy {_fmt(tw.expectancy, 'usd')}, {_fmt(tw.expectancy_r, 'r')}, {tw.n} trades. "
            f"{by_name[picked].blurb}",
            "",
            "### Selected setting and its control",
            "",
            "Train is how the setting was chosen. Test is the out-of-sample look. "
            "Recent is Jul 6–Sep 25 2026. Long includes the train window.",
            "",
            TABLE_HEADER,
        ]
    )
    buckets = (
        ("train", train),
        ("test", test),
        ("recent", recent),
        ("long", long),
    )
    for label, bucket in buckets:
        for name in (picked, control):
            stats, chosen = bucket[name]
            lines.append(stats_line(f"{name} {label}", stats, chosen))
    lines.extend(
        [
            "",
            "Difference is the oscillator mean minus the control mean. The two trade lists are resampled "
            "independently, 5,000 draws, seed 20260925. An interval entirely above zero is the claim that "
            "the filter adds value beyond sampling noise.",
            "",
            "| Window | Oscillator trades | Control trades | Expectancy $ difference (95% CI) | Expectancy / max risk difference (95% CI) |",
            "| --- | ---: | ---: | --- | --- |",
        ]
    )
    test_usd = None
    test_r = None
    for label, bucket in buckets:
        osc_trades = bucket[picked][1]
        control_trades = bucket[control][1]
        usd = mean_diff_interval([t.pnl for t in osc_trades], [t.pnl for t in control_trades])
        rmul = mean_diff_interval([t.r_multiple for t in osc_trades], [t.r_multiple for t in control_trades])
        if label == "test":
            test_usd = usd
            test_r = rmul
        lines.append(
            f"| {label} | {len(osc_trades)} | {len(control_trades)} | {_fmt(usd, 'usd')} | {_fmt(rmul, 'r')} |"
        )
    verdict = oscillator_verdict(picked, test[picked][0].n, test_usd, test_r, test[picked][0])
    lines.extend(
        [
            "",
            verdict,
            "",
            "The other oscillator settings' test numbers sit in the appendix with the rest of the search. "
            "They were not used to choose the setting.",
        ]
    )
    return lines, verdict


def _usd(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):.0f}"


def _money(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return _usd(value)


def _fmt(interval: Optional[Interval], kind: str) -> str:
    if interval is None:
        return "n/a"
    if kind == "pct":
        return f"{100 * interval.point:.1f}% [{100 * interval.low:.1f}%, {100 * interval.high:.1f}%]"
    if kind == "usd":
        return f"{_usd(interval.point)} [{_usd(interval.low)}, {_usd(interval.high)}]"
    return f"{100 * interval.point:.1f}% [{100 * interval.low:.1f}%, {100 * interval.high:.1f}%]"


def _mean_delta(trades: Sequence[ReplayTrade]) -> Optional[float]:
    vals = [abs(t.short_delta) for t in trades if t.short_delta]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _dd_cell(trades: Sequence[ReplayTrade]) -> str:
    if not trades:
        return "n/a"
    dollars, frac = max_drawdown(trades)
    return f"{_usd(dollars)} ({100 * frac:.1f}%)"


def stats_line(name: str, stats: BookStats, trades: Sequence[ReplayTrade]) -> str:
    delta = _mean_delta(trades)
    delta_s = "n/a" if delta is None else f"{delta:.2f}"
    return (
        f"| {name} | {stats.n} | {stats.trades_per_week:.2f} "
        f"| {_fmt(stats.win_rate, 'pct')} | {_fmt(stats.expectancy, 'usd')} "
        f"| {_fmt(stats.expectancy_r, 'r')} | {_money(stats.avg_win)} | {_money(stats.avg_loss)} "
        f"| {_dd_cell(trades)} | {delta_s} |"
    )


TABLE_HEADER = (
    "| Design | Trades | Trades/week | Win rate (95% CI) | Expectancy $ (95% CI) | "
    "Expectancy / max risk (95% CI) | Avg win | Avg loss | Max drawdown | Avg |delta| |\n"
    "| --- | ---: | ---: | --- | --- | --- | ---: | ---: | --- | ---: |"
)


def load_earnings(path: Path) -> dict[str, list[date]]:
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, list[date]] = {}
    for symbol, days in raw.items():
        out[symbol] = [date.fromisoformat(day) for day in days]
    return out


def run_confirm(bars, symbols, rows: Sequence[Design], regime, earnings, structure) -> dict[str, list[ReplayTrade]]:
    if not rows:
        return {}
    variants = {row.name: allow_confirm(row) for row in rows}
    limits_by = {row.name: row.limits for row in rows}
    print(f"confirm replay, {len(rows)} designs", flush=True)
    grouped, diags = replay_universe(
        bars,
        symbols,
        variants,
        timing_key="1h",
        minutes=60,
        limits=rows[0].limits,
        structure=structure,
        limits_by_variant=limits_by,
        regime_by_symbol=regime,
        earnings_by_symbol=earnings,
    )
    for name, diag in diags.items():
        print(
            f"  {name}: opened {diag.opened} ready {diag.ready} "
            f"filter {diag.filter_reject} credit {diag.credit_skip}",
            flush=True,
        )
    return grouped


def run_alt(bars, symbols, rows: Sequence[Design], regime, earnings, structure) -> dict[str, list[ReplayTrade]]:
    if not rows:
        return {}
    print(f"alt replay, {len(rows)} designs", flush=True)
    grouped, diags = replay_alt_universe(
        bars,
        symbols,
        [to_alt(row) for row in rows],
        regime=regime,
        earnings=earnings,
        etfs=set(ETFS),
        structure=structure,
    )
    for name, diag in diags.items():
        print(
            f"  {name}: opened {diag.opened} ready {diag.ready} "
            f"filter {diag.filter_reject} credit {diag.credit_skip}",
            flush=True,
        )
    return grouped


def _book(trades, label, start, end) -> tuple[BookStats, list[ReplayTrade]]:
    chosen = in_dates(trades, start, end)
    return summarize_fast(chosen, label, start, end), chosen


def _capped(trades, start, end, cap: int) -> tuple[BookStats, list[ReplayTrade]]:
    chosen = select_risk_book(
        in_dates(trades, start, end),
        max_concurrent=cap,
        equity=100_000.0,
        risk_pct=0.005,
        max_portfolio_risk_pct=0.10,
    )
    return summarize_fast(chosen, "capped", start, end), chosen


def _positive_point(stats: BookStats) -> bool:
    return stats.expectancy_r is not None and stats.expectancy_r.point > 0 and stats.expectancy is not None and stats.expectancy.point > 0


def subfold_ok(trades: Sequence[ReplayTrade]) -> tuple[bool, str]:
    """Both halves of the test window must not be a loss when they have enough trades."""
    halves = (
        ("2025-07-01 to 2025-12-31", TEST_START, H2_2025_END),
        ("2026-01-02 to 2026-09-25", Y2026_START, TEST_END),
    )
    notes = []
    ok = True
    for label, start, end in halves:
        stats, _ = _book(trades, label, start, end)
        if stats.expectancy_r is None:
            notes.append(f"{label}: no trades")
            if stats.n >= 10:
                ok = False
            continue
        notes.append(
            f"{label}: {stats.n} trades, expectancy/risk {_fmt(stats.expectancy_r, 'r')}"
        )
        if stats.n >= MIN_FOLD and stats.expectancy_r.point <= 0:
            ok = False
        elif stats.n >= 10 and stats.expectancy_r.point <= 0:
            ok = False
    return ok, "; ".join(notes)


def recent_veto(stats: BookStats) -> bool:
    if stats.n >= MIN_FOLD and stats.expectancy_r is not None and stats.expectancy_r.high < 0:
        return True
    return False


def decide(
    name: Optional[str],
    test: BookStats,
    recent: BookStats,
    capped: BookStats,
    fold_ok: bool,
) -> tuple[bool, str]:
    if name is None:
        return False, "No design cleared the train window."
    if not passes(test, MIN_TEST):
        return False, (
            f"{name} was the train selection. On 2025-07-01 through 2026-09-25 its "
            "expectancy interval does not sit entirely above zero, or the sample is under 30 trades."
        )
    if not fold_ok:
        return False, (
            f"{name} is positive on the combined test window, but one half of that window "
            "(2025 H2 or 2026) has a non-positive point estimate."
        )
    if recent_veto(recent):
        return False, (
            f"{name} is vetoed by Jul 6–Sep 25 2026: that slice has at least {MIN_FOLD} trades "
            "and its expectancy interval sits entirely below zero."
        )
    if capped.expectancy is None or capped.expectancy.point <= 0:
        return False, (
            f"{name} is positive per spread, but the 5-spread book (0.5% risk, 10% open risk) "
            "does not have a positive dollar expectancy on the test window."
        )
    return True, f"{name} is positive out of sample beyond sampling noise, including the 5-spread book."


def _blurb(rows: Sequence[Design], name: str) -> str:
    for row in rows:
        if row.name == name:
            return row.blurb
    return name


def _section(title: str, lines: list[str]) -> str:
    return "\n".join([f"## {title}", "", *lines, ""])


def render(ctx: dict) -> str:
    adopted = ctx["adopted"]
    winner = ctx["winner"]
    reason = ctx["reason"]
    if adopted and winner:
        lead = (
            f"Adopt `{winner}`. {reason} "
            "The tracked default and the untracked paper file should both use the values in the decision section. "
            "The hard locks stay: options-native software exits, atomic 2-leg opens and closes, paper only, "
            "at most 5 spreads, 0.5% of equity at risk per spread, 10% open risk."
        )
    else:
        lead = (
            "Keep the bot paused. No precommitted design has a positive expectancy out of sample "
            "beyond sampling noise. "
            f"{reason} "
            "The live book is still the PR #10 sleeve with the price stop off, and that sleeve loses money. "
            "Appendix rows that look less bad on the test window were not the selection. "
            f"{ctx['osc_verdict']}"
        )
    parts = [
        "# Strategy redesign",
        "",
        lead,
        "",
        "## How a design was allowed to win",
        "",
        "Train is 2024-01-02 through 2025-06-30. Test is 2025-07-01 through 2026-09-25. "
        "The recent window is 2026-07-06 through 2026-09-25. The long window is 2024-01-02 through 2026-09-25 "
        "and is reported for continuity with the earlier study. It includes the train data, so it is not the adoption test.",
        "",
        f"A train row needs at least {MIN_TRAIN} trades. Both the dollar expectancy and the expectancy divided by "
        "that spread's max loss need a 95% bootstrap interval entirely above zero. The winner is the eligible row "
        "with the highest expectancy per unit of risk. That one row then has to clear the same bar on the test "
        f"window with at least {MIN_TEST} trades, show a positive point estimate on 2025 H2 and on 2026 when those "
        f"slices have at least 10 trades, not have a Jul–Sep 2026 interval entirely below zero when that slice has "
        f"at least {MIN_FOLD} trades, and show a positive dollar point estimate on the 5-spread book.",
        "",
        f"There are {ctx['n_search']} searchable designs. The train intervals are not adjusted for that search. "
        "The test interval is one look at the precommitted winner. A different design that looks good only on the "
        "test window is listed in the appendix and is not implemented.",
        "",
        "MACD and Stochastic RSI are in that same search, so a row that clears the bar can still be the winner. "
        "Separately, the oscillator settings are ranked on the train window by expectancy per unit of risk among "
        f"rows with at least {MIN_OSC} trades. That one setting is then compared with its matched shelf entry on "
        "the test window. Adding value means both the dollar difference and the difference as a share of max risk "
        "have a 95% interval entirely above zero, on at least 30 test trades. The train intervals for that grid "
        "are not adjusted for the number of settings.",
        "",
        "## Pricing",
        "",
        "Same model as `docs/win-rate-study.md`. There is no stored option tape. Alpaca historical option quotes "
        "were not reachable: `OPTIONS_APCA_API_KEY_ID` and `OPTIONS_APCA_API_SECRET_KEY` are unset, and "
        "`config/paper-live.yaml` is not in the repo. Each vertical is Black-Scholes on the underlying bars.",
        "",
        "The IV used to price a spread is the last 20 sessions of close-to-close realized vol times 1.15, frozen "
        "at entry, clamped between 15% and 125%. Rates are 4% with no dividend. Because that IV is defined as a "
        "markup on 20-day realized vol, comparing it with the same 20-day realized vol is not a filter. The IV "
        "filter compares it with 60-day realized vol, and the IV percentile compares it with the prior 252 readings "
        "of itself. VIX is the Yahoo `^VIX` close. VIX richness means the VIX percentile is high and VIX/100 is "
        "above SPY's 20-day realized vol. That is a market-regime filter, not a listed implied vol for the single name.",
        "",
        "Each leg's half-spread is 6% of its mid, at least $0.05 and at most $0.25. The entry credit in the primary "
        "book is the natural credit: short bid minus long ask. Take-profit and the credit stop are judged on the "
        "mid. A take-profit fills at a debit of (1 − fraction) × credit. A stop that gaps through the open fills at "
        "the open's natural debit. A stop that trades through fills at the multiple times the credit plus the "
        "bid/ask, and no better than that bar's worst debit. A close through structure, the short, or the shelf "
        "pays the natural debit at that daily close. Expiration settles at intrinsic. A position still open on the "
        "last bar is marked at the natural debit and kept in the averages.",
        "",
        "The mid-fill sensitivity books the spread mid on entry and the mid on every debit, so it pays no bid/ask. "
        "The nickel sensitivity takes the natural credit minus $0.05 and adds $0.05 to every debit, including the "
        "formula take-profit fill. Stops are judged on the mid in every sensitivity. The primary tables are natural fills.",
        "",
        "A close through the short strike or the HVN shelf is checked before take-profit, because it is a stop. "
        "The legacy structure-break (a daily close through swing invalidation) is still checked after take-profit, "
        "which is the order in the earlier replay. The live engine checks structure break before the mark. "
        "That difference is unchanged from PR #10 for the baseline.",
        "",
        "Strike grid: $1 on the ETFs in the full-A list; stocks use $0.50 under $50, $1 under $200, $2.50 under $500, "
        "and $5 above that. A delta short has to clear the anchor by the $1 gap. If the closest listed delta is more "
        "than 0.08 closer to the money than the target, the entry is skipped. A strike further out than the target "
        "is kept, because the shelf is what protects the short. The anchor is swing invalidation on a confirm entry, the shelf edge "
        "on an extension, and the prior 20-day high or low on a condor. Earnings dates are the Nasdaq announcement "
        "calendar. Five of 771 weekday requests failed, so an announcement that fell on one of those days is not "
        "blacked out. ETFs had no earnings rows. The blackout is three calendar days around the entry and around "
        "expiration, which is the live `earnings_blackout_days` rule. The baseline row leaves the calendar empty so "
        "it can be compared with PR #10.",
        "",
        "Sizing in the per-spread tables is 0.5% of a fixed $100,000, one spread per name. Max loss is "
        "(width − credit) × 100 × contracts. An iron condor is one slot: the credit is the sum of the two sides, "
        "and the max loss is one width minus that combined credit. The 5-spread book then applies the 10% open-risk "
        "cap on realized equity. Intervals are 5,000-draw percentile bootstraps from a NumPy generator seeded at "
        "20260925. Max drawdown is the peak-to-trough of cumulative dollars ordered by exit time, starting from "
        "$100,000. It is a path fact, not a bootstrap interval.",
        "",
    ]
    parts.append(_section("Train window (2024-01-02 to 2025-06-30)", [TABLE_HEADER, *ctx["train_lines"]]))
    parts.append("### What each train row is\n")
    for design in ctx["search"]:
        parts.append(f"- `{design.name}` — {design.blurb}")
    parts.append("")
    parts.append(_section("Selection", ctx["selection_lines"]))
    parts.append(_section("Out-of-sample test (2025-07-01 to 2026-09-25)", [TABLE_HEADER, *ctx["test_lines"]]))
    parts.append(_section("Walk-forward", ctx["fold_lines"]))
    parts.append(_section("Recent window (2026-07-06 to 2026-09-25)", [TABLE_HEADER, *ctx["recent_lines"]]))
    parts.append(_section("Long window (2024-01-02 to 2026-09-25)", [TABLE_HEADER, *ctx["long_lines"]]))
    parts.append(_section("Five-spread book", ctx["cap_lines"]))
    parts.append(_section("SPY buy and hold", ctx["spy_lines"]))
    parts.append(_section("Fill sensitivity", [TABLE_HEADER, *ctx["fill_lines"]]))
    parts.append(_section("Exit mix for the baseline and the train selection", ctx["exit_lines"]))
    parts.append(_section("MACD and Stochastic RSI", ctx["osc_lines"]))
    parts.append(_section("Appendix: test window for every searchable design", [
        "These numbers were not used to pick the winner. A row whose test interval sits above zero, "
        "and that was not the train selection, is not a candidate to ship.",
        "",
        TABLE_HEADER,
        *ctx["appendix_lines"],
    ]))
    parts.append(_section("Decision", ctx["decision_lines"]))
    parts.append("## Reproduce\n\n```bash\nPYTHONPATH=src python3 -m alpaca_options_credit.replay.redesign --cache var/replay-cache --out docs/strategy-redesign.md\n```\n\n")
    parts.append(
        "The Yahoo cache, the VIX cache, and the Nasdaq earnings cache live under `var/replay-cache` (gitignored). "
        "A warm cache does not hit the network. Bootstrap seed is 20260925, 5,000 resamples.\n"
    )
    return "\n".join(parts)


def _paper_block(design: Design) -> list[str]:
    limits = design.limits
    credit_stop = limits.stop_check != "none"
    lines = [
        "Copy this into `config/paper-live.yaml` on the machine that places paper orders. "
        "The file is not in the repo.",
        "",
        "```yaml",
        "bot:",
        "  dry_run: false   # paper account only; broker.paper_only stays true",
        "universe:",
        f"  active: {'etf' if design.etf_only else 'stocks' if design.stock_only else 'full_a'}",
        "spreads:",
        f"  width: {limits.width:.2f}",
        f"  width_pct: {limits.width_pct if limits.width_pct is not None else 'null'}",
        f"  dte_min: {limits.dte_min}",
        f"  dte_max: {limits.dte_max}",
        f"  min_credit_pct_of_width: {limits.min_credit_pct:.2f}",
        f"  target_abs_delta: {limits.target_abs_delta if limits.target_abs_delta is not None else 'null'}",
        f"  delta_tolerance: {limits.delta_tol if limits.delta_tol is not None else 'null'}",
        f"  min_short_inv_gap: {limits.min_short_inv_gap:.1f}",
        "  credit_from: natural",
        "exits:",
        "  path: credit_mark_and_structure",
        "  forbid_equity_oco_bracket: true",
        "  never_cancel_working_close: true",
        "  atomic_spread_only: true",
        f"  take_profit_frac_of_credit: {limits.tp_frac:.2f}",
        f"  credit_stop: {'true' if credit_stop else 'false'}",
        f"  stop_multiple_of_credit: {limits.stop_mult:.1f}",
        f"  stop_on_close_only: {'true' if limits.stop_check == 'close' else 'false'}",
        f"  close_at_dte: {limits.close_dte if limits.close_dte is not None else 'null'}",
        f"  underlying_stop: {limits.spot_stop}",
        "  honor_structure_break: true",
        "entry:",
        f"  mode: {design.entry}",
        f"  trend_ema50: {'true' if design.trend else 'false'}",
        f"  iv_percentile_min: {design.iv_pct_min if design.iv_pct_min is not None else 'null'}",
        f"  iv_above_rv60: {'true' if design.iv_over_rv else 'false'}",
        f"  vix_percentile_min: {design.vix_pct_min if design.vix_pct_min is not None else 'null'}",
        f"  vix_above_spy_rv: {'true' if design.vix_rich else 'false'}",
        "risk:",
        "  risk_per_trade_pct: 0.005",
        "  max_concurrent: 5",
        "  max_portfolio_risk_pct: 0.10",
        "  one_spread_per_underlying: true",
        "calendar:",
        "  skip_earnings: true",
        "  earnings_blackout_days: 3",
        "broker:",
        "  paper_only: true",
        "  order_class: mleg",
        "```",
        "",
        f"Rule: {design.blurb}",
    ]
    return lines


def build_report(bars, cache: Path, cfg: dict) -> str:
    symbols = list((cfg.get("universe") or {}).get("symbols") or [])
    structure = structure_from_config(cfg)
    vix = load_bars(cache / "VIX_1d.json") if (cache / "VIX_1d.json").is_file() else []
    print(f"building regime, vix bars {len(vix)}", flush=True)
    daily = {symbol: (bars.get(symbol) or {}).get("1d") or [] for symbol in symbols}
    regime = build_regime(daily, vix) if vix else {}
    earnings = load_earnings(cache / "earnings.json")
    print(f"earnings names {len(earnings)}", flush=True)
    rows = designs()
    search = [row for row in rows if row.searchable]
    confirm = [row for row in search if row.entry == "confirm"]
    alt = [row for row in search if row.entry != "confirm"]
    grouped = run_confirm(bars, symbols, confirm, regime, earnings, structure)
    grouped.update(run_alt(bars, symbols, alt, regime, earnings, structure))

    def pack(name: str, start: date, end: date):
        return _book(grouped.get(name) or [], name, start, end)

    train = {row.name: pack(row.name, TRAIN_START, TRAIN_END) for row in search}
    test = {row.name: pack(row.name, TEST_START, TEST_END) for row in search}
    recent = {row.name: pack(row.name, RECENT_START, RECENT_END) for row in search}
    long = {row.name: pack(row.name, LONG_START, LONG_END) for row in search}
    winner = select_name([(row.name, train[row.name][0]) for row in search])
    fold_notes = ["Each fold picks from trades entered in its train window, then the next window is the out-of-sample slice of that same design. The design was simulated as if it ran the whole tape, so a position opened before the fold can still be busy."]
    fold_notes.append("")
    fold_notes.append("| Fold train end | Selected | OOS trades | OOS expectancy $ | OOS expectancy / max risk |")
    fold_notes.append("| --- | --- | ---: | --- | --- |")
    for train_s, train_e, test_s, test_e in FOLDS:
        fold_rows = []
        for row in search:
            stats, _ = _book(grouped.get(row.name) or [], row.name, train_s, train_e)
            fold_rows.append((row.name, stats))
        picked = select_name(fold_rows)
        if picked is None:
            fold_notes.append(f"| {train_e.isoformat()} | none | 0 | n/a | n/a |")
            continue
        oos, _ = _book(grouped.get(picked) or [], picked, test_s, test_e)
        fold_notes.append(
            f"| {train_e.isoformat()} | {picked} | {oos.n} | {_fmt(oos.expectancy, 'usd')} | {_fmt(oos.expectancy_r, 'r')} |"
        )
    fold_ok = True
    fold_detail = "No winner, so the halves were not a veto."
    if winner:
        fold_ok, fold_detail = subfold_ok(grouped.get(winner) or [])
        fold_notes.append("")
        fold_notes.append(f"Halves of the test window for `{winner}`: {fold_detail}.")

    test_stats, test_trades = test[winner] if winner else (summarize_fast([], "none", TEST_START, TEST_END), [])
    recent_stats = recent[winner][0] if winner else summarize_fast([], "none", RECENT_START, RECENT_END)
    cap_stats, cap_trades = _capped(grouped.get(winner) or [], TEST_START, TEST_END, 5) if winner else (
        summarize_fast([], "capped", TEST_START, TEST_END),
        [],
    )
    adopted, reason = decide(winner, test_stats, recent_stats, cap_stats, fold_ok)

    # Fill sensitivity. Re-simulate the baseline and the train selection.
    sens_names = ["baseline"]
    if winner and winner != "baseline":
        sens_names.append(winner)
    by_name = {row.name: row for row in search}
    sens_rows: list[Design] = []
    for name in sens_names:
        src = by_name[name]
        for mode, label in (("mid", "mid"), ("nickel", "nickel")):
            sens_rows.append(
                replace(
                    src,
                    name=f"{name}_{label}",
                    blurb=f"{src.blurb} Fills at the {label} assumption.",
                    limits=replace(src.limits, fill_mode=mode),
                    searchable=False,
                )
            )
    sens_confirm = [row for row in sens_rows if row.entry == "confirm"]
    sens_alt = [row for row in sens_rows if row.entry != "confirm"]
    sens_grouped = run_confirm(bars, symbols, sens_confirm, regime, earnings, structure)
    sens_grouped.update(run_alt(bars, symbols, sens_alt, regime, earnings, structure))

    train_lines = []
    for row in search:
        stats, chosen = train[row.name]
        train_lines.append(stats_line(row.name, stats, chosen))
    show = ["baseline"] + ([winner] if winner and winner != "baseline" else [])
    # Also show every train passer on the test table.
    passers = [row.name for row in search if passes(train[row.name][0], MIN_TRAIN)]
    for name in passers:
        if name not in show:
            show.append(name)
    test_lines = [stats_line(name, test[name][0], test[name][1]) for name in show]
    recent_lines = [stats_line(name, recent[name][0], recent[name][1]) for name in show]
    long_lines = [stats_line(name, long[name][0], long[name][1]) for name in show]
    appendix = [stats_line(row.name, test[row.name][0], test[row.name][1]) for row in search]

    cap_lines = [
        "Same signals, then a greedy book: at most 5 names, 0.5% of realized equity, 10% open risk. "
        "A skipped signal does not invent a later replacement.",
        "",
        TABLE_HEADER,
    ]
    for name in show:
        cstats, ctrades = _capped(grouped.get(name) or [], TEST_START, TEST_END, 5)
        cap_lines.append(stats_line(f"{name} test, max 5", cstats, ctrades))
        cstats, ctrades = _capped(grouped.get(name) or [], LONG_START, LONG_END, 5)
        cap_lines.append(stats_line(f"{name} long, max 5", cstats, ctrades))

    spy = (bars.get("SPY") or {}).get("1d") or []
    spy_lines = [
        "SPY buy-and-hold is the close on the first session of the window to the close on the last, "
        "on $100,000. Max drawdown is the peak-to-trough of the daily closes. It is not a credit-spread expectancy.",
        "",
        "| Window | SPY return | $100k P&L | Max drawdown |",
        "| --- | ---: | ---: | ---: |",
    ]
    for label, start, end in (
        ("Train 2024-01-02 to 2025-06-30", TRAIN_START, TRAIN_END),
        ("Test 2025-07-01 to 2026-09-25", TEST_START, TEST_END),
        ("Recent 2026-07-06 to 2026-09-25", RECENT_START, RECENT_END),
        ("Long 2024-01-02 to 2026-09-25", LONG_START, LONG_END),
    ):
        held = spy_buy_hold(spy, start, end)
        if held is None:
            spy_lines.append(f"| {label} | n/a | n/a | n/a |")
        else:
            spy_lines.append(
                f"| {label} | {100 * held['return']:.1f}% | {_usd(held['pnl'])} | {100 * held['max_dd_frac']:.1f}% |"
            )

    fill_lines = []
    for name in sens_names:
        stats, chosen = train[name] if name in train else _book(grouped.get(name) or [], name, TRAIN_START, TRAIN_END)
        fill_lines.append(stats_line(f"{name} natural, train", stats, chosen))
        stats, chosen = test[name]
        fill_lines.append(stats_line(f"{name} natural, test", stats, chosen))
        for mode in ("mid", "nickel"):
            key = f"{name}_{mode}"
            stats, chosen = _book(sens_grouped.get(key) or [], key, TRAIN_START, TRAIN_END)
            fill_lines.append(stats_line(f"{key}, train", stats, chosen))
            stats, chosen = _book(sens_grouped.get(key) or [], key, TEST_START, TEST_END)
            fill_lines.append(stats_line(f"{key}, test", stats, chosen))

    exit_lines = [
        "| Design | Window | Trades | Take-profit | Credit stop | Structure break | Underlying stop | 21 DTE | Expiration | Open mark |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in show:
        for label, bucket in (("train", train), ("test", test), ("long", long)):
            stats = bucket[name][0]
            counts = stats.exit_counts
            def n(key: str) -> int:
                return int(counts.get(key, 0))
            exit_lines.append(
                f"| {name} | {label} | {stats.n} | {n('take_profit')} | {n('stop_credit')} "
                f"| {n('structure_break')} | {n('underlying_stop')} | {n('dte_exit')} "
                f"| {n('expiration')} | {n('open_mtm')} |"
            )

    selection = []
    if winner is None:
        selection.append(
            f"No searchable design had {MIN_TRAIN} or more train trades and both expectancy intervals entirely above zero."
        )
    else:
        tw = train[winner][0]
        selection.append(
            f"Train selection: `{winner}`. {_blurb(search, winner)} "
            f"Train expectancy {_fmt(tw.expectancy, 'usd')}, {_fmt(tw.expectancy_r, 'r')}, {tw.n} trades."
        )
    if passers:
        selection.append("")
        selection.append("Train rows that cleared the interval bar: " + ", ".join(f"`{n}`" for n in passers) + ".")
    else:
        selection.append("")
        selection.append("No train row cleared the interval bar.")
    selection.append("")
    selection.append(reason)

    decision = [reason, ""]
    if adopted and winner:
        decision.extend(_paper_block(by_name[winner]))
        decision.append("")
        decision.append(
            "The tracked `config/default.yaml` is updated to the same rules. "
            "`risk.max_concurrent` is 5. Paper only, atomic 2-leg mleg, no equity stops."
        )
    else:
        decision.append(
            "No change to `config/default.yaml`. Leave `exits.credit_stop: false` as PR #10 set it, and do not run the sleeve. "
            "`config/paper-live.yaml` is not in this repo. If a copy is still placing orders, stop it. "
            "The hard locks stay in the code either way: options-native exits, atomic 2-leg opens and closes, "
            "paper only, and the risk caps the operator asked to keep (0.5% per spread, 10% open, and a 5-spread book "
            "on the machine that trades — the tracked default still says 20 concurrent, which is the dry-run list size, "
            "not a reason to keep trading)."
        )
    osc_rows = [row for row in search if row.family == "oscillator"]
    osc_lines, osc_verdict = _oscillator_section(osc_rows, grouped, train, test, recent, long)
    decision.append("")
    decision.append(osc_verdict)
    decision.append("")
    decision.append(
        f"Plain English: selling the near-the-money credit the 20% width rule demands, into a breakout retest, "
        "did not become a winner by moving the short to a listed delta, by waiting for a high vol-proxy rank, "
        "by closing at 21 DTE, by stopping on the shelf, by switching to condors and ETFs, or by requiring "
        "a Stochastic RSI turn and a MACD histogram at the shelf. "
        + (
            f"`{winner}` cleared the train and the untouched test, so that is the book to paper-trade."
            if adopted and winner
            else "Nothing in the precommitted search cleared an untouched test window. Keep the bot paused."
        )
    )

    ctx = {
        "adopted": adopted,
        "winner": winner,
        "reason": reason,
        "n_search": len(search),
        "search": search,
        "train_lines": train_lines,
        "test_lines": test_lines,
        "recent_lines": recent_lines,
        "long_lines": long_lines,
        "appendix_lines": appendix,
        "selection_lines": selection,
        "fold_lines": fold_notes,
        "cap_lines": cap_lines,
        "spy_lines": spy_lines,
        "fill_lines": fill_lines,
        "exit_lines": exit_lines,
        "decision_lines": decision,
        "osc_lines": osc_lines,
        "osc_verdict": osc_verdict,
    }
    text = render(ctx)
    # Keep the machine-readable winner next to the doc so a later step can implement it.
    (cache / "redesign-decision.json").write_text(
        json.dumps({"adopted": adopted, "winner": winner, "reason": reason}, indent=2),
        encoding="utf-8",
    )
    return text


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Replay the credit-spread redesign and write the report.")
    parser.add_argument("--cache", default="var/replay-cache")
    parser.add_argument("--out", default="docs/strategy-redesign.md")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    symbols = list((cfg.get("universe") or {}).get("symbols") or [])
    if "SPY" not in symbols:
        symbols.append("SPY")
    from alpaca_options_credit.replay.data import ensure_universe

    cache = Path(args.cache)
    print(f"loading bars for {len(symbols)} symbols", flush=True)
    bars = ensure_universe(cache, symbols, include_15m=False)
    text = build_report(bars, cache, cfg)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
