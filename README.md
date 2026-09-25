# AlpacaOptionsCreditSpreadTrading

Paper-only **options credit-spread** bot: **bull put credit** (bullish) and **bear call credit** (bearish). Dedicated Alpaca paper account (expect ~$100k when keyed).

This is a **sibling** of [AlpacaTradingBots](https://github.com/chwolf42-rgb/AlpacaTradingBots) (equity) and [AlpacaCryptoTrading](https://github.com/chwolf42-rgb/AlpacaCryptoTrading) (crypto). Separate repo, separate loops, separate SQLite journal, separate heartbeat, separate credentials. Do **not** share `.env`, `var/`, or process supervisors with those bots.

Keys are not in this repo. Copy `.env.example` → `.env` when they arrive. Missing secrets **fail closed** (no Alpaca client, no orders). Until then, `observe --fixture` runs the loop on synthetic bars and logs proposed strikes/credits with **zero orders**.

## Playbook (locked)

| Knob | Default |
| --- | --- |
| Structures | bull put credit + bear call credit only |
| Day-1 universe | SPY, QQQ, IWM, AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA (`universe.active: day1`) |
| Full A (57, **active**) | See [`config/universe.yaml`](config/universe.yaml); `universe.active: full_a` |
| Higher TF | **Daily + 1Hour hybrid** (locked; not 1H-only) |
| Entry | daily strict confirm → arm → **first 1H pullback** into the *daily* VP shelf **or** daily S/R, then **1H reconfirm** of the daily trend; **no chase** |
| Cancel arm | **daily** structure-break **close** through invalidation; 1H dips do **not** cancel |
| Short strike | nearest listed strike that clears `spreads.min_short_inv_gap` (default **1.0** point) beyond invalidation. Inside the gap skips `short_too_close_to_inv` (do not tighten back into invalidation) |
| Underwater open | no open and no openable proposal when the last **completed** daily close is already through invalidation (bull put: close ≥ inv, bear call: close ≤ inv). Skip `underwater_open_blocked` / `daily_close_through_inv` |
| Width | `$5.00` (or `$2.50`) |
| DTE | 30–45 |
| Credit gate | natural credit = short bid − long ask. Missing, crossed, absurd, or non-positive quotes skip **before** the proposal log (`quote_missing`, `quote_crossed`, `quote_absurd`, `credit_debit`). Positive credit still skips below ~20% of width (`credit_below_min_pct`). Optional wide-market and open-interest knobs default off. |
| Earnings / FOMC | skip new entries via `config/calendar.yaml` stub |
| Take profit | ~50% of credit (debit-to-close ≤ 50% of credit), software mark each poll |
| Stop | ~1.5× credit **or** underlying structure break, whichever first — **not** an equity OCO/bracket |
| Roll/repair | **stub** — close unless `exits.roll.execute` and thesis+DTE say otherwise |
| Size | max loss = width×100 − credit×100; ~0.5% of equity; one spread per underlying |

Exits are **options-native** (fraction of credit + structure-break). This is not the equity R-ladder and **must not** reuse that bot’s OCO/bracket stop path.

## Exits (options-native — no equity OCO)

Equity-sleeve stop bugs that **this bot is forbidden from growing**:

| Equity failure mode | Why it cannot apply here |
| --- | --- |
| Held OCO / bracket child legs | No `order_class=oco` or attached stop-limit. Open and close are a **single mleg** (two option legs, `position_intent` set). |
| `pending_cancel` limbo holding qty | We never cancel a working close to free qty. A live mleg close is left alone until it fills or the broker drops it (day TIF). |
| Cancel-before-replace naked window | `exits.never_cancel_working_close: true` is enforced. Replacement is only submitted if there is **no** working close id. |
| qty=60 vs leftover tranche fights | Close qty is the **journaled spread qty**, always. No child-stop remainder, no partial equity tranche. |

`config/default.yaml` locks `exits.path: credit_mark_and_structure` and `forbid_equity_oco_bracket: true`. `load_config` / `Engine` **refuse** equity stop keys (`oco`, `bracket`, …) and `broker.order_class` other than `mleg`.

**Atomic spread close only — no legging out.** Entry and exit submit `order_class=mleg` with **both** legs (short + long) in one order. Never close or cancel only the long or only the short of a healthy credit spread — that is how a naked short and a margin call happen. `exits.atomic_spread_only: true` is enforced. If a mleg only fills one side, that is **CRITICAL**: latch `naked_leg`, alert, and flatten the residual immediately (paired leftover via mleg, lone leftover via emergency flatten). Never leave a naked short resting.

On TP, 1.5×-credit stop, or structure-break:

1. Latch `EXITING` + reason immediately (sticky even if the mark recovers).
2. Submit one **2-leg** debit-to-close mleg.
3. Journal `CLOSED` only through `Journal.record_close`, which always writes `closed_at` and a `close_debit` when a price exists.
   - **Dry-run / observer:** the debit is the spread mid (short mid − long mid), the same mark that tripped the exit. Zero orders.
   - **Live paper:** wait for the mleg **fill**. `close_debit` is the order's net debit (`filled_avg_price`, not the quote) and `closed_at` is the fill time. A still-working order is left alone. A terminal partial fill increments `close_attempts`, keeps the filled contracts, and the next poll submits only the remainder. The stored debit is the qty-weighted average of those fills.
4. If submit raises or returns empty, or the order dies with no fill: stay `EXITING`, journal `close_failed`, **ERROR** log, increment `close_attempts`. Next poll retries. Paper ticks **block new entries** while any spread is `EXITING`.
5. If the close is real but no price can be read, the row still closes, `close_price_source` is `missing`, and a `close_price_missing` event plus a warning are written. Stats count that row in `n_missing_price`.

There is no separate expiry/DTE closer. The roll stub still closes through this same recorder when an exit fires. Off-hours only latches (`EXITING`); the first RTH poll submits, then records the fill. Naked-leg flattens also go through `record_close` (quote mid).

Off-hours (`rth.manage_exits_off_hours: false`): options do not trade AH, so we **do not submit**. Open spreads are still flagged (`overnight_open` once per ET date + heartbeat `overnight_open=N`). Daily structure-break can latch overnight. The **first RTH poll always runs `_manage_exits` before any new entry**.

## Timeframe (locked: daily + 1Hour hybrid)

Daily bars own **trend bias** (strict HH/HL or LL/LH), the **VP shelf**, and the **S/R zone / invalidation**. 1Hour bars only **time** the entry: wait for the first pullback into that daily shelf or daily S/R while daily structure still holds, then require a 1H reconfirm of the daily trend (same-side HH/HL or LL/LH, last 1H turning with the trend). Do not chase an extended 1H print.

1H dips do **not** cancel an arm. Only a **daily** close through invalidation does. `timeframe.arm_timeout_bars` is counted in **daily structure bars** after confirm (not 1H bars, not calendar days). Daily VP lookback is ~20–30 sessions (`timeframe.volume_profile.lookback_bars`).

Journal reasons: `daily_not_confirmed`, `waiting_1h_pullback`, `1h_reconfirm_failed`, `chase`, `daily_structure_break`.

An arm can sit for days. Before strikes are chosen and again before an observer fill or live open, the last completed daily close must still hold the armed invalidation (bull put `close >= invalidation`, bear call `close <= invalidation`). A forming session bar does not hide a prior completed close that already went through. That blocks the EEM-style open (armed with inv 68.41, later filled while the completed daily close was already under it). 1H dips still do not cancel the arm. `exits.honor_structure_break` is a separate exit and is unchanged.

The short strike has to clear `spreads.min_short_inv_gap` (default 1.0) so structure-break and short-ITM are not the same print. Bull put: `short <= inv - gap`. Bear call: `short >= inv + gap`. EEM short 68 vs inv 68.41 (gap 0.41) fails a 1.0 gap. If no listed strike clears it, the skip is `short_too_close_to_inv`.

Credit-spread construction (bull put / bear call, 30–45 DTE) and options-native exits are unchanged. Observer / dry-run still places **zero** orders.

## Universe (config swap, not a code change)

Lists live in [`config/universe.yaml`](config/universe.yaml) as two tiers:

| `universe.active` | Names | When |
| --- | --- | --- |
| `day1` | 10 megas/indexes: SPY, QQQ, IWM, AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA | Optional shrink-back sleeve |
| `full_a` (default) | 57 liquid A names (AAPL … ADBE, including GLD/SLV/TLT and the sector ETFs) | **Active** paper dry-run universe |

Flip in [`config/default.yaml`](config/default.yaml):

```yaml
universe:
  file: config/universe.yaml
  active: full_a    # or day1 to shrink
```

`risk.max_concurrent` is **20** on this sleeve. No Python changes.

## Observe vs supervise vs paper

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# No keys: synthetic bars, proposed mleg payloads logged, zero orders.
alpaca-options-credit observe --fixture --once

# With options paper keys in .env: live bars + chains, still zero orders.
alpaca-options-credit observe --config config/default.yaml

# Parent process: restarts `run` on child death or stale heartbeat.
alpaca-options-credit supervise --dry-run --fixture

# Paper orders (fail closed without secrets; paper host only).
alpaca-options-credit run --config config/default.yaml
```

| Command | Orders | Data | Restarts |
| --- | --- | --- | --- |
| `observe --fixture` | never | in-process fixture | no |
| `observe` | never | Alpaca stock bars + option contracts/snapshots | no |
| `run --dry-run` | never | Alpaca | no |
| `supervise --dry-run` | never | child as above | **yes** |
| `run` (no dry-run) | paper mleg | Alpaca | no |
| `supervise` (no dry-run) | paper mleg | Alpaca | **yes** |
| `backfill-close-debits` | never (writes journal debits only) | historical option quotes/bars | no |

`--once` (on `run` / `observe`) ticks a single loop — useful for smoke tests.

`observe --fixture` writes `var/options-fixture/` (not the paper journal) and scans even outside RTH so the scaffold is runnable before keys arrive.

## Heartbeat watchdog and RTH

US **equity and ETF options** on Alpaca trade Regular Trading Hours **9:30–16:00 America/New_York**, Monday–Friday. Multi-leg orders do **not** accept `extended_hours=true`.

The engine writes `var/options/heartbeat.json` **every loop**, including off-hours (`status=idle_off_hours`), so overnight silence is not a crash. The supervisor:

- restarts if the child exits
- during RTH, restarts if the heartbeat is older than `heartbeat.stale_after_seconds_rth` (default 90s)
- off-hours, uses the longer `stale_after_seconds_off_hours` (default 600s)

Journal path: `var/options/journal.sqlite` (arms, open spreads, exit state, event log). Isolated from equity/crypto `var/` trees.

Closed spreads store `exit_reason`, `closed_at`, `close_debit` (per-spread debit, premium points), and `close_price_source` (`quote`, `fill`, `backfill`, or `missing`). `Journal.summarize_managed_outcomes(start, end)` is the EOD query: managed win rate (take-profit vs credit-stop / structure-break), average win, average loss, `pnl`, `n_wins`, `n_losses`, `n_estimated`, and `n_missing_price` for an inclusive America/New_York calendar-day range. Pass `session="rth"` to keep only weekday 09:30–16:00 ET closes. Dollars are `(credit - close_debit) × qty × multiplier` (default multiplier 100). Backfilled rows are included and counted in `n_estimated`. A stop journaled before the debit column existed still counts; the helper implies the locked 1.5× stop and 50% take-profit when `close_debit` is missing, and those rows are still reported in `n_missing_price`. A structure exit with no debit counts as a loss and is left out of `avg_loss` / `pnl`. Legacy `stop_2x_credit` rows count as managed losses. `naked_leg` is not a managed outcome. Dry-run writes the same close row and still submits zero broker orders.

### Backfill close debits

Closes journaled before `close_debit` existed (or any closed row still NULL) can be filled from historical option quotes, then minute bars, at `closed_at` or, when that is empty, `updated_at`. The debit is short price − long price. The row is tagged `close_price_source=backfill` and a `close_debit_backfilled` event with `estimated: true`. Rows that already have `close_debit` are never touched. `--dry-run` previews and writes nothing. This command does **not** read `bot.dry_run` and does not flip the engine into paper orders.

```bash
# Preview only.
python -m alpaca_options_credit backfill-close-debits --dry-run

# Write the paper journal (needs OPTIONS_APCA_* keys).
python -m alpaca_options_credit backfill-close-debits --config config/default.yaml

# Another sqlite file.
python -m alpaca_options_credit backfill-close-debits --journal var/options/journal.sqlite
```

## Credentials (isolation)

Precedence: **`OPTIONS_APCA_*` overrides inherited `APCA_*`**.

| Variable | Role |
| --- | --- |
| `OPTIONS_APCA_API_KEY_ID` / `OPTIONS_APCA_API_SECRET_KEY` | This bot’s paper keys |
| `OPTIONS_APCA_API_BASE_URL` | Must be `https://paper-api.alpaca.markets` |
| `OPTIONS_APCA_DATA_URL` | Default `https://data.alpaca.markets` |
| `OPTIONS_APCA_EXPECTED_ACCOUNT_NUMBER` or `APCA_EXPECTED_ACCOUNT_NUMBER` | Optional; refuse if `GET /v2/account` mismatches |
| `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` | Inherited fallback only |
| `EQUITY_APCA_API_KEY_ID` | If set and equal to the resolved key → **refuse** |
| `CRYPTO_APCA_API_KEY_ID` | If set and equal to the resolved key → **refuse** |

Live `https://api.alpaca.markets` is refused. Secrets are never logged.

## Alpaca API notes (encapsulated in `broker/`)

Investigated against current **alpaca-py** + Trading API:

- **Open credit spread:** `POST /v2/orders` with `order_class=mleg`, `type=limit`, two legs, `time_in_force=day`.
- **Signed `limit_price`:** Alpaca treats a **positive** mleg limit as a **debit** and a **negative** limit as a **credit**. We send `-credit` on entry and `+debit` on close. (This is easy to get backwards; payload tests lock it.)
- **Legs:** `sell_to_open` the short strike, `buy_to_open` the long (further OTM) strike. Close flips to `buy_to_close` / `sell_to_close`.
- **Contracts:** `GET /v2/options/contracts` via `TradingClient.get_option_contracts`.
- **Quotes:** `OptionHistoricalDataClient.get_option_snapshot` (batch). Config default feed is **`indicative`** so paper works without an OPRA subscription; set `market_data.options_feed: opra` if entitled.
- **Underlying bars:** `StockHistoricalDataClient.get_stock_bars`. Default feed **`iex`** (free paper); `sip` needs a paid plan.
- **Deviation vs some Alpaca sample notebooks:** samples often use `MarketOrderRequest` for mlegs. This bot uses **limit** mlegs so the credit/debit is explicit. We also always set `position_intent` (not only `side`).
- Engine code talks to **dicts** from `broker/payloads.py`. Only `broker/alpaca.py` imports alpaca-py.

## Path when keys arrive

1. Create a **dedicated** Alpaca paper account (do not reuse equity or crypto keys).
2. `cp .env.example .env` and fill `OPTIONS_APCA_*`. Optionally set `EQUITY_APCA_API_KEY_ID` / `CRYPTO_APCA_API_KEY_ID` so a copy-paste mistake fails closed.
3. `alpaca-options-credit observe` — live data, **no orders**, watch logs for arms/proposals.
4. `alpaca-options-credit supervise --dry-run` — same, with restart/heartbeat.
5. When proposals look sane: `alpaca-options-credit supervise` (paper mleg). Keep `bot.dry_run: false` only then.

## Tests

```bash
pytest
```

Coverage includes credential isolation, strike-near-invalidation, credit/width gate, TP/stop credit math, **engine TP / 1.5×-stop / structure-break close**, **failed-close retry + alert**, the EOD managed win/loss summary, observer places no orders, max-loss sizing, one-spread-per-underlying, and the daily + 1H hybrid entry path.

## Config

See comments in [`config/default.yaml`](config/default.yaml). Knobs for width, DTE, credit gate, risk, roll stub, RTH, heartbeat, the locked daily + 1Hour hybrid (`structure_bar` / `timing_bar`), and the **locked options-native exit policy** (`exits.path`, `forbid_equity_oco_bracket`, `never_cancel_working_close`) live there.
