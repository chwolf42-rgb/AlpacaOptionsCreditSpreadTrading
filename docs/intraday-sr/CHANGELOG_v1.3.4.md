# SPEC v1.3.4 — rulings R1–R5 on Christian's five requests (2026-10-05 CT)

**Source:** Christian's five requests relayed via Chief of Staff and Team Manager; Architect rulings Mon 2026-10-05 CT. Committed before any 33×192 result is read. New S/R rules are deliberately not matched to the old bot.

- R1: whole-day over-cap score ranking **rejected** (lookahead, not tradable live). Primary stays causal: time order; same-bar ties → higher zone score, then symbol A→Z. Add 0-trial report row: share of sessions where 12/day and 4-concurrent caps bind; count + score distribution of skipped vs taken signals. Causal alternative (pre-set score floor / reserved slots) only as a separate pre-registered study after CP4, trials counted.
- R2: zone sensitivity `min_clearance_atr` {0.05, 0.10, 0.15} × `max_zone_width_atr` {0.75, 1.0, 1.25} = 9 cells (0.10/1.0 primary), frozen finalists only, post-selection, report-only robustness; own parquet (e.g. `zone_sensitivity.parquet`) no selection code reads; never feeds selection/FREEZE/pass bar/frontier flags. 0.10/1.0 stay locked for the primary run.
- R3: comparison `RiskCfg` d3+w6 (3/session, 6/week) added to `guardrail_compare.parquet`; after selection, same selected variants + finalists, 0 trials. Comparison set = none, d2+w6, d3+w6. Primary stays d2+w5. G4 table → 4 rows.
- R4: IEX 5m re-check of equity finalists after finalists are chosen; same frozen config; window stated, set by available IEX data; caveat that IEX volume share is small so RVOL and volume-profile zones differ materially; robustness of the live feed, not a pass/fail gate; 0 trials.
- R5: after equity CP4, overlay finalists (daily + weekly books, O1) re-priced on real Alpaca option bars from 2024-02 onward where bars exist; beside the Black-Scholes MODEL; report only; never re-selects equity or overlay finalists.
- Cross-refs added: §5 Risk cap tie line (R1), G1 comparison configurations + G4 table (R3), Z8 audit note (R2), §7 options MODEL pricing (R5), §8 readout (all).
- N stays 456. `GRID_SHA256` unchanged: `2ef95123c015d70a1751f559f21fa1249d0b08aea272319e232f2facb120aa22`. Launch head stays `30c20eb`. None of R1–R5 holds the 33×192 launch.

- R6: R2/R4/R5 never open the §6 holdout or the G7 forward window (data through 2026-03-31 before the holdout read; later rows labeled post-holdout, descriptive).

Publish to `docs/intraday-sr/SPEC.md` on `research/intraday-sr` (PR #18). Docs only; no `config/` or code.
