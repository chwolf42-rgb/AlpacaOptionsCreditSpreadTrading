# SPEC v1.3.5 — formations and Test B locks (2026-10-06 CT)

**Source:** Architect rulings in the Engineering room, Tue 2026-10-06 CT. Christian decided A1b runs on to formally close Test A, and Test B work starts now. Ruled before any formation or Test B result exists.

- Order: FORMATIONS (48) first, then Test B (192), and Test B only if the P1 pre-screen doesn't screen it out. Both stay in N.
- F1–F10: formation semantics (pivots, 5–60 spacing, pivot_tol scope, neckline, break window, invalidation by close, dedupe, retest and entry, risk, ATR_d/as-of).
- B1–B3: Test B = stack conditions 1–5 plus a formation whose last low is the touch bar on the same zone. The F8 neckline trigger replaces stack step 6.
- S1–S4: per-kind selection within 12; DSR N = 48 per kind; folds with no >= 200-train-trade variant count as non-positive; underpowered results can't pass.
- R7: gross / cost / net R report rows per test (0 trials).
- P1: Test B pre-screen on A1b OOS trades filtered to formation last lows. If the net mean R 95% day-block CI upper bound is < 0, Test B is screened out. It's a proxy that can only stop a run, never count toward a pass.
- GB0–GB6: gate list up to CP4.
- N stays 456. `GRID_SHA256` unchanged: `2ef95123c015d70a1751f559f21fa1249d0b08aea272319e232f2facb120aa22`. New constants are engine ClassVars, `ENGINE_SPEC = v1.3.5`.

Publish to `docs/intraday-sr/SPEC.md` on `research/intraday-sr` (PR #18). Docs only; no `config/` or code.
