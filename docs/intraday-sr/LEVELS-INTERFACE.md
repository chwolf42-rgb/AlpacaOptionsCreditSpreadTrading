# Intraday S/R levels interface

Research only. This document is the contract between the level engine and
the harness (signal stack, options overlay). It is not a trading spec.
Nothing here places orders, reads credentials, or calls Alpaca.

`docs/intraday-sr/SPEC.md` was not in the tree when this interface was
published. Names below are the ones to rename against if the Architect's
spec lands with different identifiers.

Package: `research.intraday_sr.levels`.
Config: `research/intraday_sr/config/research.yaml`.
The live package `alpaca_options_credit` does not import this tree.

## No lookahead

Every emitted object carries `known_at`, the bar **close** at which that
version became knowable.

- A value computed at time T uses only bars whose close is at or before T.
- A swing pivot's `ref_time` is the pivot bar's close. Its `known_at` is
  the close of the last right-hand confirming bar. With `right >= 1` those
  timestamps differ.
- A daily pivot and any prior-day high, low, close, or prior-day VWAP
  close are knowable only after that session's 16:00 ET close.
- Premarket, overnight, and opening-range levels are knowable only once
  their window has ended. The opening range may also be emitted early, but
  only with `provisional=True` and `known_at` equal to the last opening-range
  bar already closed.
- Zone versions: `born_at` is the first close that published the zone id.
  `known_at` on each later version is the close that produced that version
  (a new touch, a score change, a flip). The id does not change. Truncation
  tests compare versions, not a mutated final object.

`LevelStudy.asof(t)` filters candidates, zone versions, formations, and
events to `known_at <= t`.

## Bar input

A research frame is a pandas DataFrame indexed by tz-aware bar **end**
time in `America/New_York`.

| Column | Required | dtype |
| --- | --- | --- |
| `open`, `high`, `low`, `close` | yes | float32 |
| `volume` | yes | float32 in memory |
| `vwap` | no | float32 |
| `trade_count` | no | int32 |
| `session` | after a cache load | `premarket`, `rth`, `afterhours` |

The strategy uses regular hours for pivots, the opening range, session
VWAP, and the volume profile. Extended-hours rows are used only for
premarket and overnight high/low. If they are absent those candidates
are simply not emitted.

### Alpaca cache timestamps are bar starts

Inspected file: ADBE 5-minute SIP, extended hours, 2016-11-01 through
2026-09-30, 249,956 rows (`part_0001.parquet`). Columns:

`ts` datetime64[us] naive, `symbol`, `open`, `high`, `low`, `close`
float32, `volume` float64, `trades` int32, `vwap` float32.

`ts` is the bar's **left edge**, America/New_York wall time, with no
timezone. That is Alpaca's convention. Evidence from this file:

- Clock labels run from 04:00 through 19:55. There is no 20:00 label and
  no 03:55 label. That is a left-labeled extended session `[04:00, 20:00)`.
- The 09:30 label has a high trade count (median about 1,600). That is the
  opening bar, not a bar that already closed at 09:30.
- The 16:00 label is a different object: median range about 0.03, median
  volume about 528k, median trades about 68, against about 3,700 trades
  on the 15:55 bar. That print is the closing auction sitting on the
  16:00 start label. It is not the last regular 5-minute bar (15:55–16:00)
  and it is not a normal after-hours bar.

`to_bar_close_index` / `load_cached_bars` do the following, in order:

1. Read naive `ts` as `America/New_York` wall time. Do not treat it as UTC.
2. Localize to that zone.
3. Add the bar width (5 minutes for this file) so the index is the close.
   The 09:30 start becomes 09:35. The 15:55 start becomes 16:00. The 09:25
   start becomes 09:30.
4. Rename `trades` to `trade_count`. Cast prices and volume to float32.
   Keep `vwap` when present.
5. Tag `session` from the **start** label, which the close-index still
   implies:
   - premarket: start in `[04:00, 09:30)`, so closes in `(04:00, 09:30]`
   - RTH: start in `[09:30, 16:00)`, so closes in `(09:30, 16:00]`
   - after-hours: start in `(16:00, 20:00)`, so closes in `(16:05, 20:00]`
6. A row whose start is exactly 16:00 ET is removed from the frame when
   `treat_1600_as_settlement` is true (the default). It is stored at
   `df.attrs["settlement"]`, indexed at 16:00 ET, and its knowable time
   is 16:00, not 16:05. Putting it on the main index would collide with
   the real 15:55–16:00 bar, which also closes at 16:00.

`load_bars_file` is the same conversion for one parquet or csv path.
`load_cached_bars(root, symbol, timeframe)` searches the root because the
production directory layout is not fixed yet:

- `{root}/{timeframe}/{symbol}.parquet` or `.csv`
- `{root}/{symbol}/{timeframe}.parquet` or `.csv`
- `{root}/{symbol}_{timeframe}.parquet` or `.csv`

Pass `bar_time=BarTimeConvention.END` only when the file is already
indexed by close. Daily cache files are expected to be close-indexed
already. The default daily path does not read a daily file; it derives
the day from 5-minute RTH bars.

### Resample

`resample_closed(rth_bars, "15m" | "1h")` builds higher timeframes from
RTH closes, anchored at 09:30 ET.

- A bucket is emitted only when the source bar that closes on the bucket
  end is present. Missing interior prints are dropped from the aggregate;
  the bucket is still not emitted early.
- 15-minute ends: 09:45, 10:00, …, 16:00.
- 1-hour ends: 10:30, 11:30, 12:30, 13:30, 14:30, 15:30. The 15:30–16:00
  remainder is thirty minutes and is not labeled 1h.

### Daily source

`DailyBarSource.completed_sessions(symbol, asof)` returns RTH daily OHLC
indexed at 16:00 ET, and only sessions whose close is at or before `asof`.

- `ResampledDailySource` is the default. It calls
  `derive_daily_from_intraday`. A session without the bar that closes at
  16:00 is omitted. The official close is the settlement print when the
  cache has one, otherwise the last RTH close. High and low include that
  print. Premarket and after-hours do not enter the daily bar.
- `NpzDailyBarSource` reads a simple float32 panel (`dates`, `symbols`,
  and `open/high/low/close/volume` shaped `(n_dates, n_symbols)`). The
  production point-in-time npz on the other machine is a different layout
  and needs an adapter that still implements this protocol. This package
  does not open that file.

## Session windows (America/New_York)

| Level | Window | Knowable |
| --- | --- | --- |
| Premarket high/low | `[04:00, 09:30)` | close of the bar that ends the window (normally 09:30). If that bar is missing, the next bar close after 09:30, and that later bar is not part of the range |
| Opening range | first `or_minutes` of RTH, default 15 (09:30–09:45) | the bucket-end close. Earlier prints may be emitted with `provisional=True` |
| Overnight high/low | previous settlement exclusive through 04:00 | the first bar close at or after 04:00. No extended bars means no candidate |
| Prior-day high/low/close | completed RTH session, plus settlement | that session's 16:00 close. Usable on later bars only |
| Session VWAP | RTH bars of the current session | each RTH bar close. Clustering keeps the latest value in the session, not the path |
| Prior-day VWAP close | last RTH VWAP of a completed session | that session's 16:00 close. Config `include_prior_vwap` |

Overnight is the prior after-hours session on this feed (starts after the
16:00 auction, last start label 19:55, close 20:00). It is not the
premarket window. Both degrade to "no candidate" when those rows are
absent.

## Candidates

```text
LevelCandidate(symbol, price, source, timeframe, ref_time, known_at, meta, provisional=False)
```

`source` is `LevelSource`:

`pivot_high`, `pivot_low`, `prior_day_high`, `prior_day_low`,
`prior_day_close`, `premarket_high`, `premarket_low`, `overnight_high`,
`overnight_low`, `or_high`, `or_low`, `hvn`, `vp_shelf`, `vwap`,
`round_number`.

Pivot timeframes: 5m, 15m, 1h (resampled from RTH 5m), and daily.
`left` and `right` default to 3. Confirmation is strict: a pivot high is
strictly greater than the `left` bars before it and the `right` bars
after it.

Round numbers use the configured steps `(1, 5, 10)`. The active step is
the coarsest step that is still `<= price / 10`, otherwise the finest
step. Levels inside `round_band_atr * ATR` of the bar close are emitted
once, with `known_at` on that bar.

Volume profile: a rolling `profile_sessions` (default 20) RTH profile,
updated only with bars already closed. Bin size is `profile_bin_size` or,
if unset, 0.25 / 0.50 / 1 / 2 by price band, frozen for the session from
the price at the session open. An HVN is a bin at or above
`profile_percentile` (default 0.70). A shelf is a contiguous run of those
bins; the candidate price is the run's midpoint. `meta` carries
`shelf_low` and `shelf_high`.

## Zones

```text
Zone(
    id, symbol, low, high, mid, side, score, components,
    touches, rejections, last_touch_at, volume_at_level, sources,
    known_at, timeframe, atr_at_known, broken_at, invalidated_at,
    flipped_at, side_history, born_at,
)
```

`last_touch` is a read-only alias of `last_touch_at`.

`side` is `support`, `resistance`, or `both`. Lows (pivot, prior day,
premarket, overnight, opening range) vote support. Highs vote resistance.
VWAP, round numbers, HVN, shelves, and prior-day close vote `both`. A
cluster with both support and resistance votes, or any `both` vote, is
`both`.

ATR is Wilder ATR(14) on `zone_timeframe` (default 5m) as of `known_at`.
`atr_at_known` stores that value. Candidates within `cluster_atr` (default
0.25) ATR are one zone, single-linkage on price.

Score components (`ScoreComponents`, also `as_dict()`):

```text
touch       = weight.touch * touches
rejection   = weight.rejection * rejections
recency     = weight.recency * exp(-bars_since_touch / half_life_bars)
volume      = weight.volume * log1p(volume_at_level / volume_unit)
confluence  = weight.confluence * max(0, n_distinct_sources - 1)
total       = touch + rejection + recency + volume + confluence
```

Default weights: touch 1, rejection 1.5, recency 1, volume 0.25,
confluence 1, half-life 78 bars, volume unit 100_000. Defaults for the
gate: `min_score` 3, `top_n` 8, `max_return` 32.

A touch is a bar whose range intersects `[low, high]`. A rejection is a
wick into the zone and a close back out: for support, `low <= high` of
the zone and `close > zone.high`; for resistance, `high >= zone.low` and
`close < zone.low`. `both` counts either pattern. Counts use only bars
already closed at this version's `known_at`.

Published set at time T, best score first:

- rank by `total` descending, ties by `id`;
- keep the zone if `rank < top_n` or `total >= min_score`;
- drop anything past `max_return`;
- drop a zone on bars after `invalidated_at`.

Role flip. A support zone breaks when a bar closes below `low`. A
resistance zone breaks when a bar closes above `high`. A `both` zone
becomes resistance on a close below `low` and support on a close above
`high`. The first such close sets `broken_at`. Each side change appends
to `side_history` and sets `flipped_at` to that close. A close more than
`invalidate_atr` (default 1) ATR beyond the broken boundary sets
`invalidated_at`. The version that first carries `invalidated_at` is
emitted once so the harness can see it, then later calls omit it.

`id` is assigned at birth and kept when a new cluster overlaps the
previous price interval (or its mid lies inside the cluster width). A
merge keeps the older id.

## Streaming API

```text
engine = LevelEngine(config, daily_source=None, history=False)
engine.update(symbol, bar)                  # one closed bar
engine.update_settlement(symbol, bar)       # 16:00 auction, optional
engine.update_frame(symbol, bars)           # replay in order
engine.levels_asof(symbol, t) -> list[Zone]
engine.nearest_zones(symbol, t, price) -> NearestZones
engine.candidates_asof(symbol, t)
engine.formations_asof(symbol, t)
engine.events_asof(symbol, t)

levels_asof(symbol, t, engine)
nearest_zones(symbol, t, price, engine)
run_symbol(bars, symbol, config, history=False) -> LevelStudy
run_universe(bars_by_symbol, config) -> dict[str, LevelStudy]
```

`update` is the incremental path and is what a bar-by-bar loop should
call. `levels_asof` is then a read of state already reduced to `t`. It is
not a rescan of the tape. The engine must not have been shown a bar that
closes after `t`; `run_symbol` on `bars.loc[:t]` is the batch way to ask
an arbitrary past question.

`nearest_zones` uses the published set at `t`. `below` is the zone with
the greatest `mid` still strictly under `price`. `above` is the least
`mid` strictly over `price`. A zone that contains `price` is not returned
as either target.

`history=True` keeps every published zone version inside `LevelStudy`.
The forward pass defaults to the latest set only. `run_universe` builds
one engine per symbol and drops it, so a 30-name batch does not hold
every tape at once.

## Formations

Built on 5m and 15m only, and only from confirmed pivots.

```text
Formation(
    id, symbol, kind, side, timeframe, points, neckline, zone_ref,
    known_at, extreme, measured_move_target,
)
Formation.neckline_price_at(t)
Formation.slope / anchor_price / anchor_time
```

`kind`: `double_bottom` (W), `double_top` (M), `inverse_hs`, `hs`.
`side`: `bullish` for W and inverse head-and-shoulders, `bearish` for M
and head-and-shoulders.

`points` are `FormationPoint(price, bar_time, known_at, kind)` with
`kind` `"high"` or `"low"`. `bar_time` is the pivot bar. `known_at` is
that pivot's confirmation close.

Neckline:

- W and M: horizontal (`slope_per_second == 0`) through the intervening
  extreme (the highest high between the two lows, or the lowest low
  between the two highs).
- Head-and-shoulders and inverse: the line through the two neckline
  pivots. `price_at(t) = anchor_price + slope_per_second * seconds`.

`extreme` is the lower W trough, the higher M peak, or the head.
`measured_move_target` is fixed when the formation is confirmed:

- bullish: `neckline_at_extreme + (neckline_at_extreme - extreme)`
- bearish: `neckline_at_extreme - (extreme - neckline_at_extreme)`

`zone_ref` is the nearest zone id at `known_at` whose side agrees
(support or both for bullish, resistance or both for bearish), else the
nearest zone of any side, else None.

Tolerances (`FormationConfig`): trough/peak equality within `atr_mult`
ATR (default 0.5); at least `min_bars_between` bars between adjacent
points (default 3); whole pattern within `max_bars_span` (default 80);
neckline separated from the extreme by `min_prominence_atr`; head above
or below both shoulders by `min_head_prominence_atr`.

## Formation events

```text
FormationEvent(
    kind, formation_id, price, bar_time, known_at,
    level_crossed, retest_extreme=None,
)
```

`kind`: `neckline_break`, `neckline_retest`, `invalidated`.

`bar_time` and `known_at` are both the event bar's close. `price` is that
close. `level_crossed` is `neckline_price_at(bar_time)`.

- Break: bullish close above the neckline, bearish close below it.
- Retest: within `retest_bars` (default 10) after the break, the bar tags
  the neckline within `retest_tolerance_atr * ATR` and closes back on the
  break side. `retest_extreme` is the retest bar's low when bullish and
  its high when bearish.
- Invalidated: before a break, a close through the extreme (below it when
  bullish, above it when bearish). After a break, a close back through
  the neckline. No break or retest is emitted after invalidation.

## Data budget

Recorded in `DataBudget` and `research.yaml`. This package never calls
Alpaca. Any later pull, on another machine, is capped at:

- 30 requests/minute on weekdays from 08:15 inclusive to 15:15 exclusive,
  America/Chicago;
- 60 requests/minute at all other times.

`DataBudget.limit_per_min(now)` is the lookup. Holidays that fall on a
weekday are not special-cased here.

## Universe and memory

The yaml list is SPY, QQQ, IWM plus liquid optionable large caps (about
thirty names). Trading may edit it. `load_universe()` reads it.

Production memory is about 5 GB shared with the live bots. Frames are
float32. `run_universe` processes one symbol and releases the engine
before the next. ATR has a numba loop when numba is installed and a
numpy fallback that matches it. The ADBE parquet and the handoff tarball
are local inputs only; they are not part of this repo.

## Open points for the Architect

- `SPEC.md` was absent. Align or rename against it when it arrives.
- The #40 daily npz layout is not in this checkout. `NpzDailyBarSource`
  documents a simple panel; the real adapter is still to write.
- Cache root layout is a search list, not a single pattern.
- Session VWAP excludes the 16:00 auction on purpose. Say if it should be
  included in the daily VWAP close.
- Cluster width is the 5-minute ATR for every source, including daily
  pivots. A separate daily ATR would change confluence.
- Weekday holidays use the 30/minute peak cap. Wire a calendar only if
  that is wrong.
