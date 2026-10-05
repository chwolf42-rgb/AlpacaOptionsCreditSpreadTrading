"""VIX prior close and the refusal to invent an adjustment factor."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from research.intraday_sr.data.adjust import factor_for
from research.intraday_sr.data.vix import asof_timestamp, load_vix_csv, prior_close
from research.intraday_sr.types import ET


def test_missing_factor_is_nan_and_unconfigured_factors_fail():
    factors = pd.Series({("AAPL", date(2019, 1, 3)): 1.5})
    assert factor_for(factors, "AAPL", date(2019, 1, 2)) != factor_for(factors, "AAPL", date(2019, 1, 2))
    with pytest.raises(ValueError):
        factor_for(None, "AAPL", date(2019, 1, 2))


def test_vix_prior_close_skips_the_same_day(tmp_path):
    path = tmp_path / "vix.csv"
    path.write_text("DATE,OPEN,HIGH,LOW,CLOSE\n01/02/2019,15,16,14,15.5\n01/03/2019,16,17,15,16.5\n", encoding="utf-8")
    history = load_vix_csv(path)
    assert prior_close(history, date(2019, 1, 3)) == pytest.approx(15.5)
    stamp = asof_timestamp(date(2019, 1, 3))
    assert stamp.tzinfo == ET
    assert stamp.hour == 9 and stamp.minute == 30
