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


def _mod(**over):
    """A copy of the grids module (real grids.py once #20 lands, else the test stand-in) with overrides; the hash
    is NOT recomputed for mutated rows, exactly like a grid edited after its hash was logged."""
    import copy
    from types import SimpleNamespace
    from research.intraday_sr.harness import s0grids
    g = s0grids.grids()
    m = SimpleNamespace(**{k: copy.deepcopy(getattr(g, k)) for k in s0grids.REQUIRED})
    m.grid_document, m.grid_sha256 = g.grid_document, g.grid_sha256
    for k, v in over.items():
        setattr(m, k, v)
    return m


def test_grids_module_passes_unmutated():
    assert G.check_grids_module(_mod()) == []


def _reject(m, needle):
    errs = G.check_grids_module(m)
    assert errs and any(needle in e for e in errs), errs


def test_grids_module_rejects_mutations():
    import copy
    base = _mod()
    a = [dict(r) for r in base.TEST_A]
    a[0]["rvol_min"] = 2.5                                             # mutate a value
    _reject(_mod(TEST_A=tuple(a)), "rvol_min")
    _reject(_mod(TEST_B=tuple(base.TEST_B) + (dict(base.TEST_B[0], variant_id="B-extra"),)), "193 rows")   # add a row
    f = [dict(r) for r in base.FORMATIONS]
    f[0]["pivot_tol_atr"] = 0.35                                       # CP0 mutation list
    _reject(_mod(FORMATIONS=tuple(f)), "pivot_tol_atr")
    o = [dict(r) for r in base.OPTIONS]
    o[0]["dte_bucket"] = "8-30"
    _reject(_mod(OPTIONS=tuple(o)), "dte_bucket")
    relabel = tuple(dict(r, test="A") for r in base.TEST_B)            # Test B rows relabelled as Test A
    _reject(_mod(TEST_B=relabel), "labelled test")
    a3 = [dict(r, target="3R") if r["target"] == "zone" else dict(r) for r in base.TEST_A]
    _reject(_mod(TEST_A=tuple(a3)), "target")
    fx = copy.deepcopy(base.FIXED)
    fx["max_concurrent"] = 5                                           # change a FIXED constant
    _reject(_mod(FIXED=fx), "max_concurrent")
    _reject(_mod(PRIMARY_GUARDRAIL={"daily_losses": 2, "weekly_losses": 6}), "PRIMARY_GUARDRAIL")   # guardrail
    _reject(_mod(PRIMARY_GUARDRAIL=(2, 5)), "PRIMARY_GUARDRAIL")       # old tuple / key form
    _reject(_mod(COMPARISON_GUARDRAILS=({"name": "none"},)), "COMPARISON_GUARDRAILS")
    _reject(_mod(N_TRIALS=451), "N:")
    _reject(_mod(GRID_SHA256="0" * 64), "GRID_SHA256")
