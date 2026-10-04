# Intraday S/R confluence research: spec v1.1 (frozen before any run)

Owner: Architect. Engine: Developer 2. Harness, walk-forward, costs, options overlay, readout: Developer 1.
Status: **research only, model-based, no live code.** Written Sun 2026-10-04 (CT). Values marked
**[P]** are provisional and may change only through a new spec version committed *before* the run
that uses them. Anything not marked is frozen.

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
- Total options variants = 9 (v1.0 overlay) + 9 (0DTE) = 18, all logged, never used to re-select equity finalists.

**A3. Loss guardrails (reporting overlay, not selection).**
- Implemented as `RiskCfg` flags: **d2** = no more entries for the day after 2 losing trades; **w5 / w6** = no more entries for the week after 5 / 6 losing trades.
- Combinations run: none, d2, w5, w6, d2+w5, d2+w6. They are applied to the same selected variants and frozen finalists (stocks and options) **after** selection, so they add no trials to N.
- Report per combination vs. none: trigger rate, and the change in trades/mo, monthly return, max DD, and worst week. Note that d2 directly limits the ~200 trades/mo goal.
- Promoting a guardrail into the selected configuration requires a new spec version before the holdout opens.

**A4. Ordering is unchanged.** CP4 review happens before any result goes to Trading. Developer 1 pings the Architect as soon as Test A has a first OOS read. After CP4 clears, the interim note goes to Trading, labeled INTERIM, holdout untouched.

## 0. Scope and non-goals
- Question: does a support/resistance confluence entry on 5m/15m, flat by the close, have a positive
  after-cost edge out of sample on a fixed liquid universe, as equities and as a 0–7 DTE long-options overlay?
- Not in scope: live or paper trading, changes under `src/alpaca_options_credit/`, any running bot,
  tuning toward Christian's targets. Results below target are reported plainly.

## 1. Universe (pre-declared, frozen once filled)
- Fixed list: **SPY, QQQ, IWM + 30 single names = 33 symbols.** Same list for every fold, the interim
  runs, and the holdout.
- **[PLACEHOLDER: filled verbatim from Trading's list file, path to be forwarded by Trading.]**
  The handoff (`/workspace/research2/intraday_lab_handoff.md`) and the cache contain *quarterly*
  point-in-time (PIT) top-30 lists (`research/intraday_lab/plans/universes.json`, 39 quarters,
  2017-01-03 … 2026-07-01), not one fixed list.
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
- Bad bars: drop bars with high < low, non-positive prices, or a close more than 8×ATR_5m from the prior
  close (count them in the readout). A missing 5m bar is left missing, never forward-filled for signals.

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
- Swing pivots on 5m/15m/1h/1d.
- PDH, PDL, PDC.
- ORH, ORL.
- HVN shelf edges and midpoints.
- Session VWAP.
- Round numbers on the as-traded price: step $1 if < $50, $5 if < $250, $10 if < $1,000, else $50.
- Candidates are taken within ±2 ATR_d of the last close. Premarket and overnight levels are deferred (§2.2).

**Zones:**
- Single-linkage clustering of candidates within `k_cluster × ATR_d`. The zone spans the min to max of its
  cluster, padded to at least 0.05 ATR_d.
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
- **Options overlay:** 9 variants, applied only to frozen equity finalists, never used to re-select them:
  - structure: long ATM, long 1 strike OTM, or a debit vertical (long ATM, short at the strike nearest the
    equity target);
  - DTE bucket: 0–1, 2–4, or 5–7.
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
- **Expiries (conservative [P]; Dev 1 verifies against Cboe listing history):**
  - Single names: Friday weeklies only.
  - SPY: M/W/F until 2022-11-13, daily from 2022-11-14.
  - QQQ and IWM: Fridays until 2022-12-31, then M/W/F/daily per verified history.
  - Each DTE bucket uses the nearest listed expiry inside it, or skips.
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
**Pass (per test, per finalist). All of these must hold, after costs:**
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

**Christian's targets:** 5–10 trades a day, a 60% win rate, 3–5% a month. Monthly return ≈ trades/month
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
  - Columns: equity, plus options overlay per structure and DTE (MODEL).
  - Metrics: trades, trades/day, win rate, avg win/loss R, mean R [CI], monthly return [CI], max drawdown,
    Sharpe, DSR, % cap-limited, % stop-before-target-same-bar.
  - Robustness: per-year, per-symbol, and a parameter-neighborhood heatmap.
  - Sensitivity: 1.5× costs, skew ×2, exit IV −10%, $0.65 fees.
  - Every result vs. target is stated in words.
  - The trial count and grid hash appear on top.

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
| D1-1 | Dev 1 | guard, fills (§4.1–2), portfolio/risk, trial log | hand-computed fixture trades match exactly; test (c) passes |
| D1-2 | Dev 1 | equity cost model + tiers | unit tests against the §7 tables; **CP3** |
| D1-3 | Dev 1 | walk-forward, selection, finalists, FREEZE, holdout lock | refuses holdout without FREEZE or with < 33 symbols |
| D1-4 | Dev 1 | options overlay (IV, skew, expiries, spread, fees) | BS parity checks; expiry calendar verified and cited |
| D1-5 | Dev 1 | stats (day/month block bootstrap, DSR) + readout generator | reproducible with seed |
| R1 | Dev 1 | **interim run** (Mon AM, completed symbols) | INTERIM readout; **CP4 Architect review before anything goes to Trading** |
| R2 | Dev 1 | full development run → FREEZE.md | **CP5 Architect sign-off on the freeze** |
| R3 | Dev 1 | holdout, once → final READOUT.md | **CP6 Architect review**, then to Trading via the Team Manager |
- Order: S0 first, then D2-1 ∥ D1-1/D1-2. D2-2/D2-3 come before R1. D2-4 and D1-4 can land after R1, and
  Test B, the formations and the options overlay then join the next interim run.
- Each Dev 2 module merges only with the lookahead tests green.
- At every checkpoint, the Architect reviews for lookahead (guard coverage, resample, pivots, fills) and
  overfitting (trial counts, grid adherence, untouched holdout).
