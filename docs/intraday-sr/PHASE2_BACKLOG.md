# Intraday research, phase 2: layer backlog on the S/R + formation backbone (pre-declared), v2

Owner: Architect. Engine/data: Developer 2. Harness, costs, readout: Developer 1.
Status: **research only, model-based, no live code.** Written Sun 2026-10-04 (CT), *before* any phase-1
result exists, so nothing here is fitted to phase-1 output. Values marked **[P]** are provisional and may change
only through a new version of this file committed before the run that uses them. Anything not marked is frozen.
Inherits everything in `SPEC.md` **v1.3** unless this file says otherwise.

**v2 (Sun 2026-10-04 CT, still before any phase-1 result).** Christian's decision, relayed by Trading on 2026-10-04,
is binding: the loss guardrail is **not** report-only. **2 losses/day + 5 losses/week (d2+w5) is the primary
configuration for every layer comparison and every pass rule**, with the semantics of SPEC v1.3 G3. The base and the
layer always run under the same guardrail. "No guardrail" and d2+w6 are report-only comparison rows. Because
150 trades/mo can't hold under d2+w5, the §3 frequency path is now relative to the base (§3). The crypto cost
gate and the forward holdout are **approved by Christian via Trading on 2026-10-04**. The trial budget is unchanged;
the guardrail adds 0 trials. The v1 text is kept as `PHASE2_BACKLOG_v1.md`.

## 0. Structure (Christian's ruling) and scope
- The phase-1 **S/R zones and formations (W, IHS, M, HS) are the backbone of every candidate.** Nothing in
  phase 2 is a standalone strategy.
- Each idea is a **layer** on that backbone, one of two kinds:
  - **Filter:** gates which backbone signals are taken. It never creates a signal.
  - **Trigger:** replaces or augments the entry trigger *at a top-K zone* (§5 of SPEC: step 4 hold / RC and
    step 6 arm + stop-entry; on the Test B base, the neckline-retest entry). It never trades away from a zone.
- Phase 2 starts once phase 1 reads out (CP6), or at CP5 if phase 1 freezes no finalist. The grids below are
  frozen now, so the phase-1 holdout readout cannot shape them.

## 1. Bases (every layer is tested on both)
| Base | If phase-1 FREEZE.md has finalists | Otherwise: reference config **[P]** |
|---|---|---|
| **A** (stack, no formation) | Test A finalist (2), the fixed variant; else finalist (1)'s variant | `k_confirm`=2, K=5, RSI14 30/70, `rvol_min`=1.5, entry 5m, target 1R, `k_cluster`=0.25 |
| **B** (stack + formation) | Test B finalist (2); else finalist (1)'s variant | Base A ref + formation required at the zone (SPEC §5 Test B rules), neckline-retest entry |
- One base per test, so layer trials are ×2, not ×4.
- A frozen finalist was chosen on the whole development window, so its absolute OOS level is optimistic in the
  early folds. That is why the **stage gate is the paired difference** (§3), and the absolute bar is the holdout.
- On base B, a trigger layer fires only at zones carrying a confirmed formation, and its entry replaces the
  neckline-retest entry. Filters on base B only gate.
- Portfolio caps, risk, stops, costs, folds and dates are SPEC v1.3 unchanged: 12 entries/day, 4 concurrent,
  1 per symbol, −1.5% daily stop, 0.5% risk, flat by 15:55 ET, **plus the primary loss guardrail d2+w5** (no new
  entries after 2 losing trades in a session or 5 in a week; SPEC v1.3 G1–G3). **The 12/day cap puts a hard ceiling
  of ~252 trades/mo on every equity candidate; the 5/week loss limit gives an expected ceiling of ≈ 54/mo at 60% WR
  [P], which binds first** (§7).
- **Base and layer run under the same guardrail.** Each is simulated as its own portfolio with its own day and week
  loss counters (SPEC v1.3 G3.3). A base's reference numbers are always its d2+w5 numbers.

## 2. Reuse of phase-1 infrastructure (each layer is mostly one new module)
```
research/intraday_sr/
  layers/  base.py (Layer protocol), l1_orb.py, l2_vwap.py, l3_gap.py, l4_burst.py, l5_index.py,
           l6_pdlevels.py, l7_tod.py, combine.py
  grids_p2.py   # §4 grids as constants; sha256 logged with every trial, same scheme as grids.py
  crypto/  calendar.py (UTC sessions), pull.py, costs.py      # L8 only
```
```python
@dataclass(frozen=True)
class LayerCfg: layer_id: str; kind: Literal["filter","trigger"]; attach: str; params: Mapping[str, Any]

class Layer(Protocol):
    def features(self, bars: BarSet, as_of: datetime, cfg: LayerCfg) -> LayerFeatures  # all fields carry available_at
    def apply(self, base_signals: Iterator[Signal], zones: ZoneFeed, bars: BarSet,
              cfg: LayerCfg) -> Iterator[Signal]   # filter: subset; trigger: new Signal(s) at zones
```
- `Signal` gains `base_id` and `layer_id` (or `combo_id`). Every other contract stays as is.
- Reused unchanged: `guard.py` (every `LayerFeatures` field is guard-wrapped), fills, costs, portfolio/caps,
  `walk_forward` (25 folds, 12m/3m, same selection rule), `triallog`, `stats` (day/month block bootstrap, DSR),
  the SPEC v1.3 guardrail (`PRIMARY_GUARDRAIL` = d2+w5 is the default `RiskCfg` in every run; the comparison rows
  "none" and d2+w6 go to `guardrail_compare.parquet`), and the A1 trade-off frontier readout (computed on d2+w5).
- New in `stats.py`: `paired_day_bootstrap(layer_trades, base_trades)` (§3). New in the readout: one "layer vs
  base" block per layer per base.
- `tests/test_lookahead.py` (a), (b) and (c) run on every layer's features and on its emitted signals. A layer
  merges only with them green.
- Walk-forward selection inside a layer's grid follows SPEC §6, on the primary configuration only, with a train
  minimum of **≥ 100 trades [P]** (counted under d2+w5), because filters shrink samples. A fold with no qualifying variant trades **nothing** for that layer (counted
  as zero trades, never as falling back to the base).

## 3. Layer stage gate (incremental test vs the same base, decided on dev OOS before any holdout)
- **Primary configuration only.** Base and layer are both run under d2+w5, on the same OOS days. Every quantity in
  this section (ΔmeanR, trades/mo, CIs, fold counts, neighbours) is computed under d2+w5. The comparison rows
  ("none", d2+w6) are reported beside the gate table but never decide a pass, a kill, a ranking or a combination.
- **Paired comparison:** the same OOS days for layer and base. Resample trading days (5,000 resamples, seed
  20260925). Each draw computes `ΔmeanR = meanR(layer) − meanR(base)` and `Δtrades/mo` over the same days.
  Also report Δ win rate and Δ daily R sum.
- A layer **passes** if either path holds after costs:
  - **(a) Quality:** ΔmeanR 95% CI lower bound > 0. *(v2: the v1 floor of ≥ 150 trades/mo is removed, because no
    candidate is expected to reach 150/mo under d2+w5, §7. The sample floor that still bites is SPEC §8's ≥ 500 OOS
    and ≥ 100 holdout trades for any final candidate.)*
  - **(b) Frequency:** the layer achieves **≥ 1.2 × its base's trades/mo**, both under d2+w5 over the same full
    25-fold dev OOS, **and** the layer's own absolute OOS meanR day-block 95% CI lower bound > 0. *(v2: replaces
    the v1 rule "≥ 1.2 × base and ≥ 150/mo", whose 150 floor can't hold under the guardrail.)*
  - **And both:** the median ΔmeanR over the selected variant's ±1-step grid neighbours is > 0 (a lone spike
    fails), and the layer is positive in ≥ 55% of folds **[P]**.
- **Kill** = fails both (a) and (b) on the full 25-fold dev OOS. No early peeking: interim runs can label a
  layer INTERIM but cannot kill or promote it.
- **Trades/mo reporting:** every gate row shows base and layer trades/mo under all three configurations (d2+w5,
  none, d2+w6), stated in words against Christian's ~200/mo target. Missing the target is reported plainly, not
  fixed by loosening anything.
- **How the guardrail changes layer behaviour (mechanism, not data):** under d2+w5 a layer that lifts the win rate
  also lifts trades/mo, because losses arrive more slowly and halts come later. A filter can therefore pass (b).
  A trigger that adds lower-quality trades brings the halts forward and can lower trades/mo against its base.
- Passing the gate is **necessary, not sufficient**. A final candidate (base + layer(s)) must still clear SPEC §8
  in full under d2+w5, with N = the cumulative program trial count (§6). If the guardrail makes an §8 minimum
  unreachable, that is reported as a finding and the candidate cannot pass (SPEC v1.3 G5).

## 4. Ranked layer backlog
Ranking rule: (1) uses the phase-1 cache only, (2) can plausibly lift trades toward ~200/mo, (3) build cost.
Crypto is last. Frequencies are priors **[P]**, based on base A ref ≈ **80–160/mo** and base B ≈ **10–30/mo**,
and get replaced by phase-1 actuals at CP-P0. "Variants" = per base; the ledger adds both bases.
- *v2:* the frequency priors below are **without the guardrail** (the "none" comparison row). Under the primary
  d2+w5, every equity candidate is expected to sit at or below ≈ 54/mo at 60% WR, ≈ 36/mo at 40% WR **[P]** (§7),
  so the "~200/mo plausible?" column now applies only to the comparison row. The ranking is unchanged: it orders
  build work and adds no trials. Under d2+w5, the high-WR modes (L2 reversion, L6/L1 failure, L4 exhaustion) are
  the likeliest to raise trades/mo.

### Summary
| Rank | Layer | Kind @ attach point | Variants (A+B) | Data / requests | Expected trades/mo on base A (no guardrail, prior) | ~200/mo plausible without guardrail? | Owner |
|---|---|---|---|---|---|---|---|
| 1 | L6 prior-day level break / failure | trigger @ step 4–6 | 12+12 = 24 | cache, 0 | base + 50–100 | yes, near the 252 cap | Dev 2 |
| 2 | L1 opening range / drive | trigger @ 4–6, or filter @ gate | 12+12 = 24 | cache, 0 | filter 0.5–0.7×; trigger base + 40–90 | yes (trigger modes) | Dev 2 |
| 3 | L4 relative-volume burst | trigger @ 4–6 | 8+8 = 16 | cache, 0 | base + 40–80 | yes, borderline | Dev 2 |
| 4 | L2 VWAP reversion / trend day | filter @ gate (+ target) | 8+8 = 16 | cache, 0 | 0.3–0.8× | only on a high base | Dev 1 |
| 5 | L5 index vs component | filter @ gate | 10+10 = 20 | cache, 0 (IWM excluded) | 0.4–0.6× | only on a high base | Dev 1 |
| 6 | L3 gap fill / gap-and-go | filter @ gate (+ target), gap days only | 9+9 = 18 | cache, 0 | 0.7–0.9× | only on a high base | Dev 1 |
| 7 | L7 time of day | filter @ gate | 6+6 = 12 | cache, 0 | 0.25–0.8× | no (cuts frequency) | Dev 1 |
| 8 | L8 crypto majors (backbone + ported layers) | backbone on new data | 24 + ≤ 4 = 28 | new pull, ~0.2–0.8k req | 90–180 [P] | possible, but fees likely kill it | Dev 2 data, Dev 1 costs |
| — | Combination stage (top 2 passing layers) | as the layers | 1+1 = 2 | — | — | — | Dev 1 |
- **Equity layers: 130 trials. With the combination stage and crypto: 160 planned.**
- **Request cost for L1–L7 is zero.** They need only the fixed-33 5m RTH cache, the daily tape (ATR_d, prior-day
  and prior-week levels, 60-session β) and the Cboe VIX files already in SPEC.

### L6. Prior-day level breaks and failures (rank 1, trigger)
- **Hypothesis:** at zones that contain PDH/PDL (or the prior-week high/low), a clean break-and-retest
  continues, and a failed break reverses. Both are high-traffic events that the bounce-only stack skips.
- **Attach:** trigger at top-K zones whose `kinds` include the chosen level set. It replaces SPEC step 4–6:
  - *break-retest:* a 5m close beyond the level by ≥ 0.05·ATR_d, then a retest within `n` bars that doesn't
    close more than 0.10·ATR_d back through; buy-stop above the retest bar (mirror for shorts). Same
    mechanics as the Test B neckline retest.
  - *failure:* trades beyond the level by ≥ 0.05·ATR_d, then closes back inside within `n` bars; stop-entry
    beyond the failure bar's far extreme, in the reversal direction.
- **No lookahead:** PDH/PDL/PDC from completed sessions only (SPEC §4.5). Prior-week H/L from the completed
  week, available Monday 09:30.
- **Grid:** mode {break-retest, failure} × level set {PDH/PDL, PDH/PDL/PDC, PDH/PDL + prior-week H/L} ×
  `n` {3, 6} = **12 per base**.
- **Frequency:** PDH/PDL lies within reach on ~50% of symbol-days and is tested on ~35% **[P]**; ~40% of
  tests give a valid trigger → ~33 × 21 × 0.35 × 0.4 ≈ 95 raw per mode → **+50–100/mo** net of caps.
- **Win-rate profile:** break-retest is low WR / larger wins; failure is higher WR / smaller wins.
- **Kill:** §3. Also killed if > 60% of its OOS R comes from one mode-level combination that isn't the
  selected one (instability).
- **Reserve, not budgeted:** premarket H/L levels need extended-hours bars. The fixed-33 pull fetched them but
  kept RTH only, so re-pulling costs ~33 × 150 ≈ 5,000 requests ≈ 1.5 h at 55/min off-hours (one night).
  Needs a new version of this file before use, and its trials count.

### L1. Opening range / opening drive (rank 2, trigger or filter)
- **Hypothesis:** the opening range sets the day's first decision boundary. Zone signals aligned with the OR
  break (or a failed OR break at a zone) carry more edge than unconditioned zone touches.
- **Attach:**
  - *filter (gate before the touch):* after the OR ends, take longs only if the decision-bar close > ORH,
    shorts only if < ORL; inside the OR, no entries.
  - *trigger break-retest:* where ORH/ORL lies inside or within 0.10·ATR_d of a top-K zone, a close beyond
    the OR boundary, then the L6 retest mechanics.
  - *trigger failure:* a close beyond the OR boundary, then back inside within 3 bars, with the L6 failure
    mechanics.
- **No lookahead:** an OR of length `L` is usable only from 09:30 + `L` (10:00 for 30 min, 09:45 for 15 min).
  No layer decision before then. The OR here is the layer's own; phase-1 ORH/ORL zone levels stay 30-min.
- **Grid:** `L` {15, 30} min × mode {filter, break-retest, failure} × window {until 11:30, until 15:00} =
  **12 per base**.
- **Data:** cache. Applies to all 33 names; SPY/QQQ/IWM-only results are a readout row, not extra trials.
- **Frequency:** filter keeps 0.5–0.7× base. Triggers: OR edge near a top zone on ~40% of symbol-days × ~35%
  valid → **+40–90/mo**.
- **Win-rate profile:** breakout modes are low WR (~40%) and need ≥ 1.5R average wins. Failure modes are
  higher WR.
- **Kill:** §3.

### L4. Relative-volume momentum burst (rank 3, trigger)
- **Hypothesis:** a high-RVOL wide-range bar through a zone marks real participation. Its continuation after
  a retest (or its exhaustion into the next zone) is tradable at higher frequency than slow touches.
- **Attach:** trigger at top-K zones. A burst bar has RVOL ≥ `r` (SPEC §5 slot-RVOL) and |close − open| ≥
  `m` × ATR_5m(20).
  - *continuation:* the burst closes through the zone, then the L6 retest → stop-entry with the burst.
  - *exhaustion:* the burst runs into the next zone and the SPEC step-4 hold fires there → stop-entry against
    the burst, replacing the oscillator/MACD conditions.
- **No lookahead:** RVOL uses the prior 20 sessions' same-slot medians. ATR_5m uses completed bars only.
- **Grid:** `r` {2, 3} × `m` {1.0, 1.5} × mode {continuation, exhaustion} = **8 per base**.
- **Frequency:** ~0.3 qualifying bursts per symbol-day × ~40% valid retests → **+40–80/mo**.
- **Kill:** §3.

### L2. VWAP reversion and trend-day detection (rank 4, filter, optional target)
- **Hypothesis:** zone bounces stretched away from session VWAP revert with a high win rate. On trend days,
  counter-trend zone bounces fail and with-trend pullbacks work.
- **Attach (gate before the touch):**
  - *reversion:* take a long only if decision close ≤ VWAP − `x`·σ_V; mirror for shorts. σ_V is the
    volume-weighted std of typical price around session VWAP, bars up to and including the decision bar.
  - *trend gate:* after 10:30, a trend day = share of session bars closed on one side of VWAP ≥ `p` and
    |close − session open| ≥ 0.5·ATR_d. On a trend day, only with-trend signals; other days pass unchanged.
- **Target axis (reversion only):** {base target, VWAP}. The VWAP target is frozen at entry. If VWAP is less
  than 0.5R away, skip the trade.
- **No lookahead:** SPEC §4.7 VWAP. σ_V and the trend share use only bars up to the decision bar.
- **Grid:** reversion `x` {1.0, 1.5, 2.0} × target {base, VWAP} = 6, + trend `p` {0.7, 0.8} = 2 →
  **8 per base**.
- **Frequency:** reversion keeps 0.3–0.5×; trend gate keeps 0.6–0.8×.
- **Win-rate profile:** this is the layer most likely to show ~60% WR (reversion with VWAP targets), but with
  sub-1R wins, where the 0.06–0.11R cost bites hardest.
- **Kill:** §3.

### L5. Index vs component dislocation (rank 5, filter)
- **Hypothesis:** a stock pulling into support only because its index fell (stronger than the index on
  relative strength) bounces more reliably than one with its own weakness. For the ETFs, breadth across the
  in-universe components confirms index zone signals.
- **Attach (gate before the touch):**
  - *single names:* RS = sym return since 09:30 − β·bench return since 09:30, in ATR_d units. β is from 60
    completed daily sessions. Sign axis: *with* (long only if RS ≥ +`s`) or *against* (long only if RS ≤ −`s`,
    a dislocation fade).
  - *SPY/QQQ:* breadth = the share of in-universe names whose close is above their session VWAP at the
    decision bar. Long only if ≥ `b`, short only if ≤ 1 − `b`.
- **IWM excluded:** none of its ~2,000 components are in the cache, and a 2,000-symbol pull is out of scope.
  QQQ breadth uses the 12 in-universe Nasdaq-100 names (AMZN AAPL MSFT NFLX META NVDA GOOGL AMD INTC MU ADBE
  CSCO, fixed in `l5_index.py`); SPY uses all 30.
- **No lookahead:** all symbols are aligned on the same decision-bar close. A missing component bar drops out
  of breadth at that timestamp (never forward-filled). β uses daily bars through yesterday.
- **Grid:** `s` {0.25, 0.5} × sign {with, against} × bench {SPY, QQQ for QQQ members / SPY otherwise} = 8,
  + breadth `b` {0.6, 0.7} = 2 → **10 per base**.
- **Data:** cache, 0 requests. Not budgeted: adding missing QQQ heavyweights (TSLA, AVGO, COST, PEP, TMUS,
  QCOM) costs ~6 × 150 ≈ 900 requests ≈ 17 min off-hours. Not recommended, because those six are picked with
  2026 hindsight, which brings survivorship bias.
- **Frequency:** 0.4–0.6× (the sign axis roughly halves the base).
- **Kill:** §3.

### Note on the remaining filters (L3, L7)
Both are pure filters. Under d2+w5 a filter can pass path (a), or path (b) if it lifts the win rate enough that
fewer halts raise trades/mo to ≥ 1.2 × base (§3). Otherwise they are most useful after a trigger layer has lifted
frequency (the combination stage).

### L3. Gap fill / gap-and-go (rank 6, filter on gap days, optional target)
- **Hypothesis:** on gap days, zone signals pointing toward the prior close (fill) outperform, or, in the
  alternative mode, with-gap signals do. Non-gap days are unaffected.
- **Attach:** gap = (first RTH bar open − PDC) / ATR_d, known at 09:30 and usable from the first decision
  (09:35 close). On days with |gap| ≥ `g`:
  - *fade:* only signals toward PDC, until PDC is touched (the gap is filled), then pass-through.
  - *go:* only with-gap signals.
- **Target axis (fade only):** {base target, PDC}, with the < 0.5R skip.
- **Grid:** `g` {0.3, 0.6, 1.0} × (fade × target {base, PDC} = 2, + go = 1) = **9 per base**.
- Gaps > 3·ATR_d stay flagged as the SPEC §7 earnings proxy.
- **Frequency:** 0.7–0.9× (gap days are ~30–50% of symbol-days at `g` = 0.3).
- **Kill:** §3.

### L7. Time of day (rank 7, filter)
- **Hypothesis:** zone edges concentrate in the open and the last hour; the lunch tape is noise.
- **Attach:** entry-time gate on the signal's `available_at`.
- **Grid:** window {09:35–10:30, 09:35–11:30, all except 11:30–13:30, 14:00–15:00, 09:35–11:00 ∪ 14:00–15:00,
  09:45–15:00} = **6 per base**. The windows are fixed here because time-of-day picking is a classic
  overfitting trap.
- **Frequency:** 0.25–0.8×.
- **Kill:** §3.

### L8. Crypto majors: the same backbone on its own data (rank 8, last)
- **Hypothesis:** zone and formation signals transfer to liquid crypto, where 24/7 sessions give more
  opportunities.
- **Universe [P]:** BTC/USD, ETH/USD, SOL/USD (top liquid, Alpaca-tradable). Fixed before any run, same
  freeze rule as SPEC §1.
- **Session definition:**
  - day = **00:00–24:00 UTC** (the convention most crypto daily candles and levels use).
  - Prior-day H/L/C, ATR_d, VWAP anchor, the 30-min opening range (00:00–00:30 UTC), RVOL slots (288 per day),
    the daily loss stop and the **day loss count (d2)** all reset at 00:00 UTC.
  - week = Monday 00:00 UTC to the next Monday 00:00 UTC (7 days, because crypto trades on weekends) for the
    **week loss count (w5; w6 in the comparison row)**. The primary configuration d2+w5 applies to L8 like every
    other layer.
  - No new entries after 23:00 UTC, forced exit at 23:55 UTC (flat each UTC day).
  - Readouts also show CT times.
- **Data:** `/v1beta3/crypto/us/bars`, 5m, from the earliest Alpaca history **[P, verify; expected ~2021]**
  through 2026-09-30. ~2,100 days × 288 ≈ 605k bars per symbol → 61 pages at 10k/page, or ~250 at the
  ~2,400 bars/page seen on stocks.
  - Total ≈ **200–750 requests ≈ 4–14 min at 55/min off-hours.** Fits any night; run it off-hours.
  - `TimeOfDayLimiter` must add this path to its allow-list (spec change, Dev 2) and counts it in the same
    budget as stocks.
- **Folds:** 12m/3m from the first full year, roughly 17 folds through 2026Q1, then the same holdout window
  (2026-04-01 → 09-30, ~183 days including weekends).
- **Costs [P, verify against Alpaca's current crypto fee schedule]:**
  - Taker 25 bp per side (stop-entries, stops, forced exits); maker 15 bp (limit targets).
  - Half-spread 2 bp for BTC/ETH, 5 bp for SOL. Stop and forced-exit slippage 2× the half-spread.
  - No borrow: shorts are excluded unless Alpaca supports them for the account (expected: **long-only [P]**).
- **Cost gate (run first, 0 extra trials; approved by Christian via Trading on 2026-10-04):** compute round-trip
  cost ÷ stop distance on base A ref crypto signals. If the median is > **0.25R**, kill L8 before any grid runs.
  The gate is a cost ratio on signals, so the guardrail does not affect it.
  - Expected: BTC ATR_d ≈ 3% with a ~0.3·ATR_d stop ≈ 90 bp, against ≈ 45–55 bp round trip ≈ 0.5–0.6R.
    **Likely kill.**
- **Grid:** backbone target {1R, 2R, next zone} × entry TF {5m, 15m} × `k_confirm` {1, 2} × base {A, B} =
  **24**, plus at most the 2 passing equity layers ported once × 2 bases = **≤ 4**.
- **Frequency:** 3 symbols × ~30 days × 1–2 signals/day ≈ **90–180/mo [P]** without the guardrail. Under d2+w5,
  the 5/week limit gives the same expected ceiling as equities, ≈ 54/mo at 60% WR **[P]**.
- **Kill:** cost gate, then §3 under d2+w5. There is no equity base to pair against, so the crypto ports are
  paired against crypto base A, and the crypto backbone itself must show an absolute OOS meanR day-block 95% CI
  lower bound > 0.

## 5. Combination stage (at most one, pre-declared)
- Per base, take the **top 2 passing layers** by ΔmeanR CI lower bound.
- Each layer is fixed at its frozen variant: the best median OOS fold rank, SPEC §6 rule (2).
- How they combine:
  - filter + filter: both gates.
  - trigger + filter: the trigger's signals, gated.
  - trigger + trigger: the union, deduplicated (same symbol and direction within 3 bars: the earliest wins).
- **1 trial per base (2 total)**, logged. The combination passes only if it beats the better single layer on
  §3 (paired against that layer, not the base), with both run under d2+w5.
- No further stacking. A third layer needs a new version of this file, and its trials count.
- **Final candidates per base ≤ 2:** the best single layer, plus the combination if it passed.

## 6. Program-wide multiple-testing rule (applies to phase 1 and phase 2)
1. **One ledger:** `triallog.parquet` for the whole program, one row per (program, phase, test or layer,
   base, variant, fold) with grid hash, git sha, symbols, metrics, and `program_seq`.
   - Phase-1 rows are imported as-is: **N₁ = 192 + 192 + 48 + 18 = 450**.
   - Grid hashes are shared: a layer variant on base A and the same variant on base B are distinct trials.
   - A rerun that follows a code fix **after results were seen** counts again.
2. **Budget:** phase 2 is capped at **200 trials**: 160 planned (130 equity layers + 2 combinations + 28 crypto)
   plus a reserve of 40 for spec-amended reruns or the L6 premarket reserve.
   - **Program N_max = 650.** Going past it requires a new spec version and a note in every readout.
   - **The guardrail adds 0 trials.** d2+w5 is fixed ex ante and is not a searched parameter (SPEC v1.3 G2). The
     comparison rows (none, d2+w6) and the sensitivity rows are report-only, are not ledger rows, and never feed
     selection. The 200-trial phase-2 budget and N_max = 650 are unchanged.
   - Choosing a guardrail after seeing results is forbidden. Changing the primary configuration needs a new SPEC
     version, and its trials count.
3. **Deflated Sharpe on cumulative N:** every candidate's DSR (daily returns under d2+w5) uses N = the ledger
   count at its FREEZE, at least 450 + all phase-2 rows logged so far.
   - The variance of trial Sharpes comes from the whole ledger.
   - Pass threshold is still ≥ 0.95. At N ≈ 650, the expected maximum null Sharpe is ≈ 3.6 standard
     deviations of the trial-Sharpe distribution, so stacking ideas raises the bar for everyone.
4. **Per-candidate one-time holdout:** each final candidate gets `FREEZE-P2-<id>.md` (variant, ledger sha256,
   git sha) and is read exactly once through `run_holdout(candidate_id)`, which writes
   `holdout-<id>.lock`. A second read is a protocol breach.
5. **Shared-holdout problem:** 2026-04-01 → 09-30 is the only holdout window and gets read by phase-1
   finalists and every phase-2 candidate. Phase 2 is also designed after the phase-1 holdout readout, even
   though its grids are frozen now. Each extra read leaks.
   - **Screen (on the shared window):** Holm–Bonferroni across **all** candidates that read it (phase-1
     finalists + phase-2 candidates, m ≤ 4 + 6). One-sided day-block bootstrap p-value for mean R > 0 at
     family α = 0.05, plus the SPEC §8.5 conditions.
   - **Confirmation (approved by Christian via Trading on 2026-10-04; decisive):** a **fresh forward holdout**,
     as in SPEC v1.3 G7.
     - Bar collection starts 2026-10-05 09:30 ET. Each candidate's window starts at the later of that and its
       `FREEZE-P2-<id>.md` commit; only bars after the freeze are scored for it.
     - Signals for the frozen survivors are generated by the same code on new bars only (shadow scoring, **no
       orders**), under d2+w5.
     - Read once at ≥ 100 trades **and** ≥ 3 months, Holm across the survivors. Before the read, only trade and
       session counts are visible.
     - **Forward scorer data pulls:** ≤ **30 requests/min on weekdays 08:15–15:15 CT** and ≤ 60/min otherwise,
       enforced by `TimeOfDayLimiter`; at most one pull cycle every 5 minutes after the bar close; symbols
       batched per request; global backoff on HTTP 429 (30 s doubling to 600 s).
   - Only a candidate that passes both goes to Trading as **validated**. A screen-only pass is labeled
     *holdout-screened, not confirmed*. Any paper trading is Trading's decision after that.

## 7. Honest target math (read before judging any readout)
- **Break-even WR after costs.** With cost c in R per round trip: 1:1 needs (1 + c)/2, 2:1 needs (1 + c)/3.
  - At c = 0.06–0.11R: **53–55.5% at 1:1** and **35.3–37% at 2:1**.
  - 60% at 1:1 net is 0.20 − c ≈ +0.09 to +0.14R per trade, ≈ 9–14%/mo at 200 trades and 0.5% risk (no-guardrail comparison only). That
    is far above the 3–5% target and would be exceptional, so expect to trade WR against frequency.
- **Win-rate profiles:**
  - High WR, small wins: L2 reversion (VWAP target), L3 fade (PDC target), L6/L1 failure modes,
    L4 exhaustion. These are the only realistic route to ~60%, and the most cost-sensitive, because their
    targets are often < 1R.
  - Low WR, big wins: L6/L1 break-retest, L4 continuation, the L2 trend gate. Expect 35–45% WR with ≥ 1.5R
    average wins. They can be profitable while visibly missing the 60% target.
- **Frequency ceiling:** the 12 entries/day cap gives ≤ ~252/mo. Without the guardrail, 200/mo is ~80% of the cap,
  so the backbone plus triggers must fire nearly every day across 33 names. Under the primary d2+w5 the
  guardrail is the tighter ceiling (next bullet).
- **The guardrail is binding and conflicts with 200/mo.** Christian chose the guardrail (2026-10-04), so d2+w5 is the
  primary configuration and ~200/mo is reported against it, not chased. Expected trades until the k-th loss =
  k / (1 − WR). At 60% WR **[P]**:
  - **d2** alone allows ~5 trades/day on average, **≈ 105/mo**.
  - **w5 / w6** alone allow ~12.5–15/week, **≈ 54–65/mo**.
  - **d2+w5 (primary):** w5 binds first, so **≈ 54/mo** (≈ 43/mo at 50% WR, ≈ 36/mo at 40% WR) **[P]**.
  - 150/mo under w5 needs WR ≥ ~**86%**; 200/mo (~46/week) needs ≥ **89%** under w5 and ≥ **87%** under w6.
  - At ≈ 54 trades/mo and 0.5% risk, a 60% win rate at 1:1 nets ≈ 2.4–3.8%/mo after c = 0.06–0.11R **[P]**. 3%/mo
    needs E[R] ≈ 0.11R net per trade and 5%/mo ≈ 0.19R **[P]**.
  - These are expectations that ignore signal supply and the other caps. Phase-1 actuals replace them at CP-P0.
- Every readout states each target (WR, trades/mo, net return) in words, met or not.

## 8. Start order, owners, and checkpoints (after phase-1 CP6, or CP5 with no finalists)
| # | Owner | Task | Done when |
|---|---|---|---|
| P0 | Dev 1 + Dev 2 | `layers/base.py`, `grids_p2.py` (hashes), program-wide triallog migration (450 phase-1 rows), base resolution from FREEZE.md or §1 reference, `paired_day_bootstrap`, phase-1 base frequencies (under d2+w5 and the two comparison rows) replacing the §4 priors | **CP-P0 Architect review** (ledger count, base ids, gate math on a fixture, base and layer both on d2+w5) |
| P1 | Dev 2 | L6 trigger (+ lookahead tests) | tests (a)(b)(c) green on L6 features and signals |
| P1' | Dev 1 | L2 filter + "layer vs base" readout block | paired readout reproducible with the seed; **CP-P1** reviews L6 + L2 together |
| P2 | Dev 2 | L1, then L4 triggers | lookahead green |
| P2' | Dev 1 | L5 (multi-symbol alignment), L3, L7 filters | lookahead green, IWM exclusion tested |
| P3 | Dev 1 | full dev-OOS run of all 7 layers × 2 bases under d2+w5 → gate table, plus the comparison rows | **CP-P2 Architect review**, then an INTERIM note to Trading (holdout untouched) |
| P4 | Dev 1 | combination stage → `FREEZE-P2-<id>.md` | **CP-P3 sign-off on the freezes and the ledger N** |
| P5 | Dev 1 | holdout screen, once per candidate, Holm | **CP-P4 review**, then to Trading via the Team Manager, labeled screened |
| P6 | Dev 2 | crypto: limiter allow-list, UTC calendar, off-hours pull, cost gate (approved 2026-10-04; runs before any crypto grid) | **CP-P5** reviews the gate; kill or continue |
| P7 | Dev 2 + Dev 1 | crypto grid (+ ported layers) → freeze → holdout screen | **CP-P6 review** |
| P8 | Dev 1 | forward holdout harness (approved 2026-10-04; shadow scoring under d2+w5, §6.5), pulls ≤ 30 req/min weekdays 08:15–15:15 CT and ≤ 60/min otherwise | limiter settings reviewed before the first market-hours pull; read at ≥ 100 trades and ≥ 3 months; **CP-P7 final review** |
- Order: P0, then P1 ∥ P1', then P2 ∥ P2', then P3. Dev 2 starts P6 once P2 lands, so crypto data is ready by
  CP-P2 without delaying equity.
- At every CP the Architect checks: guard coverage of the new features, OR/VWAP/gap availability times,
  cross-symbol alignment (L5), grid-hash adherence, ledger N, and that no holdout lock exists early. From v2 on,
  also: the guardrail logic (SPEC v1.3 G8 fixture), base and layer on the same d2+w5, and that no selection, gate
  decision, combination or freeze used a comparison configuration.
- Operational rules are SPEC §9 unchanged: research only, no running bots, no pytest in live checkouts, keys in
  memory only, ≤ 30 req/min weekdays 08:15–15:15 CT and ≤ 60/min otherwise, gitignored data, draft PRs titled
  `[research, do not merge]`.
