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
| Short strike | at/just beyond equity-style invalidation, rounded to a listed strike (not far-OTM lottery) |
| Width | `$5.00` (or `$2.50`) |
| DTE | 30–45 |
| Credit gate | skip if credit &lt; ~20% of width (natural: short bid − long ask) |
| Earnings / FOMC | skip new entries via `config/calendar.yaml` stub |
| Take profit | ~50% of credit (debit-to-close ≤ 50% of credit), software mark each poll |
| Stop | ~2× credit **or** underlying structure break, whichever first — **not** an equity OCO/bracket |
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

On TP, 2×-credit stop, or structure-break:

1. Latch `EXITING` + reason immediately (sticky even if the mark recovers).
2. Submit one debit-to-close mleg. Journal `CLOSED` only if the broker **accepts** the close (dry-run treats the recorded payload as success).
3. If submit raises or returns empty: stay `EXITING`, journal `close_failed`, **ERROR** log, increment `close_attempts`. Next poll retries. Paper ticks **block new entries** while any spread is `EXITING`.

Off-hours (`rth.manage_exits_off_hours: false`): options do not trade AH, so we **do not submit**. Open spreads are still flagged (`overnight_open` once per ET date + heartbeat `overnight_open=N`). Daily structure-break can latch overnight. The **first RTH poll always runs `_manage_exits` before any new entry**.

## Timeframe (locked: daily + 1Hour hybrid)

Daily bars own **trend bias** (strict HH/HL or LL/LH), the **VP shelf**, and the **S/R zone / invalidation**. 1Hour bars only **time** the entry: wait for the first pullback into that daily shelf or daily S/R while daily structure still holds, then require a 1H reconfirm of the daily trend (same-side HH/HL or LL/LH, last 1H turning with the trend). Do not chase an extended 1H print.

1H dips do **not** cancel an arm. Only a **daily** close through invalidation does. `timeframe.arm_timeout_bars` is counted in **daily structure bars** after confirm (not 1H bars, not calendar days). Daily VP lookback is ~20–30 sessions (`timeframe.volume_profile.lookback_bars`).

Journal reasons: `daily_not_confirmed`, `waiting_1h_pullback`, `1h_reconfirm_failed`, `chase`, `daily_structure_break`.

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

`--once` (on `run` / `observe`) ticks a single loop — useful for smoke tests.

`observe --fixture` writes `var/options-fixture/` (not the paper journal) and scans even outside RTH so the scaffold is runnable before keys arrive.

## Heartbeat watchdog and RTH

US **equity and ETF options** on Alpaca trade Regular Trading Hours **9:30–16:00 America/New_York**, Monday–Friday. Multi-leg orders do **not** accept `extended_hours=true`.

The engine writes `var/options/heartbeat.json` **every loop**, including off-hours (`status=idle_off_hours`), so overnight silence is not a crash. The supervisor:

- restarts if the child exits
- during RTH, restarts if the heartbeat is older than `heartbeat.stale_after_seconds_rth` (default 90s)
- off-hours, uses the longer `stale_after_seconds_off_hours` (default 600s)

Journal path: `var/options/journal.sqlite` (arms, open spreads, exit state, event log). Isolated from equity/crypto `var/` trees.

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

Coverage includes credential isolation, strike-near-invalidation, credit/width gate, TP/stop credit math, **engine TP / 2×-stop / structure-break close**, **failed-close retry + alert**, observer places no orders, max-loss sizing, one-spread-per-underlying, and the daily + 1H hybrid entry path.

## Config

See comments in [`config/default.yaml`](config/default.yaml). Knobs for width, DTE, credit gate, risk, roll stub, RTH, heartbeat, the locked daily + 1Hour hybrid (`structure_bar` / `timing_bar`), and the **locked options-native exit policy** (`exits.path`, `forbid_equity_oco_bracket`, `never_cancel_working_close`) live there.
