from datetime import date

import pandas as pd
import pytest

from research.intraday_sr.harness import adjfactors as AF
from research.intraday_sr.harness import costs as K
from research.intraday_sr.harness.config import CostCfg
from research.intraday_sr.harness.options import IVContext, TradingCalendar
from research.intraday_sr.harness.portfolio import FrameBarSource
from research.intraday_sr.tests._harness_stubs import flat_day


def _write(tmp_path, sym="BRK.B"):
    d = tmp_path / "adj_factors"
    d.mkdir()
    # Trading's convention: adj_factor = adj_close / raw_close (here 0.9 on 03-04, 0.95 on 03-05)
    pd.DataFrame({"date": ["2024-03-04", "2024-03-05"], "raw_close": [200.0, 200.0], "adj_close": [180.0, 190.0],
                  "adj_factor": [0.9, 0.95]}).to_parquet(d / f"{sym.replace('.', '-')}.parquet")
    return tmp_path


def test_factor_file_converted_to_harness_raw_over_adj_and_joined_by_session(tmp_path):
    root = _write(tmp_path)
    f = AF.load_factors("BRK.B", root)
    assert f[date(2024, 3, 4)] == pytest.approx(200 / 180) and f[date(2024, 3, 5)] == pytest.approx(200 / 190)
    fr = pd.concat([flat_day("BRK.B", "2024-03-04", px=180.0), flat_day("BRK.B", "2024-03-05", px=190.0),
                    flat_day("BRK.B", "2024-03-06", px=190.0)], ignore_index=True).drop(columns="adj_factor")
    src = FrameBarSource({"BRK.B": fr}, adj_root=root)
    assert src.session_frame("BRK.B", date(2024, 3, 4))["adj_factor"].iloc[0] == pytest.approx(200 / 180)
    assert src.session_frame("BRK.B", date(2024, 3, 6))["adj_factor"].iloc[0] == 1.0     # missing -> 1.0
    assert src.adj_info.approximate and src.adj_info.approx_sessions["BRK.B"] == 1
    # as-traded price = adjusted * f = 180 * 200/180 = 200: cost uses 3.5 bp of 200 per as-traded share
    f4 = 200 / 180
    c = K.fill_cost(K.ENTRY, 1, 100, 180.0, f4, "T2", CostCfg())
    assert c == pytest.approx((100 / f4) * 3.5e-4 * 200.0)


def test_missing_file_falls_back_to_one_labelled_approximate(tmp_path):
    fr = flat_day("AAA", "2024-03-04").drop(columns="adj_factor")
    src = FrameBarSource({"AAA": fr}, adj_root=tmp_path)
    assert src.session_frame("AAA", date(2024, 3, 4))["adj_factor"].iloc[0] == 1.0
    assert "APPROXIMATE" in src.adj_info.note()


def test_option_factor_map_feeds_ivcontext(tmp_path):
    root = _write(tmp_path)
    info = AF.AdjInfo()
    m = AF.option_factor_map(["BRK.B", "SPY"], [date(2024, 3, 4)], root, info)
    ctx = IVContext(TradingCalendar([date(2024, 3, 4)]), {}, {}, {}, adj_factor=m)
    assert ctx.f("BRK.B", date(2024, 3, 4)) == pytest.approx(200 / 180)        # strike from 180 adj -> 200 raw
    assert ctx.f("SPY", date(2024, 3, 4)) == 1.0 and info.source["SPY"] == "approx_1.0"


REAL = AF.DEFAULT_ROOT / "adj_factors" / "SPY.parquet"


@pytest.mark.skipif(not REAL.is_file(), reason="Trading's factor files not on this box")
def test_spy_early_2019_as_traded_is_about_1_over_0894_of_adjusted():
    f = AF.load_factors("SPY")
    for d in (date(2019, 1, 2), date(2019, 3, 29)):
        assert 1 / f[d] == pytest.approx(0.894, abs=0.005)       # Trading adj_factor ~0.892-0.896
        assert f[d] == pytest.approx(1 / 0.894, rel=0.006)       # as_traded = adjusted * f
    assert AF.load_factors("BRK.B") is not None                   # BRK-B.parquet naming
    assert AF.coverage_gaps("SPY", [date(2019, 1, 2), date(2026, 10, 2)]) == []


def test_require_coverage_refuses_gaps(tmp_path):
    root = _write(tmp_path)
    AF.require_coverage({"BRK.B": [date(2024, 3, 4)]}, root)
    with pytest.raises(AF.FactorCoverageError):
        AF.require_coverage({"BRK.B": [date(2024, 3, 6)], "AAA": [date(2024, 3, 4)]}, root)
