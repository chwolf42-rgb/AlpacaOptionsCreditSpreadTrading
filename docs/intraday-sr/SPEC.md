# Intraday S/R confluence research: spec v1.3.3 (frozen before any run)

Owner: Architect. Engine: Developer 2. Harness, walk-forward, costs, options overlay, readout: Developer 1.
Status: **research only, model-based, no live code.** Written Sun 2026-10-04 (CT); v1.3.2 and v1.3.3 Mon 2026-10-05 (CT). Values marked
**[P]** are provisional and may change only through a new spec version committed *before* the run
that uses them. Anything not marked is frozen.
v1.3 (Sun 2026-10-04 CT, before any run): the loss guardrail d2+w5 is binding and is the primary configuration for
every result; a forward holdout is added. See the v1.3 amendments (G1–G8), which replace A3.
v1.3.1 (Sun 2026-10-04 CT, before any run): clarifications C1–C3 (bad prints, holdout lock, guardrail constant),
from the CP0 review of PR #20. No new trials; N was 450.
v1.3.2 (Mon 2026-10-05 CT, before any options overlay run): dual-expiry books (O1). Options overlay N becomes 24;
program N = **456**. Equity grids and F2/cache work are unchanged.
v1.3.3 (Mon 2026-10-05 CT, before the 33×192 launch): zone collapse + density locks (Z1–Z8): pivot lookback,
max zone width, straddle split, min clearance, stable zone identity. Fixed constants, no new trials; N stays 456;
`GRID_SHA256` unchanged.

## v1.1 amendments (Sun 2026-10-04 CT; these supersede v1.0 wherever they conflict)

Source: Christian's asks relayed via Trading after v1.0 was frozen. Committed before any run and before `grids.py` freezes.

**A1. Frequency first (target ~150–250 trades/mo).**
- New grid axis `k_confirm` = number of the 3 *optional* stack conditions that must hold (OB/OS oscillator, MACD cross or histogram turn, RVOL ≥ `rvol_min`): **{0, 1, 2, 3}**. The level hold (rejection, close-back, or reclaim) and the re-confirmation entry stay **mandatory**, so a signal always has 2–5 of the 5 conditions. `k_confirm = 3` is v1.0's all-conditions stack.
- `k_cluster` is fixed at **0.25·ATR_d** (removed from the grid) to stay inside the cap.
- New grid: `K` {3,5} × oscillator (2) × `rvol_min` (2) × entry TF {5m,15m} × target (3) = 48, × `k_confirm` (4) = **192 variants for Test A** and **192 for Test B**. The variant cap per test is raised from 144 to 192. Every trial is logged, and the deflated Sharpe uses the new N.
- Portfolio caps (fixed, not grid axes): **12 entries/day** portfolio-wide (was 10), **4 concurrent** (was 3), 1 open position per symbol, daily loss stop −1.5% unchanged. All 33 names.
- **Readout addition:** a trade-off frontier across all logged variants showing trades/mo, win rate, avg R, and net monthly return, each with its OOS 95% CI. Flag settings at 150–250 trades/mo whose OOS mean-R 95% lower bound is > 0. A flag is informational only; flagged settings must still clear the full §8 pass bar, because choosing from the curve is itself selection.

**A2. 0DTE options scenario (second overlay grid, frozen finalists only).**
- 9 variants: option-price stop {−30%, −40%, −50%} × take-profit {+50%, +65%, +80%}. Time exit fixed at **15:45 ET**.
- Sizing: **$2,000 premium per trade** on $100k model equity (2%). Reported separately from the 0.5% baseline overlay and labeled *higher-risk, model-based*.
- Trades only where a same-day expiry actually existed on that date (SPY/QQQ/IWM per their historical expiry calendars; single names only on their expiry days). Report the count of skipped signals.
- Pricing: Black-Scholes repriced on every 5m bar from the underlying's high, low and close, using the §7 IV and skew model. If the stop and the TP are both reachable within a bar, the stop wins. Buy at the ask, sell at the bid, $0.05/contract.
- Report per variant: win rate, avg win and avg loss, trades/mo, monthly mean with 95% CI, max DD, worst day, and break-even win rate after costs.
- Caveat in every readout: VIX9D-based IV understates near-expiry skew and gamma, so these 0DTE numbers are low-confidence.
- Total options variants = 9 (v1.0 overlay) + 9 (0DTE) = 18, all logged, never used to re-select equity finalists. *(v1.3.2 O1: dual books → 6 baseline + 18 A2 = 24 rows; N = 456. A2 scenarios apply to both books, not daily-only.)*

**A3. Loss guardrails (reporting overlay, not selection).** *[Superseded by v1.3 G1–G8: the guardrail d2+w5 is now
binding and primary. Text kept for the record only.]*
- Implemented as `RiskCfg` flags: **d2** = no more entries for the day after 2 losing trades; **w5 / w6** = no more entries for the week after 5 / 6 losing trades.
- Combinations run: none, d2, w5, w6, d2+w5, d2+w6. They are applied to the same selected variants and frozen finalists (stocks and options) **after** selection, so they add no trials to N.
- Report per combination vs. none: trigger rate, and the change in trades/mo, monthly return, max DD, and worst week. Note that d2 directly limits the ~200 trades/mo goal.
- Promoting a guardrail into the selected configuration requires a new spec version before the holdout opens.

**A4. Ordering is unchanged.** CP4 review happens before any result goes to Trading. Developer 1 pings the Architect as soon as Test A has a first OOS read. After CP4 clears, the interim note goes to Trading, labeled INTERIM, holdout untouched.

## v1.3.3 amendments (Mon 2026-10-05 CT; these supersede §5 "Levels" and "Zones" wherever they conflict)

Source: zone-collapse ruling and density amendment, Mon 2026-10-05 CT (`reviews/ZONE_COLLAPSE_2026-10-05.md`).
Diagnosis: single-linkage at 0.25·ATR_d over ~530–600 unbounded historical pivot candidates chained into one
~3.1–3.4 ATR_d cluster that straddled `last_close` and was dropped, so K was inert (K3 ≡ K5 on SPY). After the
first fix, the straddle split glued the near edge to `last_close` (median emitter gap 0.01 ATR_d) and zone identity
reset every 15m recompute, giving ~30 emits/day. Committed **before** the 33×192 launch and any `PROGRAM_LEDGER`
entry. Implemented on engine PR #23 at head `30c20eb`; Architect re-check **GO** Mon 2026-10-05 CT.

**No new trials. N stays 456. No new grid axes.** Both new constants are fixed engine constants (`ClassVar`,
not grid fields), so `GRID_SHA256` is unchanged:
`2ef95123c015d70a1751f559f21fa1249d0b08aea272319e232f2facb120aa22`.

**Z1. Swing-pivot lookback.** Swing-pivot candidates (all TFs: 5m/15m/1h/1d) are limited to pivots whose
confirmation `available_at` falls in the prior `touch_sessions` (= 20) sessions plus today, up to `as_of`.
PDH/PDL/PDC, ORH/ORL, session VWAP, HVN and round numbers are unchanged (already session- or profile-scoped).

**Z2. Max zone width.** Single-linkage at fixed `k_cluster = 0.25·ATR_d` stays (no switch to complete-linkage).
Add fixed `max_zone_width_atr = 1.0`. A cluster whose (max − min) member price exceeds 1.0·ATR_d is split at its
largest internal gap between adjacent sorted members; if several gaps tie for largest, split at the lowest-priced
one. Repeat recursively on each piece until every piece is ≤ 1.0·ATR_d wide.

**Z3. Straddle split.** A cluster that straddles `last_close` is **never dropped**. It splits at `last_close` into
a support piece (members strictly below `last_close`) and a resistance piece (members at or above). A level
exactly at `last_close` goes to **resistance**. The minimum pad (0.05·ATR_d, §5) is anchored on the
**price-facing edge** (support: `high`; resistance: `low`) and grows away from price, so a padded zone never
contains or crosses `last_close`. No emitted zone contains `last_close`.

**Z4. Minimum clearance (density lock a).** Add fixed `min_clearance_atr = 0.10`. Applies to every drafted zone,
not only straddle halves:
1. **Trim:** remove members whose price is closer than 0.10·ATR_d to `last_close`. A piece left empty is dropped.
2. **Pad** per Z3 (price-facing edge anchored).
3. **Drop:** drop any zone whose price-facing edge is still closer than 0.10·ATR_d to `last_close`. A gap of
   **exactly** 0.10·ATR_d is kept (the drop test is strict `<`).

**Z5. Stable zone identity across 15m recomputes (density lock c).**
1. At each 15m recompute, a new zone inherits the stable identity of a previous zone on the **same side** whose
   padded range overlaps it by **≥ 50% of the narrower padded width**.
2. Matching is one-to-one, greedy: candidate pairs are taken in order of larger overlap, then higher previous
   score, then lower `low`; each previous and each new zone is used at most once. Unmatched new zones get a new id.
3. Identities **reset at each session start**. An empty recompute (one 15m book with no zones) does not by itself
   wipe identities carried within the session.
4. The stable id is internal to the engine. `Signal.zone` stays the snapshot `Zone` at the signal's `as_of`;
   no interface field changes.
5. A touch's arm bar is **frozen** the first time it is known. The existing `armed_until` / one-active-setup block
   then suppresses re-arms of the same stable zone. This is **not** a daily cap: a new touch after the setup
   cancels or expires may emit again.

**Z6. Approach rule (density lock b): deferred.** Revisit only if Z4+Z5 leave emits/day high enough that the
guardrail caps dominate selection.

**Z7. Pipeline order (binding):** lookback-filtered levels (Z1) → single-linkage (`k_cluster`) → max-width gap
splits (Z2) → straddle split (Z3) → clearance trim (Z4.1) → pad (Z3/Z4.2) → edge-clearance drop (Z4.3) →
±2 ATR_d band → score → top `K` per side.

**Z8. Audit note.** 0.10·ATR_d now does real selection work (90–93% of emitting zones sit within 0.25·ATR_d).
It stays fixed and is **not** tuned on results; any change needs a new spec version committed before the run.

## v1.3.2 amendments (Mon 2026-10-05 CT; these supersede A2, G6, and the §5/§7 options overlay text wherever they conflict)

Source: Christian's reinforcer relayed by Trading via Team Manager on Mon 2026-10-05: **options overlay scaffolding must cover both daily and weekly expiries from the start** — side-by-side books, same exits and risk scenarios. Not daily-only. Committed before any options overlay run. Does **not** start overlay coding; Dev 1 implements only when the Architect clears parallel overlay work. Equity engine/harness (F2, cache, CP4) stays first.

**O1. Dual-expiry books (binding scaffold rule).**
1. From the first options overlay scaffold commit (`harness/options.py` and dependents), every overlay **scenario** runs **two books in parallel**: `book=daily` and `book=weekly`.
2. Same frozen-finalist signals, same structure, same stop / take-profit / time-exit / sizing. Only the listed expiry chosen for that trade differs.
3. **Daily book:** nearest listed expiry with calendar DTE ≤ 1 on that date (same-day / 0DTE when the calendar lists it). If none, skip for that book and count the skip. A skip is not a trade under G3.
4. **Weekly book:** nearest listed **weekly** expiry for that symbol — Friday weeklies for single names; for SPY/QQQ/IWM the **Friday weekly** even after daily listings exist — with calendar DTE in **2–10** trading days **[P]**. Prefer Friday over mid-week weeklies when both exist **[P]**. If none, skip and count.
5. Each `(scenario, book)` is its own G3 portfolio under primary **d2+w5**. Loss counts and blocked-signal counts do **not** cross books.
6. **Readouts** always show daily | weekly side-by-side for every scenario. A scaffold or readout that is daily-only or weekly-only is a **protocol breach**.
7. **A2 high-risk sleeve (amends A2):** the 9 option-price stop {−30%, −40%, −50%} × take-profit {+50%, +65%, +80%} scenarios, time exit **15:45 ET**, sizing **$2,000** premium on $100k (2%), apply to **both** books (A2 was daily/0DTE only). Still labeled *higher-risk, model-based*. VIX9D understatement caveat applies especially to the daily book; the weekly book is still MODEL. Rows = **9 × 2 = 18**.
8. **Baseline §5 sleeve (amends §5 options overlay):** replace the DTE-bucket axis with the book axis. Structures stay {long ATM, long 1 strike OTM, debit vertical (long ATM, short nearest the equity target)}. Count = **3 structures × 2 books = 6** (was 9 structure×DTE). Exit still matches the equity trade (stop, target, or 15:55 forced exit) unless a scenario says otherwise.
9. **Trial count N (amends G2 / A2 totals):** options overlay rows = 6 + 18 = **24**. Program N = 192 (Test A) + 192 (Test B) + 48 (formations) + 24 = **456** (was 450). Overlay rows are logged and feed DSR N; they still never re-select equity finalists. Guardrail comparisons remain 0 selection trials.
10. §7 expiry **calendars** (what was listed when) stay the authority Dev 1 verifies against Cboe history. O1 only chooses which listed expiry each book takes. Single-name Friday-only listing history still means the daily book skips most single-name days — report those skips.
11. G6 still applies: both books run under primary d2+w5; each `(scenario, book)` is its own portfolio; skips are not losses.

## v1.3 amendments (Sun 2026-10-04 CT; these supersede v1.2 and earlier wherever they conflict)

Source: Christian's decision, relayed by Trading on Sun 2026-10-04 and now binding: **the loss guardrails are part of
the strategy, not a report-only overlay.** In the same relay Christian approved the crypto cost gate before any crypto
grid (`PHASE2_BACKLOG.md` L8) and a fresh forward holdout from 2026-10-05. v1.3 is committed before any run: no trial
log, FREEZE file or holdout lock exists on the box, and the first interim run (R1) is Mon 2026-10-05 AM. Universe,
data, grids, folds, costs and the §8 pass bar are unchanged. **G1–G8 replace A3.**

**G1. Primary configuration: loss guardrail d2+w5 (replaces A3).**
- **Primary configuration, used for every reported result:** no new entries for the rest of the session after
  **2 losing trades in that session**, and no new entries for the rest of the week after **5 losing trades in that
  week**. Shorthand **d2+w5**.
- It sits on top of the existing fixed caps, which are unchanged: 12 entries/day, 4 concurrent, 1 open position per
  symbol, −1.5% daily loss stop, no new entries after 15:00 ET, forced exit at the open of the 15:55 bar.
- Implementation: `RiskCfg(max_losses_day=2, max_losses_week=5)` held as the constant `PRIMARY_GUARDRAIL` in
  `grids.py`, so it is inside the grid hash logged with every trial. It is the default `RiskCfg` for `simulate()`. It
  is not a grid axis.
- **Comparison configurations (report only):** **none** (the existing caps only) and **d2+w6** (2 losses/day,
  6 losses/week). The v1.1 combinations d2, w5 and w6 on their own are dropped.

**G2. Multiple testing and selection.**
- The guardrail is fixed ex ante. It is not a searched parameter, so it adds **0 trials**. N stays 192 (Test A),
  192 (Test B), 48 (formations alone) and 24 (options overlays under O1 dual books): **456** in total. *(v1.3.2: was 18 / 450.)*
- The following are computed **only** on the primary configuration: per-fold variant selection (including the
  ≥ 200 train-trade minimum), both §6 finalist rules, FREEZE.md, every item of the §8 pass bar, the deflated Sharpe,
  the ±1-step neighborhood check, the per-year and per-symbol robustness checks, the holdout, the forward holdout
  (G7), and the A1 frontier flags.
- Comparison configurations are run **after** selection, on the same per-fold selected variants and the same frozen
  finalists, with only `RiskCfg` changed. They never feed selection, a freeze, a pass decision or a frontier flag.
  Their results go to `guardrail_compare.parquet`, which no selection, freeze or holdout code reads. They are not
  trial-log rows and are not counted in N.
- **Choosing the best guardrail after seeing results is forbidden.** Changing the primary configuration requires a
  new spec version, committed before the run that uses it. Its trials are counted in N, and every trial already run
  under d2+w5 stays in N.

**G3. Guardrail semantics (frozen).**
1. **Loss:** a closed trade with realized R < 0 after costs (`Trade.r < 0`). R = 0 is not a loss. Wins never reduce
   a count.
2. **When a loss counts:** at the trade's exit fill, in its exit bar. Never at entry, and never on open
   (unrealized) P&L.
3. **Scope:** losses are counted across the whole portfolio (all 33 symbols), not per symbol. Each simulated
   portfolio has its own counters: one variant or finalist, in one test, in one simulation window. Test A, Test B,
   each formation test and each options overlay variant are separate portfolios.
4. **Day:** one RTH session (09:30–16:00 ET, or to the early close). The day count resets at the start of every
   session.
5. **Week:** the Monday–Friday calendar week in ET. The week count resets at the first session of each week
   (Monday, or Tuesday after a Monday holiday). A holiday-shortened week is still one week.
6. **What a limit does:** when the day count reaches 2, no new entries for the rest of that session. When the week
   count reaches 5, no new entries until the first session of the next week. Armed stop-entry triggers that have not
   filled are cancelled. A limit blocks new entries only.
7. **Trades already open** when a limit trips run to their normal exit (stop, target, or the 15:55 forced exit). They
   are not flattened by the guardrail. Their losses still count: a loss after the day limit has tripped is added to
   the week count (and can trip the week limit), and a loss after the week limit has tripped is still recorded.
8. **Daily loss stop (−1.5%, unchanged):** when it is hit, flatten everything and take no more entries that session,
   exactly as in §5. A day that hits the daily stop is over whether or not the day count reached 2. Flattened trades
   that close with R < 0 count as losses toward the week count (and the day count). G3 does not change when the
   −1.5% check runs.
9. **Order of events inside one bar (deterministic):**
   - (i) First, exits of positions that were open at the start of the bar, in exit order: exit fill time, then
     symbol A→Z. All fills inside one 5m bar share that bar's timestamp, so in practice the symbol breaks the tie.
     The counts update after each exit, so the trade log names the exit that tripped a limit.
   - (ii) Then entries that would fill in this bar, in the existing entry priority (higher zone score, then symbol
     A→Z). An entry is skipped if a limit has already tripped, including a trip in step (i) of the same bar.
   - (iii) If an entry from step (ii) is also stopped out in its fill bar (§4.2, the stop wins), its loss counts
     immediately, before the next entry in step (ii) is considered.
10. **Window starts:** counters start at 0 on the first session of each simulated window: each fold's train window,
    each fold's test window, the holdout, and the forward holdout. A week that straddles a window start counts only
    the trades inside the window.
11. **Logging:** every trade row carries `day_losses_before` and `week_losses_before`. Every session row carries the
    time of any day-limit trip, week-limit trip or daily-stop hit, and the number of signals blocked by the guardrail.

**G4. Readouts.**
- **Per finalist** (equity and each options overlay row), a 3-row table: **primary d2+w5** (first row; this is the
  result), **none**, and **d2+w6**. Columns: trades/mo; win rate; mean R with its 95% day-block bootstrap CI (§8
  settings); monthly return at 0.5% risk (mean with its month-block 95% CI); max drawdown; days halted by the day
  limit; weeks halted by the week limit; daily-stop days; signals blocked by the guardrail.
- Every readout states the trades/mo each configuration actually achieved, in words, against Christian's ~200/mo
  target and the 150–250 band.
- The **A1 trade-off frontier** (150–250 trades/mo) is computed on the primary configuration, and its flags come from
  the primary configuration only. A no-guardrail frontier may be shown beside it, labeled *comparison*, with no flags.
- **Plain note, required in every readout:** the expected number of trades until the k-th loss is k / (1 − WR). At a
  60% win rate the 2/day limit alone allows about 5 trades per session, ≈ 105/mo. The 5/week limit allows about 12.5
  trades per week, ≈ 54/mo, so it binds first: the primary configuration's expected ceiling is **≈ 54/mo at 60% WR**
  (≈ 43/mo at 50%, ≈ 36/mo at 40%) **[P, an expectation that ignores signal supply and the other caps]**. Reaching
  150/mo under the primary configuration would need a win rate of about 86% or more, and 200/mo about 89% **[P]**.
  **The 150–250 trades/mo target is therefore likely unreachable under the primary configuration.** Readouts say so
  honestly. Nothing (grids, caps, the guardrail, or the pass bar) is loosened to chase it.
- The same applies to §8's "5–10 trades a day": ≈ 54/mo is ≈ 2.6 trades per session at 60% WR **[P]**. At ≈ 54
  trades/mo and 0.5% risk, 3%/mo needs E[R] ≈ 0.11R net and 5%/mo ≈ 0.19R **[P]**. At 1:1 with c = 0.06–0.11R per
  round trip, that is a win rate of about 59–61% and about 62–65% **[P]**; a 60% win rate at 1:1 gives
  ≈ 2.4–3.8%/mo **[P]**.

**G5. Pass bar under the primary configuration.**
- §8 is unchanged and is measured on the primary configuration, including the minimums of **≥ 500 concatenated OOS
  trades** and **≥ 100 holdout trades**, and the ≥ 200 train-trade selection minimum in §6.
- Feasibility **[P]**: at ≈ 54/mo, the 25 OOS quarters (75 months) have room for ≈ 4,000 trades and the 126-session
  holdout for ≈ 320, so the minimums are reachable in principle on Test A. The ≥ 200 train-trade minimum needs
  ≈ 17 trades/mo over a 12-month train window, and the holdout minimum about the same rate. Lower-frequency tests
  (Test B, single formation kinds) may fall short.
- If the guardrail makes a minimum unreachable, the readout reports it as a **finding** (for example "Test B under
  d2+w5: 61 holdout trades, underpowered"), and that result cannot pass. The bar is not relaxed, and a comparison
  configuration cannot stand in for the primary one.

**G6. Options overlays under the primary guardrail (amends A2 and the §5 options overlay).** *(v1.3.2: see O1 dual books.)*
- Every overlay scenario runs under d2+w5 on **both** the daily and weekly books (O1). Every reported result uses the
  primary configuration.
- Each `(scenario, book)` is its own portfolio. It receives the frozen finalist's signals and applies G3 to its **own**
  closed option trades: a loss is an option trade with R < 0 after spread and fees (R = premium paid, §7). Signals
  skipped because that book's expiry did not exist are not trades and do not count.
- **24** options rows (6 baseline + 18 A2×books), 0 added selection trials, never used to re-select equity finalists.
  The overlay readout gets the same 3-row table (G4) **per book**, shown side-by-side (O1.6).

**G7. Forward holdout (approved by Christian via Trading, Sun 2026-10-04).**
- **Purpose:** a confirmation read after the §6 holdout, because 2026-04-01 → 2026-09-30 is shared by every phase-1
  finalist and every phase-2 candidate (`PHASE2_BACKLOG.md` §6.5).
- **Window:** bar collection starts Mon 2026-10-05 09:30 ET. For each finalist, only bars with `available_at` after
  its FREEZE commit are scored, so its window starts at the later of the two. Bars collected before a candidate's
  freeze are stored but never scored or inspected for that candidate.
- **No orders,** and no calls to trading or account endpoints. The frozen code and config (FREEZE.md: variant, config
  hash, git sha) generate signals on the new bars only and simulate fills with the §4 and §7 rules (shadow scoring).
  Scoring uses the primary configuration; comparison rows may be reported beside it, labeled.
- **Read once,** when a candidate has **≥ 100 trades and ≥ 3 months** of scored window, both. `run_forward_holdout(
  candidate_id)` refuses to run before both hold, then writes `forward-<id>.lock`. A second read is a protocol breach.
  Before the read, the scorer reports only trade and session counts; no R, P&L or win rate is shown to anyone.
- **Pass:** the §8.5 conditions on the forward trades (mean R > 0 and day-block 95% CI lower bound > 0), with Holm
  across every candidate read in the forward window (`PHASE2_BACKLOG.md` §6.5). A finalist that passes §8 but not
  (yet) the forward read is labeled *holdout-screened, not confirmed*.
- **Time to read [P]:** at ≤ ~54/mo, Test A reaches 100 trades in about 2 months, so the 3-month minimum binds. A
  finalist trading 10–30/mo needs about 4–10 months.
- **Data pulls (enforced in code by `TimeOfDayLimiter`, §2.3):**
  - **≤ 30 requests/min on weekdays 08:15–15:15 CT** (market hours), **≤ 60/min otherwise**. On HTTP 429, a global
    backoff of 30 s doubling to 600 s.
  - At most one pull cycle every 5 minutes, started after the latest 5m bar has closed. Symbols are batched: one
    multi-symbol `/v2/stocks/bars` request covers all 33 (paginated if needed), never one request per symbol.
    Expected load ≈ 0.2–1 request/min **[P]**, far under the cap.
  - The scorer takes `<cache_root>/.pull.lock` for each cycle and releases it afterwards, so it never runs alongside
    another puller.
  - Same feed and adjustment as the cache (SIP, adjustment=all) **[P, verify the account's data plan]**. If SIP bars
    are only available after a delay, pull with that lag. This does not change results, because scoring uses
    `available_at` and no orders are placed.
  - VIX/VIX9D come from the Cboe daily CSVs (§2.2), once per day after the close.
  - Forward bars are stored in `/workspace/research2/data/intraday_sr/forward/` **[P]** (gitignored), separate from
    the development cache, and never merged into it.
- The scorer is a research process only (§9): not inside any bot, not run from a live checkout, and it never touches
  a running bot.

**G8. Checkpoints.** From CP4 on, every Architect checkpoint also checks:
- **Guardrail logic** against a hand-computed synthetic fixture covering: a 2nd loss mid-session; open trades running
  past a trip; a loss after the day trip that counts toward the week; a week trip carrying to Friday and resetting
  the next week (including a Monday holiday); several exits in one bar with the symbol tie-break; an entry stopped
  out in its fill bar; a daily-stop flatten whose losses count; R = 0 not counted as a loss.
- **No selection used a comparison configuration:** selection, finalist and freeze code read only primary-config
  rows; no selection, freeze or holdout code reads `guardrail_compare.parquet`; the logged grid hash includes
  `PRIMARY_GUARDRAIL`; and N is still 450.

## v1.3.1 clarifications (no new trials)

Source: Architect rulings in the CP0 review of PR #20 (Sun 2026-10-04 CT), committed before any run. They tighten
definitions only. Grids, folds, costs, the universe, the guardrail values and the §8 pass bar are unchanged, and N
stays 450.

**C1. Bad prints (replaces the 8×ATR_5m spike rule in §2.2).**
- Flag-and-clamp applies only to an **isolated spike that reverts**. Both conditions must hold:
  - the bar's high or low is more than **0.5·ATR_d** from the median close of its ±2 neighbouring 5m bars;
  - the next bar's close returns to within **0.25·ATR_d** of that median.
- Bars are **never dropped** for being spikes, and the reference is never a stale close (a stale reference caused the
  cascade found at CP0). A move that does not revert is real and is left untouched.
- A flagged bar keeps `bad_print=True`. Pivot and zone detection use the **clamped** high/low, clamped to the max/min
  of the bar's open, its close and the neighbour median. Stop and target evaluation (§4.1–4.2) use the **unclamped**
  high/low, so the cleaning can never make results look better.
- Timing (follows from §4.1): the flag and clamp for bar t depend on bars t+1 and t+2, so they become usable only at
  the close of bar t+2. Before that, the engine sees bar t unclamped. The lookahead tests (§4.12) cover this.
- Flagged counts are logged per symbol. A symbol with more than 0.1% of its bars flagged goes to checkpoint review.
- Regression test on real-cache cases: SPY 2019-01-30 (FOMC), SPY 2019-08-01 and NVDA 2021-09-13 keep all their bars
  and are not flagged.

**C2. Holdout lock in the data layer (adds to §6 Holdout).**
- Every bar loader's default end date is **2026-03-31**.
- Loading any bar dated on or after **2026-04-01** requires a `HoldoutToken`. Only the harness's
  `walkforward.run_holdout()` (D1-3) can construct one, and constructing it checks that the FREEZE file exists.
  Without a token, the loader raises.
- A test asserts that the default loaders raise on holdout dates.

**C3. Guardrail constant and single source of frozen constants (clarifies G1).**
- `grids.py` holds `PRIMARY_GUARDRAIL = {daily_losses: 2, weekly_losses: 5}` as a fixed constant inside the
  canonical grid hash. The harness builds `RiskCfg(max_losses_day=2, max_losses_week=5)` from it. It is not a grid
  axis, and N stays 450.
- `grids.py` is the single source for every frozen constant. The engine and the harness import them from it, so the
  logged grid hash covers them.

## 0. Scope and non-goals
- Question: does a support/resistance confluence entry on 5m/15m, flat by the close, have a positive
  after-cost edge out of sample on a fixed liquid universe, as equities and as a long-options overlay with **daily and weekly** expiry books side-by-side (O1)?
- Not in scope: live or paper trading, changes under `src/alpaca_options_credit/`, any running bot,
  tuning toward Christian's targets. Results below target are reported plainly.

## 1. Universe (pre-declared, frozen once filled)
- Fixed list: **SPY, QQQ, IWM + 30 single names = 33 symbols.** Same list for every fold, the interim
  runs, and the holdout.
- **LOCKED (v1.2, Sun 2026-10-04 CT), from Trading's `/workspace/research2/sr_fixed33_universe.json` (`asof_session` 2018-12-31):**
  `SPY QQQ IWM AMZN AAPL MSFT NFLX META NVDA GOOGL AMD BAC V JPM BA INTC MU BRK.B ADBE WFC CSCO C MA JNJ CRM HD XOM UNH DIS ORCL PG WMT MRK`
  - Rule as applied: PIT S&P 500 members on 2018-12-31, ranked by 60-session median dollar volume through that close; one share
    class per company (GOOGL kept, GOOG dropped, so NVDA enters as the 31st-ranked name); FB followed as META; still tradable on
    Alpaca through 2026-09. That last filter is a declared survivorship screen, but it removed **no** names, so the list equals the
    2019-01-02 PIT list and the "survivorship-biased universe" label below does **not** apply.
  - Data: `/workspace/research2/data/alpaca_intraday/m5rth_fixed33/` (per-symbol parquet, 5m SIP, adjustment=all, RTH only,
    2019-01-02 to 2026-09-30, early closes handled; BRK.B stored as `BRK-B`; notes in `README_fixed33.md`). 15m comes from
    `research/intraday_lab/resample.py::build_15m()` (09:30-anchored). Cloud workers get a packaged copy from Trading, never a re-pull.
- Background: the handoff (`/workspace/research2/intraday_lab_handoff.md`) and the cache also contain *quarterly*
  point-in-time (PIT) top-30 lists (`research/intraday_lab/plans/universes.json`, 39 quarters,
  2017-01-03 … 2026-07-01). Those are not used for selection.
- Selection rule the list must satisfy: top 30 non-ETF names by median dollar volume over the 60
  sessions **ending 2018-12-31**, among names that were tradable then (the same rule as `universe.py`). This
  is the 2019-01-02 PIT list, so it has no survivorship bias over the 2019+ test window. Reference copy of
  that quarter's list from `universes.json`, for checking only:
  `AMZN AAPL MSFT NFLX FB GOOGL GOOG AMD BAC V JPM BA INTC MU BRK.B ADBE WFC CSCO C MA JNJ CRM HD XOM UNH DIS ORCL PG WMT MRK`.
  - Share classes: keep GOOGL, drop GOOG, and add the 31st-ranked name from the same ranking.
  - Renames: FB is pulled as the literal `FB` (asof="-") and continues as META. The data layer stitches them by date.
- If Trading's list is ranked as of a later date (for example the 2026-07-01 list with SNDK, GEV, PLTR, LITE),
  every result carries the label **"survivorship-biased universe"**. The readout must then add one robustness
  row on the 2019-01-02 PIT list.
- Freeze rule: once the list is pasted here and committed, no symbol is added or removed after any result
  (interim included) has been seen. A symbol whose data is missing is reported as missing, not replaced.

## 2. Data
### 2.1 Reuse Trading's cache first, pull only gaps (paths **[P]**, Trading will forward final ones)
Found on the box (read-only look, 2026-10-04 ~15:30 CT):
| What | Path | Format / coverage |
|---|---|---|
| 5m bars | `/workspace/research2/data/alpaca_intraday/m5_current/part_NNNN.parquet` (+ `state.json`, `requests.csv`) | columns `ts` (ET-naive, **bar open**), `symbol`, `open/high/low/close` float32, `volume` float64, `trades` int32, `vwap` float32. SIP, adjustment=all, extended hours kept. **Only ADBE complete**: 249,956 bars, 2016-11-01 09:25 → 2026-09-30 19:50 ET. Rest of the pull pending (Trading's ~9 h pull, starting tonight). |
| Daily, adjusted | `/workspace/research2/data/alpaca/{current,removed}_all.parquet` | `date` (str), `symbol`, OHLC float64, `volume` int64. 2016-01-04 → 2026-10-02, 532 current + 250 removed symbols. |
| Daily, raw | `/workspace/research2/data/alpaca/{current,removed}_raw.parquet` | Same schema, unadjusted (AAPL 2019-01-02: raw 157.92 vs adj 37.44). |
| Daily PIT panel | `/workspace/tmp/review/eq-macd-intraday/research/macd_support/cache/panel_daily.npz` | 2,703 sessions × 747 columns, plus `column_sources.json`. |
| Membership | `.../research/equity_swing/cache/sp500_ticker_start_end.csv` | S&P 500 start/end dates. |
| Universes / pull plans | `.../research/intraday_lab/plans/{universes,sr_5m,m15,rest_30m,all}.json` | quarterly PIT lists, 5m plan 2016-11-01 → 2026-10-01 UTC. |
No 15m/30m bars were pulled. No VIX data exists on the box.

### 2.2 What this study uses
- **5m, RTH only, 2019-01-02 → 2026-09-30.** Keep bars with open time 09:30 ≤ ts ≤ 15:55 ET, 78 per full
  session. Early closes (13:00) are handled with the NYSE calendar from `pandas_market_calendars`, or a static
  list in `research/intraday_sr/data/calendar.py`.
- 15m, 1h, and daily are **resampled from 5m** (§3). No separate pulls.
- **Premarket/overnight high/low: DEFERRED.** The data is RTH-only, so these candidates are dropped from
  the level set. They can return only in a new, separately pre-declared test once extended-hours data is in.
- Adjustment: prices are adjustment=all. Daily factor `f_d = raw_close_d / adj_close_d` (from the daily
  tapes) is constant within a session. Round numbers, option strikes and cents-per-share costs use the
  as-traded price `adj × f_d`. Everything else uses adjusted prices.
- VIX / VIX9D: Cboe public daily CSVs, verified 2026-10-04:
  `https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv` (from 1990-01-02) and
  `.../VIX9D_History.csv` (from 2011-01-04), both through 2026-10-02 with columns DATE, OPEN, HIGH, LOW, CLOSE.
  These are daily, so a decision on day d uses the **close of day d−1** only.
- Bad bars: drop bars with high < low or non-positive prices (count them in the readout). Spikes are never
  dropped (v1.3.1 C1): only an isolated spike that reverts is flagged (`bad_print=True`) and clamped. Its high or
  low is more than 0.5·ATR_d from the median close of its ±2 neighbours, and the next close returns within
  0.25·ATR_d of that median. Pivots and zones use the clamped high/low; stops and targets use the unclamped
  extremes. A move that does not revert is left untouched. A missing 5m bar is left missing, never
  forward-filled for signals.

### 2.3 Rate limits (enforced in code, not by convention)
- `research/intraday_sr/data/ratelimit.py: TimeOfDayLimiter` is the only path to `data.alpaca.markets`.
  - **Weekdays 08:15–15:15 CT: ≤ 30 requests/min** (hard cap 30, paced target 28).
  - **All other times: ≤ 60/min** (hard cap 60, paced target 57).
  - Holidays count as weekdays (conservative).
  - The budget is re-evaluated on every `acquire()` over a rolling 60 s window, so a 60→30 switch blocks
    immediately.
  - On HTTP 429: global backoff of 30 s, doubling to 600 s.
  - Wraps `alpaca_options_credit.market_data_limit.MarketDataLimiter` semantics (injectable clocks), so it
    can be unit-tested without waiting.
  - *(v1.3: the forward-holdout scorer uses this limiter too, with ≤ 1 pull cycle per 5 minutes; see G7.)*
- One puller process at a time, enforced by the lock file `<cache_root>/.pull.lock`.
  - GETs to `/v2/stocks/bars` only.
  - Every request is logged (timestamp, path, status, bytes; never keys).
- Bulk/gap pulls run at night or on weekends. Note that Trading's existing puller paces at 57/60 s
  regardless of time and has a hard stop at Mon 2026-10-05 05:30 CT. Any run past 08:15 CT must use the
  30/min cap.

### 2.4 Cloud agents
Cloud agents cannot see the box. **Anyone working in the cloud asks Trading to package the cache** (for example
`intraday_sr_cache_<date>.tar` of the parquet files plus a manifest with sha256 per file) instead of re-pulling.
Market data is never committed to git. Only synthetic fixtures go in the repo.

### 2.5 Memory budget (~5 GB free on the box while the bots run)
- float32 for prices and indicators, int32 for counts. Data is processed **one symbol at a time** by default.
- Peak RSS ≤ 2.5 GB per process, at most 2 worker processes.
- One symbol is ~152k RTH 5m bars (≈5 MB raw, ≤ 60 MB with all features).
- Engine outputs (zones, formations, signals) are written to per-symbol parquet keyed by the engine config
  hash. The harness then loads only signals plus the bars around them.

## 3. Interfaces (module `research/intraday_sr/`, outside `src/`)
```
research/intraday_sr/
  types.py          # dataclasses below, the only shared contract
  grids.py          # §5 grids as constants; sha256 of their JSON is logged with every trial
  data/  cache.py (read Trading parquet + gaps), resample.py, calendar.py, adjust.py, vix.py, ratelimit.py, pull.py
  engine/  levels.py, zones.py, indicators.py (numba), signals.py, formations.py      # Dev 2
  harness/ guard.py, fills.py, costs.py, portfolio.py, walkforward.py, options.py, stats.py, readout.py, triallog.py  # Dev 1
  tests/   fixtures/ (synthetic only), test_lookahead.py, test_resample.py, test_fills.py, test_costs.py, ...
```
Reuse, read-only imports from `alpaca_options_credit` (do not edit):
- `replay.credit.bs_price`, `bs_delta`, `year_fraction` (Black-Scholes)
- `market_data_limit.MarketDataLimiter` (limiter pattern)
- `replay.stats.bootstrap_mean` (pattern only; §6 needs a block bootstrap)
- `strategy.volume_profile.hvn_shelves` (semantics; reimplement vectorized)

```python
TZ = "America/New_York"   # all timestamps tz-aware ET inside research code

@dataclass(frozen=True)
class Bar:                # one row of the bar frame; frames are pandas/numpy, this documents the schema
    symbol: str; tf: Literal["5m","15m","1h","1d"]
    ts: datetime          # bar OPEN time
    available_at: datetime  # bar CLOSE time = ts + tf (1d: 16:00 or early close)
    open: float; high: float; low: float; close: float  # float32, adjusted
    volume: float; vwap: float; trades: int
    session: date; adj_factor: float   # raw/adj for the session

@dataclass(frozen=True)
class Level:              # one candidate before clustering
    symbol: str; kind: str   # pivot_5m|pivot_15m|pivot_1h|pivot_1d|pdh|pdl|pdc|orh|orl|hvn|vwap|round
    price: float; weight: float; as_of_ts: datetime; available_at: datetime

@dataclass(frozen=True)
class Zone:
    symbol: str; low: float; high: float; side: Literal["support","resistance"]  # vs. price at as_of
    score: float                    # 0..1
    components: Mapping[str, float] # touches, rejections, recency, volume, n_kinds
    kinds: tuple[str, ...]; as_of_ts: datetime; valid_from_ts: datetime   # valid_from = available_at
    available_at: datetime; engine_cfg: str          # config hash

@dataclass(frozen=True)
class Formation:
    kind: Literal["W","IHS","M","HS"]; symbol: str; tf: str
    pivots: tuple[tuple[datetime, float], ...]    # confirmed pivots used
    neckline: tuple[float, float]                 # (price at break_ts, slope per bar)
    invalidation: float; break_ts: datetime; retest_ts: datetime | None
    confirmed_ts: datetime; as_of_ts: datetime; available_at: datetime; zone_id: str | None

@dataclass(frozen=True)
class Signal:
    symbol: str; tf: Literal["5m","15m"]; direction: Literal[1,-1]
    test: Literal["A","B","F_W","F_IHS","F_M","F_HS"]
    zone: Zone; formation: Formation | None
    trigger: float                 # stop-entry price (as-traded tick rounding applied)
    stop: float; targets: Mapping[str, float]   # "1R","2R","zone"
    expires_at: datetime           # trigger cancels after this
    components: Mapping[str, float]
    as_of_ts: datetime; available_at: datetime; variant_id: str

@dataclass(frozen=True)
class Fill:  symbol: str; ts: datetime; price: float; qty: float; side: int; reason: str; cost: float
@dataclass(frozen=True)
class Trade: signal: Signal; entry: Fill; exit: Fill; r: float; pnl: float; fold: int; variant_id: str

# Engine entry points (pure functions of bars with available_at <= as_of)
def zones_at(bars: BarSet, as_of: datetime, cfg: EngineCfg) -> list[Zone]
def formations_at(bars: BarSet, as_of: datetime, cfg: EngineCfg) -> list[Formation]
def signals(bars: BarSet, start: datetime, end: datetime, cfg: EngineCfg, sig: SignalCfg) -> Iterator[Signal]
# Harness entry points
def simulate(signals: Iterable[Signal], bars: BarSet, risk: RiskCfg, costs: CostCfg) -> list[Trade]
def walk_forward(grid: Grid, folds: list[Fold]) -> WFResult
```
- **Guard:** `harness/guard.py` wraps every engine object. Consuming one with `available_at > decision_ts`
  raises `LookaheadError`. The guard is always on, including in production runs.

## 4. No-lookahead rules
1. A bar is usable only at `available_at` (its close). Decisions happen at a bar's close. A market fill
   happens at the **next bar's open**. A stop-entry fills in a later bar when it trades through the trigger:
   `fill = max(trigger, open)` for a buy, plus slippage.
2. **Same-bar ambiguity:** if one bar touches both stop and target, assume the **stop**. If a bar gaps
   beyond the stop, fill at that bar's open.
3. A **pivot** needs N bars on each side. A swing high is the strict max of 2N+1 bars. It becomes available
   at the close of the Nth right-hand bar.
   - **N_5m = 3** (+15 min), **N_15m = 3** (+45 min), **N_1h = 2** (+2 h), **N_1d = 2** (+2 sessions).
4. Higher timeframes are built from 5m, RTH-anchored:
   - 15m: 09:30, 09:45, … (26 per session).
   - 1h: 09:30 … 14:30, plus a 30-min 15:30–16:00 bar flagged `partial`.
   - 1d: RTH aggregate.
   - Each is usable only once its last 5m bar has closed. A partial (forming) HTF bar is never visible.
5. **Prior-day H/L/C** come only from completed sessions and are available from 09:30 of the next session.
6. **Opening range:** 09:30–10:00 high/low, available at 10:00.
7. **VWAP:** session-anchored, built as Σ(bar vwap × volume)/Σ volume over bars up to and including the
   decision bar.
8. **Volume profile:** the prior 5 completed sessions plus the session to date, through the decision bar.
   - Bin size 0.05 × ATR_d. HVN = bins at or above the 70th percentile.
   - Shelves = contiguous HVN bins (same semantics as `hvn_shelves`).
9. **ATR_d:** Wilder 14 on completed daily bars, through yesterday.
10. **Indicators and warmup:**
    - Indicators: RSI(14), Stoch(14,3,3), MACD(12,26,9) on the entry timeframe, continuous across sessions
      over RTH bars, with a warmup of 100 bars of that timeframe.
    - Global warmup: no signals before 2019-02-01, which gives ≥ 20 sessions for ATR_d and the pivots.
    - Numba ports must match pure-Python references on a fixture to 1e-5.
11. **Zones** are recomputed at every 15m close. A zone set is valid from its `available_at` until the next
    recompute.
12. **Mandatory automated tests** (`tests/test_lookahead.py`, they gate every merge):
    - (a) For 200 random T per fixture symbol, the engine output with `available_at ≤ T` is identical
      (float32 equal) whether the input is truncated at T or is the full series.
    - (b) Poison test: replace every bar after T with NaN or random walks; outputs at T are unchanged.
    - (c) The harness guard raises on an injected future object.
    - (d) HTF resample test: no 15m/1h/1d value is visible before its last constituent 5m bar closes.

## 5. Strategy definition and pre-declared grids (frozen; caps enforced in `grids.py`)
**Levels (all candidates):**
- Swing pivots on 5m/15m/1h/1d. *(v1.3.3 Z1: only pivots confirmed in the prior 20 sessions + today.)*
- PDH, PDL, PDC.
- ORH, ORL.
- HVN shelf edges and midpoints.
- Session VWAP.
- Round numbers on the as-traded price: step $1 if < $50, $5 if < $250, $10 if < $1,000, else $50.
- Candidates are taken within ±2 ATR_d of the last close. Premarket and overnight levels are deferred (§2.2).

**Zones:**
- Single-linkage clustering of candidates within `k_cluster × ATR_d`. The zone spans the min to max of its
  cluster, padded to at least 0.05 ATR_d.
  *(v1.3.3 Z2–Z5, Z7: max width 1.0·ATR_d with gap splits, straddle split at last_close, pad anchored on the
  price-facing edge, min clearance 0.10·ATR_d, stable identity across 15m recomputes; see the v1.3.3 amendments.)*
- Score = 0.30·touches + 0.30·rejections + 0.20·recency + 0.20·volume, each a percentile rank among the
  symbol's zones at as_of. Ties break on more distinct kinds.
  - touches: bars entering the zone over the prior 20 sessions on the entry TF.
  - rejections: touches with a wick ≥ 50% of the bar range that closed back outside.
  - recency: exponential decay, half-life 5 sessions.
  - volume: the zone's share of profile volume.
- Keep the top `K` zones per side within 2 ATR_d.

**Signal stack (long at support; mirror image for short at resistance). All conditions must hold:**
1. **Touch bar:** low ≤ zone.high and close ≥ zone.low.
2. **Oscillator:** oversold on the touch bar or one of the 2 bars before it.
3. **MACD:** the histogram turns up (h_t > h_{t-1} after ≥ 2 falling bars), or MACD crosses above its
   signal line, within 3 bars of the touch.
4. **Hold:** within 3 bars of the touch, one of:
   - a close above zone.high;
   - a touch bar with a lower wick ≥ 50% of its range that closes in its upper half;
   - a reclaim: a close below zone.low, then a close above zone.high.
   The bar that satisfies the hold is the **re-confirmation bar (RC)**.
5. **Relative volume:** `RVOL ≥ rvol_min` on the touch bar or RC. RVOL is volume divided by the median
   volume of the same time-of-day slot over the prior 20 sessions.
6. **Pullback/retest, then entry:**
   - After RC, the setup arms once a bar has high < RC.high or low ≤ zone.high + 0.10·ATR_d, with no close
     below zone.low.
   - Entry is a buy-stop at RC.high + $0.01 (as-traded).
   - It cancels after 6 bars or on a close below zone.low.

**Test definitions:**
- **Test A:** the stack alone.
- **Test B:** the stack, plus a formation at the same zone on the entry TF.
  - The pattern's last low (2nd bottom or right shoulder) must be the stack's touch bar.
  - Entry is the formation's neckline retest trigger: after a close beyond the neckline, a retest within
    6 bars that does not close more than 0.10 ATR_d back through it. Entry is a buy-stop above the retest
    bar's high.
- **Formations alone (F_W, F_IHS, F_M, F_HS):** the formation trigger with no stack conditions.
  - W/M: two confirmed pivots 5–60 bars apart, within 0.25·ATR_d of each other. Neckline = the extreme
    between them.
  - IHS/HS: three pivots. The head exceeds both shoulders by ≥ 0.10·ATR_d, and the shoulders are within
    0.25·ATR_d of each other. The neckline is the line through the two intervening pivots.

**Risk (all tests):**
- Stop = min(zone.low, pullback low or pattern low) − 0.05·ATR_d. The stop is at least 0.10·ATR_d from
  entry (widened to that if smaller).
- Size = 0.5% of equity / stop distance, capped at 1.0× equity notional per position and 3.0× total.
  Positions sized down by the cap are counted and reported.
- At most 3 concurrent positions and 1 per symbol. *(v1.1: 4 concurrent, see A1.)*
- Daily loss stop: −1.5% equity, realized plus open. When hit, flatten and take no more entries that day.
- At most 10 entries per day *(v1.1: 12, see A1)*. Ties go to the higher zone score, then symbol A→Z.
- No new entries after 15:00 ET. Forced exit at the open of the 15:55 bar.
- Model equity: $100,000, compounding daily.
- If the `zone` target is less than 1R away, the trade is skipped.
- *(v1.3: loss guardrail d2+w5, no new entries after 2 losses in a session or 5 in a week, is part of the risk
  rules for every reported result; see G1–G3.)*

**Grids (variant cap ≤ 200 per test; every variant run is logged, including failures):**
| Param | Values | Owner |
|---|---|---|
| `k_cluster` (× ATR_d) | 0.15, 0.25, 0.35 | engine |
| `K` zones per side | 3, 5 | engine |
| oscillator | RSI14 30/70, Stoch(14,3,3) 20/80 | signal |
| `rvol_min` | 1.5, 2.0 | signal |
| entry TF | 5m, 15m | signal |
| target | 1R, 2R, next zone | risk |
- **Test A:** 3·2·2·2·2·3 = **144** variants. **Test B:** the same 144. *(Superseded by v1.1 A1: 192 each, with `k_confirm` added and `k_cluster` fixed at 0.25.)*
- **Formations alone:** 4 kinds × TF (2) × target (3) × pivot tolerance {0.15, 0.25 ATR_d} (2) = **48**,
  i.e. 12 per kind.
- **Options overlay:** applied only to frozen equity finalists, never used to re-select them. *(v1.3.2 O1 dual books.)*
  - **Books (required):** every scenario runs `daily` and `weekly` side-by-side (O1). Daily-only scaffolding is forbidden.
  - **Baseline sleeve (6):** structure {long ATM, long 1 strike OTM, debit vertical (long ATM, short at the strike
    nearest the equity target)} × book {daily, weekly}. The old DTE-bucket axis (0–1 / 2–4 / 5–7) is replaced by the book.
  - **A2 high-risk sleeve (18):** 9 stop×TP scenarios × book {daily, weekly}; see A2 and O1.7.
  - *(v1.3: runs under the primary guardrail d2+w5, see G6; v1.3.2: 24 rows, N = 456.)*
- Everything else is fixed: N per TF, score weights, windows, buffers, and costs.
- **Trial log:** `triallog.parquet` gets one row per (test, variant, fold) with the grid hash, git sha,
  symbols, and metrics. The trial count N per test feeds the deflated Sharpe in §6.

## 6. Walk-forward, holdout, interim runs
- Data window: 2019-01-02 → 2026-09-30. Alpaca SIP history goes back to 2016, but 2019+ is chosen to
  bound the pull.
- **Development:** 2019-01-02 → 2026-03-31. **Holdout:** 2026-04-01 → 2026-09-30 (~126 sessions).
- **Folds:** rolling 12-month train, 3-month test, stepped quarterly. Test quarters run 2020Q1 … 2026Q1,
  **25 folds**. Fold k trains on [Qk − 12 months, Qk) and tests on [Qk, Qk + 3 months).
  - Fold 1 trains 2019-01-02 → 2019-12-31 (trading from 2019-02-01) and tests 2020-01-02 → 2020-03-31.
  - Fold 25 trains 2025-01-01 → 2025-12-31 and tests 2026-01-02 → 2026-03-31.
  - No embargo is needed, because everything is flat by the close and the engine is stateless.
- **Selection per fold:** the variant with the highest train mean R per trade after costs, among those with
  ≥ 200 train trades. Ties go to more trades. The concatenated OOS test trades form the headline OOS result.
  *(v1.3: selection, finalists and every metric used for them are computed on the primary guardrail config
  only, G2.)*
- **Finalists (≤ 2 per test, frozen before the holdout):**
  - (1) the *procedure*: re-select on 2025-04-01 → 2026-03-31 and trade the holdout with that variant;
  - (2) the single variant with the best median OOS fold rank among those with positive OOS mean R in
    ≥ 60% of folds.
  - The finalists, the trial-log sha256 and the git sha are committed to `docs/intraday-sr/FREEZE.md`
    before the holdout runs.
- **Holdout:**
  - It is opened **exactly once**, by `walkforward.run_holdout()`. That function refuses to run unless
    FREEZE.md exists, all 33 symbols are complete, and `holdout.lock` is absent; it then writes
    `holdout.lock`.
  - All finalists are scored in that one run, including the options overlay on them.
  - A second opening is reported as a protocol breach.
  - *(v1.3: a fresh forward holdout from 2026-10-05 confirms survivors, read once at ≥ 100 trades and ≥ 3 months;
    see G7.)*
- **Interim runs (Monday morning, while the pull completes):**
  - Same frozen grids and folds, on the development window only, using the symbols complete at run time
    (listed in the readout).
  - Labeled **INTERIM — n of 33 symbols — not a pass/fail result**.
  - The holdout is never touched in an interim run. The holdout guard also refuses to run when fewer than
    33 symbols are present.
  - Interim results cannot change grids. Any change requires a new spec version, and all earlier trials
    still count toward N.

## 7. Cost and slippage model (equities and options; **[P]** values with a 1.5× sensitivity row)
**Equities**, per side, as-traded price, commission $0:
| Tier (PIT, prior 60-session median $ volume) | half-spread + slippage |
|---|---|
| T0: SPY, QQQ, IWM | 1.0 bp, min $0.01/share |
| T1: ≥ $3B/day | 2.0 bp, min $0.01/share |
| T2: < $3B/day | 3.5 bp, min $0.01/share |
- **Stop and forced exits:** 2× the per-side cost.
- **Limit targets:** fill only if price trades through the target by $0.01, with no slippage.
- **Regulatory fees:** 0.3 bp on sell notional.

**Options (model-based; every number labeled MODEL):**
- **IV (index ETFs):**
  - σ_atm at time-to-expiry T: variance-time interpolation between the prior close's VIX9D (9 days) and
    VIX (30 days).
  - VIX9D flat for T < 9 days. QQQ and IWM scale it by RV20(sym)/RV20(SPY) from completed daily bars.
- **IV (single names):** σ_atm,sym = σ_idx × RV20(sym)/RV20(SPY), clamped to [0.15, 1.50].
  - There is no earnings calendar, so event IV is missing. Trades on earnings days are a known bias,
    flagged by a |gap| > 3·ATR_d proxy.
- **Skew (explicit):**
  - z = ln(K/S)/(σ_atm√T); σ(K) = σ_atm·(1 − a·z).
  - Put side (z < 0): a = 0.15 for ETFs, 0.08 for single names.
  - Call side (z > 0): a = 0.03 for ETFs, 0 for single names.
  - Floor 0.85·σ_atm. Sensitivity row: a × 2.
- **Time:** T = trading minutes to the expiry close / (252 × 390), minimum 15 minutes. Rate 4% flat, no
  dividends.
  - Theta is charged by repricing at exit with the remaining T. Exit IV equals entry IV; sensitivity
    row: exit IV −10%.
- **Expiries (conservative [P]; Dev 1 verifies against Cboe listing history):** *(v1.3.2: calendars feed O1 book choice.)*
  - Single names: Friday weeklies only (daily book therefore skips most single-name days; count the skips).
  - SPY: M/W/F until 2022-11-13, daily from 2022-11-14.
  - QQQ and IWM: Fridays until 2022-12-31, then M/W/F/daily per verified history.
  - **Book selection (O1):** daily book → nearest listed expiry with DTE ≤ 1; weekly book → nearest Friday weekly with
    DTE in 2–10 **[P]**. Skip that book when none exists. (The old “nearest listed expiry inside each DTE bucket” rule
    is superseded by O1.)
- **Strikes:**
  - Increment: $1 for ETFs and for names under $200, $2.50 for $200–$500, $5 above $500.
  - ATM is the nearest strike to the as-traded spot. OTM means one increment out.
- **Spread:**
  - Half-spread per leg = clamp(c × mid, lo, hi).
  - ETFs: c = 2% ATM / 3% OTM, lo $0.01, hi $0.10, ×1.5 for 0DTE after 14:00.
  - Single names: c = 6% (same as `leg_bid_ask` in the repo), lo $0.05, hi $0.25.
  - Buy at mid + half, sell at mid − half.
- **Fees:** $0.05/contract per side (regulatory, Alpaca commission-free). Sensitivity row: $0.65.
- **Sizing:** contracts = floor(0.5% equity / (ask × 100 + fees)). R = premium paid. Exit with the
  equity trade: stop, target, or the 15:55 forced exit.

## 8. Pass/fail bar and the honest math
**Pass (per test, per finalist). All of these must hold, after costs** *(v1.3: measured on the primary guardrail
config d2+w5; minimums are not relaxed if the guardrail makes them unreachable, see G5)*:
1. Concatenated OOS mean R per trade > 0, with the **95% day-block bootstrap CI lower bound > 0**.
   (5,000 resamples, seed 20260925, blocks = trading days, because trades within a day are correlated.)
2. Monthly return mean > 0, with the month-block bootstrap 95% CI lower bound > 0.
3. ≥ 500 OOS trades, and positive OOS mean R in ≥ 60% of folds.
4. Deflated Sharpe (daily returns, N = the test's trial count) ≥ 0.95.
5. **Holdout:** ≥ 100 trades, mean R > 0, and day-block CI lower bound > 0.
   - With fewer than 100 trades, the result is labeled underpowered and cannot pass.
6. Robustness, all required:
   - no single year contributes more than 50% of the total R;
   - the result stays positive with any one symbol removed;
   - the median of the ±1-step parameter neighborhood is still positive (a lone spike is a fail).

**Christian's targets:** 5–10 trades a day, a 60% win rate, 3–5% a month. *(v1.3: under the primary guardrail the
expected ceiling is ≈ 54 trades/mo at 60% WR [P]; see the G4 note.)* Monthly return ≈ trades/month
× E[R] × 0.5% (21 sessions):
| trades/day | trades/mo | E[R] for 3%/mo | E[R] for 5%/mo | win rate needed at 1:1 | at 2:1 |
|---|---|---|---|---|---|
| 5 | 105 | 0.057R | 0.095R | 52.9% / 54.8% | 35.2% / 36.5% |
| 10 | 210 | 0.029R | 0.048R | 51.4% / 52.4% | 34.3% / 34.9% |
- The edge needed is small. **Costs are the same size as that edge**:
  - Example SPY: ATR_d ≈ 1.2% and a ~0.3·ATR_d stop is ≈ 36 bp, so a 2–4 bp round trip is ≈ 0.06–0.11R.
  - At 5 trades a day, the strategy needs ≈ 0.12–0.21R gross per trade.
- A 60% win rate at 1:1 would mean +0.20R, which is ~10%/month at 5 trades a day. That would be
  exceptional, so the target set is internally loose: hitting 3–5% does not need 60%.
- The notional cap means stops under 0.5% risk less than 0.5%. The share of trades this affects is
  reported.
- **Readout** (`docs/intraday-sr/READOUT.md`, generated):
  - One block each for Test A, Test B, F_W, F_IHS, F_M, F_HS.
  - Columns: equity, plus options overlay per structure and book (daily | weekly, MODEL; O1).
  - Metrics: trades, trades/day, win rate, avg win/loss R, mean R [CI], monthly return [CI], max drawdown,
    Sharpe, DSR, % cap-limited, % stop-before-target-same-bar.
  - Robustness: per-year, per-symbol, and a parameter-neighborhood heatmap.
  - Sensitivity: 1.5× costs, skew ×2, exit IV −10%, $0.65 fees.
  - Every result vs. target is stated in words.
  - The trial count and grid hash appear on top.
  - *(v1.3: the 3-row guardrail table per finalist, primary d2+w5 / none / d2+w6, with trades/mo achieved; see G4.)*

## 9. Operational rules
- Research only:
  - No orders.
  - No calls to trading or account endpoints.
  - No edits to `src/` or `config/`.
  - Never touch running bots.
- **No pytest in the live checkouts under `/workspace`** (for example `/workspace/AlpacaOptionsCreditSpreadTrading`,
  `/workspace/AlpacaTradingBots`).
  - Use a separate worktree (for example `/workspace/tmp/wt-intraday-sr`), its own venv, or a cloud agent.
  - The repo has no CI workflow. Adding one is out of scope.
- Keys are read into process memory only, never printed, logged, or written. This follows Trading's
  pattern; a read-only data key is preferred.
- Rate limits are as in §2.3, and the cache is reused first (§2.1).
- Our own gap cache: `/workspace/research2/data/intraday_sr/` **[P]**, parquet, gitignored.
- Branch: `research/intraday-sr`. PRs are drafts titled `[research, do not merge]`, following this
  repo's convention.

## 10. Task split, order, and review checkpoints
| # | Owner | Task | Done when |
|---|---|---|---|
| S0 | Dev 2 + Dev 1 | `types.py`, `grids.py` (grid hash), synthetic fixtures (trend, range, W, IHS, gap, early close), test skeletons | Both developers approve; **CP0 Architect review** |
| D2-1 | Dev 2 | data layer: cache reader, RTH filter, adj factor, resample, calendar, VIX loader, `TimeOfDayLimiter` + gap puller | limiter tests pass with fake clocks (30/60 switch at 08:15/15:15 CT) |
| D2-2 | Dev 2 | levels (all §5 candidates) + numba indicators | lookahead tests (a)(b)(d) pass; **CP1** |
| D2-3 | Dev 2 | zones + scoring, signal stack | lookahead tests pass on zones and signals |
| D2-4 | Dev 2 | formations W/IHS/M/HS, Test B wiring | fixture patterns detected at the expected `confirmed_ts`; **CP2** |
| D1-1 | Dev 1 | guard, fills (§4.1–2), portfolio/risk incl. the v1.3 guardrail (G3), trial log, `guardrail_compare.parquet` | hand-computed fixture trades match exactly, including the G8 guardrail fixture; test (c) passes |
| D1-2 | Dev 1 | equity cost model + tiers | unit tests against the §7 tables; **CP3** |
| D1-3 | Dev 1 | walk-forward, selection, finalists, FREEZE, holdout lock | refuses holdout without FREEZE or with < 33 symbols |
| D1-4 | Dev 1 | options overlay (IV, skew, expiries, spread, fees) | BS parity checks; expiry calendar verified and cited |
| D1-5 | Dev 1 | stats (day/month block bootstrap, DSR) + readout generator | reproducible with seed |
| R1 | Dev 1 | **interim run** (Mon AM, completed symbols), primary config d2+w5 plus the two comparison rows | INTERIM readout; **CP4 Architect review before anything goes to Trading**, which also checks the guardrail logic against the G8 fixture and that no selection used a comparison config (G8) |
| R2 | Dev 1 | full development run → FREEZE.md | **CP5 Architect sign-off on the freeze** |
| R3 | Dev 1 | holdout, once → final READOUT.md | **CP6 Architect review**, then to Trading via the Team Manager |
| R4 | Dev 1 | forward-holdout scorer (G7): rate-limited pulls, shadow scoring under d2+w5, one read at ≥ 100 trades and ≥ 3 months | **CP7 Architect review** of the limiter settings before the first market-hours pull, and of the single read |
- Order: S0 first, then D2-1 ∥ D1-1/D1-2. D2-2/D2-3 come before R1. D2-4 and D1-4 can land after R1, and
  Test B, the formations and the options overlay then join the next interim run.
- Each Dev 2 module merges only with the lookahead tests green.
- At every checkpoint, the Architect reviews for lookahead (guard coverage, resample, pivots, fills) and
  overfitting (trial counts, grid adherence, untouched holdout).
- From CP4 on, every checkpoint also checks the G8 items: the guardrail logic against the fixture, and that no
  selection, freeze, pass decision or frontier flag used a comparison config.
