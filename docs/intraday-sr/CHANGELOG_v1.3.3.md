# SPEC v1.3.3 — zone collapse + density locks (2026-10-05 CT)

**Source:** zone-collapse ruling + density amendment (`reviews/ZONE_COLLAPSE_2026-10-05.md`). Implemented on engine PR #23 at `30c20eb`; Architect GO 2026-10-05.

- Z1: swing-pivot lookback = prior `touch_sessions` (20) sessions + today, by `available_at`. Other level kinds unchanged.
- Z2: single-linkage stays (`k_cluster` 0.25); fixed `max_zone_width_atr=1.0`; wider clusters split at the largest gap (ties → lower price), recursively.
- Z3: straddling cluster splits at `last_close` into support/resistance, never dropped; level exactly at close → resistance; min pad anchored on price-facing edge, zone never crosses close.
- Z4 (a): fixed `min_clearance_atr=0.10`; trim members < 0.10 ATR_d from close → pad → drop zones whose price-facing edge is < 0.10; exactly 0.10 kept.
- Z5 (c): stable zone identity across 15m recomputes — same side, overlap ≥ 50% of narrower padded width, one-to-one greedy (overlap, then previous score, then lower low); resets each session; arm bar frozen once known; `armed_until`/block suppresses re-arms; not a daily cap; new touch after cancel/expiry can emit; `Signal.zone` stays the snapshot.
- Z6 (b): approach rule deferred.
- Z7 pipeline: lookback → linkage → max-width splits → straddle split → clearance trim → pad → edge-clearance drop → band → score → top-K.
- Both constants fixed (`ClassVar`), not grid axes. `GRID_SHA256` unchanged: `2ef95123c015d70a1751f559f21fa1249d0b08aea272319e232f2facb120aa22`. N stays 456.

Publish to `docs/intraday-sr/SPEC.md` on `research/intraday-sr` (PR #18). Docs only; no `config/` or code.
