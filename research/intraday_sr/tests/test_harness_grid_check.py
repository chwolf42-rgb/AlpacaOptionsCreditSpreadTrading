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


def test_grids_module_check_requires_primary_guardrail_in_hash():
    from types import SimpleNamespace
    a, b = G.reference_v11("A"), G.reference_v11("B")
    f = [{"variant_id": f"F{i}"} for i in range(48)]
    o = [{"variant_id": f"O{i}"} for i in range(9)]
    z = [{"variant_id": f"Z{i}"} for i in range(9)]
    pg = {"max_losses_day": 2, "max_losses_week": 5}
    good = SimpleNamespace(TEST_A=a, TEST_B=b, FORMATIONS=f, OPTIONS=o, OPTIONS_0DTE=z, PRIMARY_GUARDRAIL=pg,
                           grid_document=lambda: {"test_a": a, "primary_guardrail": pg})
    assert G.check_grids_module(good) == [] and G.N_TOTAL == 450
    bad = SimpleNamespace(TEST_A=a, TEST_B=b, FORMATIONS=f, OPTIONS=o, OPTIONS_0DTE=z, grid_document=lambda: {"test_a": a})
    errs = G.check_grids_module(bad)
    assert any("PRIMARY_GUARDRAIL" in e for e in errs) and any("hashed" in e for e in errs)
