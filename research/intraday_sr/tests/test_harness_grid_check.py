import itertools

from research.intraday_sr.harness import grid_check as G
from research.intraday_sr.harness.options import BASELINE_GRID, ZERO_DTE_GRID


def test_reference_v11_passes_and_counts():
    a, b = G.reference_v11("A"), G.reference_v11("B")
    assert len(a) == len(b) == 192
    assert G.check_all(a, b, options_baseline=BASELINE_GRID, options_0dte=ZERO_DTE_GRID) == []
    assert len(BASELINE_GRID) + len(ZERO_DTE_GRID) == 18


def test_v10_grid_is_rejected():
    v10 = [dict(K=K, osc=o, rvol_min=r, tf=tf, target=t, k_cluster=kc)
           for K, o, r, tf, t, kc in itertools.product((3, 5), ("rsi", "stoch"), (1.5, 2.0), ("5m", "15m"),
                                                       ("1R", "2R", "zone"), (0.15, 0.25, 0.35))]
    errs = G.check_test_grid("Test A", v10)
    assert any("144" in e for e in errs) and any("k_confirm" in e for e in errs) and any("k_cluster" in e for e in errs)


def test_grid_hash_stable():
    assert G.grid_hash({"b": 1, "a": [1, 2]}) == G.grid_hash({"a": [1, 2], "b": 1})
