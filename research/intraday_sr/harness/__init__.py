"""Developer 1 harness (SPEC sections 4-8): guard, fills, costs, portfolio, walk-forward, options, stats,
readout, trial log. Research only; model-based; nothing here imports or touches src/ order paths.

HoldoutToken construction is gated in walkforward.run_holdout() (SPEC v1.3.1 C2); this package is the only
place a token may be built.
"""
