"""Gross and cost R for the A1b trades file (harness 712994d).

That file has ``r``, ``pnl``, ``entry_cost`` and ``exit_cost``, and no ``gross_R`` or ``cost_R``.
CP4 and P1 both use this derivation. ``risk_usd = pnl / r``,
``cost_R = (entry_cost + exit_cost) / risk_usd``, ``gross_R = r + cost_R``.
``r == 0`` or ``pnl == 0`` is an error. Rows are not dropped.
"""
from __future__ import annotations

import math

import pandas as pd


class GrossRError(ValueError):
    """A fill row cannot be turned into gross R."""


def cost_and_gross(r, pnl, entry_cost, exit_cost) -> tuple[float, float]:
    """Return ``(cost_R, gross_R)``."""
    r_ = float(r)
    pnl_ = float(pnl)
    if not math.isfinite(r_) or r_ == 0.0 or not math.isfinite(pnl_) or pnl_ == 0.0:
        raise GrossRError(f"cannot derive gross_R when r={r_!r} and pnl={pnl_!r}")
    risk = pnl_ / r_
    if not math.isfinite(risk) or risk == 0.0:
        raise GrossRError(f"risk_usd pnl/r is {risk!r}")
    entry = float(entry_cost)
    exit_ = float(exit_cost)
    if not math.isfinite(entry) or not math.isfinite(exit_):
        raise GrossRError("entry_cost and exit_cost must be finite")
    cost = (entry + exit_) / risk
    return cost, r_ + cost


def attach_gross_r(frame: pd.DataFrame) -> pd.DataFrame:
    """Add ``cost_R`` and ``gross_R``. Sets ``net_R`` from ``r`` when ``net_R`` is absent."""
    need = [c for c in ("r", "pnl", "entry_cost", "exit_cost") if c not in frame.columns]
    if need:
        raise GrossRError(f"A1b gross_R derivation needs {need}")
    out = frame.copy()
    costs: list[float] = []
    gross: list[float] = []
    for row in out.itertuples(index=False):
        cost, g = cost_and_gross(row.r, row.pnl, row.entry_cost, row.exit_cost)
        costs.append(cost)
        gross.append(g)
    out["cost_R"] = costs
    out["gross_R"] = gross
    if "net_R" not in out.columns:
        out["net_R"] = pd.to_numeric(out["r"], errors="coerce")
    return out
