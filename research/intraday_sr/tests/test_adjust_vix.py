"""Adjustment factors and VIX are D2-1. S0 must not invent a factor of 1."""

from __future__ import annotations

import pytest

from research.intraday_sr.data.adjust import factor_for, load_adj_factors, load_factors
from research.intraday_sr.data.vix import load_vix_csv, prior_close


def test_adjust_and_vix_are_not_implemented_yet():
    with pytest.raises(NotImplementedError):
        load_factors("raw", "adj")
    with pytest.raises(NotImplementedError):
        load_adj_factors("adj")
    with pytest.raises(NotImplementedError):
        factor_for(None, "AAPL", None)
    with pytest.raises(NotImplementedError):
        load_vix_csv("vix.csv")
    with pytest.raises(NotImplementedError):
        prior_close(None, None)
