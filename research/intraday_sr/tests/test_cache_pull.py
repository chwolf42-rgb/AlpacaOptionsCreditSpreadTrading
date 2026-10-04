"""S0 cache surface. The study loader, validation, and pull land in D2-1."""

from __future__ import annotations

import pytest

from research.intraday_sr.data.cache import load_bars_file, load_symbol, validate_symbol
from research.intraday_sr.data.pull import pull_gaps


def test_load_symbol_reads_a_local_file(tmp_path):
    import pandas as pd

    path = tmp_path / "SPY.parquet"
    pd.DataFrame({"symbol": ["SPY", "QQQ"], "close": [1.0, 2.0]}).to_parquet(path)
    frame = load_symbol(path, "SPY")
    assert list(frame["symbol"]) == ["SPY"]
    assert float(frame["close"].iloc[0]) == 1.0


def test_study_helpers_wait_for_d2():
    with pytest.raises(NotImplementedError):
        load_bars_file("x")
    with pytest.raises(NotImplementedError):
        validate_symbol(None, "SPY")
    with pytest.raises(NotImplementedError):
        pull_gaps()
