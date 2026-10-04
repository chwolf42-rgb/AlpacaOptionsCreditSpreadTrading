"""Adjustment factors and the prior-close VIX rule."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from research.intraday_sr.data.adjust import factor_for, load_factors
from research.intraday_sr.data.vix import load_vix_csv, prior_close

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "vix_sample.csv"


def test_factor_is_raw_over_adjusted_and_does_not_borrow(tmp_path: Path):
    raw = pd.DataFrame(
        {"date": ["2024-06-03", "2024-06-04"], "symbol": ["AAA", "AAA"], "close": [200.0, 210.0]}
    )
    adj = pd.DataFrame(
        {"date": ["2024-06-03", "2024-06-04"], "symbol": ["AAA", "AAA"], "close": [50.0, 52.5]}
    )
    raw_path = tmp_path / "raw.parquet"
    adj_path = tmp_path / "adj.parquet"
    raw.to_parquet(raw_path, index=False)
    adj.to_parquet(adj_path, index=False)
    factors = load_factors(raw_path, adj_path)
    assert factor_for(factors, "AAA", date(2024, 6, 3)) == 4.0
    assert factor_for(factors, "AAA", date(2024, 6, 4)) == 4.0
    assert factor_for(factors, "AAA", date(2024, 6, 5)) != factor_for(factors, "AAA", date(2024, 6, 5))
    assert factor_for(None, "AAA", date(2024, 6, 5)) == 1.0


def test_vix_prior_close_ignores_the_same_day():
    history = load_vix_csv(FIXTURE)
    assert prior_close(history, date(2024, 1, 4)) == 11.5
    assert prior_close(history, date(2024, 1, 5)) == 12.5
    assert prior_close(history, date(2024, 1, 6)) == 13.5
    try:
        prior_close(history, date(2024, 1, 2))
    except KeyError:
        pass
    else:
        raise AssertionError("the first session has no prior close")
