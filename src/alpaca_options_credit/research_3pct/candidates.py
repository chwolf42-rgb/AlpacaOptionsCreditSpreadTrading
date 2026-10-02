"""Precommitted credit-spread candidates.

Search rows keep the locked rules: 50% take-profit, the 20% credit gate,
0.5% risk, and the confirm → arm → HVN pullback entry. Rows that lower the
credit gate, change the take-profit, or assume a mid fill are decision
scenarios. They are simulated so the drawdown cost can be shown, and they
are not eligible to be selected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from alpaca_options_credit.replay.credit import ReplayLimits

INDEX = frozenset({"SPY", "QQQ", "IWM"})
MEGA = frozenset({"AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"})

DTE_30 = {"dte_min": 30, "dte_max": 45, "dte_target": 37}
DTE_21 = {"dte_min": 21, "dte_max": 30, "dte_target": 25}
DTE_7 = {"dte_min": 7, "dte_max": 14, "dte_target": 10}


def limits(**kwargs) -> ReplayLimits:
    base = dict(
        width=5.0,
        dte_min=30,
        dte_max=45,
        dte_target=37,
        min_credit_pct=0.20,
        max_credit_pct=1.0,
        min_short_inv_gap=1.0,
        tp_frac=0.50,
        stop_mult=2.0,
        stop_check="none",
        target_abs_delta=None,
        delta_tol=None,
        close_dte=None,
        spot_stop="invalidation",
        fill_mode="natural",
        width_pct=None,
        earnings_blackout_days=0,
        multiplier=100,
        equity=100_000.0,
        risk_pct=0.005,
    )
    base.update(kwargs)
    return ReplayLimits(**base)


def _delta(delta: float, **kwargs) -> ReplayLimits:
    fields = dict(target_abs_delta=delta, delta_tol=0.08)
    fields.update(kwargs)
    return limits(**fields)


@dataclass(frozen=True)
class Candidate:
    name: str
    blurb: str
    limits: ReplayLimits
    searchable: bool
    universe: str = "all"  # all | index | mega
    trend: bool = False
    iv_min: Optional[float] = None


def allow_candidate(row: Candidate):
    def _allow(features) -> bool:
        symbol = features.symbol or ""
        if row.universe == "index" and symbol not in INDEX:
            return False
        if row.universe == "mega" and symbol not in MEGA:
            return False
        if row.trend and not features.ema_daily_ok:
            return False
        if row.iv_min is not None and (features.iv_pct is None or features.iv_pct < row.iv_min):
            return False
        return True

    return _allow


def candidates() -> list[Candidate]:
    """Order is the report order. Search rows come before decision rows."""
    rows: list[Candidate] = [
        Candidate(
            "base",
            "Live book. Nearest short past the $1 gap, $5 wide, 30–45 DTE, natural credit at least 20% of width, 50% take-profit, no price stop, structure break.",
            limits(),
            True,
        ),
        Candidate(
            "base_dte7",
            "Same live short, $5 wide, Friday expiration in 7–14 DTE, 20% credit gate, 50% take-profit.",
            limits(**DTE_7),
            True,
        ),
        Candidate(
            "base_dte21",
            "Same live short, $5 wide, Friday expiration in 21–30 DTE, 20% credit gate, 50% take-profit.",
            limits(**DTE_21),
            True,
        ),
        Candidate(
            "base_w10",
            "Same live short, $10 wide, 30–45 DTE, 20% credit gate, 50% take-profit.",
            limits(width=10.0),
            True,
        ),
    ]
    for delta in (0.10, 0.16, 0.20, 0.25, 0.30):
        tag = int(round(delta * 100))
        rows.append(
            Candidate(
                f"d{tag}_c20",
                f"Short nearest {tag} delta beyond invalidation, $5 wide, 30–45 DTE, 20% credit gate, 50% take-profit. A listed strike more than 0.08 above the target is skipped.",
                _delta(delta),
                True,
            )
        )
    for delta, dte_name, dte in (
        (0.20, "dte7", DTE_7),
        (0.20, "dte21", DTE_21),
        (0.30, "dte7", DTE_7),
        (0.30, "dte21", DTE_21),
    ):
        tag = int(round(delta * 100))
        rows.append(
            Candidate(
                f"d{tag}_c20_{dte_name}",
                f"{tag} delta, $5 wide, {dte_name.replace('dte', '')} DTE window, 20% credit gate, 50% take-profit.",
                _delta(delta, **dte),
                True,
            )
        )
    rows.extend(
        [
            Candidate(
                "d20_c20_w10",
                "20 delta, $10 wide, 30–45 DTE, 20% credit gate, 50% take-profit.",
                _delta(0.20, width=10.0),
                True,
            ),
            Candidate(
                "d30_c20_w10",
                "30 delta, $10 wide, 30–45 DTE, 20% credit gate, 50% take-profit.",
                _delta(0.30, width=10.0),
                True,
            ),
            Candidate(
                "idx_base",
                "Live book restricted to SPY, QQQ, and IWM.",
                limits(),
                True,
                universe="index",
            ),
            Candidate(
                "mega_base",
                "Live book restricted to AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA.",
                limits(),
                True,
                universe="mega",
            ),
            Candidate(
                "idx_d30_c20",
                "30 delta, 20% credit gate, SPY, QQQ, and IWM only.",
                _delta(0.30),
                True,
                universe="index",
            ),
            Candidate(
                "mega_d30_c20",
                "30 delta, 20% credit gate, the seven liquid single names only.",
                _delta(0.30),
                True,
                universe="mega",
            ),
            Candidate(
                "base_ema",
                "Live book, bull puts only above the daily EMA50 and bear calls only below it.",
                limits(),
                True,
                trend=True,
            ),
            Candidate(
                "base_iv50",
                "Live book, sell only when the 20-day IV proxy is at or above its prior-252-session percentile of 50.",
                limits(),
                True,
                iv_min=0.50,
            ),
            Candidate(
                "d30_c20_ema",
                "30 delta and the 20% gate, plus the daily EMA50 trend filter.",
                _delta(0.30),
                True,
                trend=True,
            ),
            Candidate(
                "d30_c20_iv50",
                "30 delta and the 20% gate, plus IV-proxy percentile at least 50.",
                _delta(0.30),
                True,
                iv_min=0.50,
            ),
            Candidate(
                "base_stop2",
                "Live book with a 2× credit stop judged on the hourly close. Take-profit stays 50%. Structure break stays.",
                limits(stop_check="close", stop_mult=2.0),
                True,
            ),
            Candidate(
                "d30_c20_stop2",
                "30 delta, 20% gate, 2× credit stop on the hourly close, 50% take-profit.",
                _delta(0.30, stop_check="close", stop_mult=2.0),
                True,
            ),
        ]
    )
    # Decision rows. Lowering the 20% gate is a signal-quality change.
    # A mid fill and a 25% take-profit are locked-rule or fill-assumption changes.
    for delta in (0.10, 0.16, 0.20, 0.30):
        tag = int(round(delta * 100))
        rows.append(
            Candidate(
                f"d{tag}_c10",
                f"Decision: {tag} delta, $5 wide, 30–45 DTE, credit gate lowered to 10% of width. 50% take-profit stays.",
                _delta(delta, min_credit_pct=0.10),
                False,
            )
        )
    rows.extend(
        [
            Candidate(
                "d16_c10_dte7",
                "Decision: 16 delta, 7–14 DTE, credit gate lowered to 10%.",
                _delta(0.16, min_credit_pct=0.10, **DTE_7),
                False,
            ),
            Candidate(
                "d16_c10_dte21",
                "Decision: 16 delta, 21–30 DTE, credit gate lowered to 10%.",
                _delta(0.16, min_credit_pct=0.10, **DTE_21),
                False,
            ),
            Candidate(
                "d16_c10_idx",
                "Decision: 16 delta, 10% credit gate, SPY, QQQ, and IWM only.",
                _delta(0.16, min_credit_pct=0.10),
                False,
                universe="index",
            ),
            Candidate(
                "d16_c10_stop2",
                "Decision: 16 delta, 10% credit gate, 2× close stop, 50% take-profit.",
                _delta(0.16, min_credit_pct=0.10, stop_check="close", stop_mult=2.0),
                False,
            ),
            Candidate(
                "base_mid",
                "Sensitivity: live book filled at the model mid, no bid/ask. Not a tradable limit.",
                limits(fill_mode="mid"),
                False,
            ),
            Candidate(
                "base_nickel",
                "Sensitivity: live book, natural credit minus $0.05 and an extra $0.05 on every debit.",
                limits(fill_mode="nickel"),
                False,
            ),
            Candidate(
                "d16_c10_mid",
                "Sensitivity: 16 delta and a 10% gate, filled at the model mid.",
                _delta(0.16, min_credit_pct=0.10, fill_mode="mid"),
                False,
            ),
            Candidate(
                "base_tp25",
                "Decision: live book with the take-profit lowered from 50% to 25% of credit.",
                limits(tp_frac=0.25),
                False,
            ),
        ]
    )
    return rows
