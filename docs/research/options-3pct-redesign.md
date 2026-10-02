# Credit-spread redesign: 3% a month

No credit-spread design that keeps the locked rules makes 3% a month. Nothing in the precommitted search had a train-window monthly return whose 95% interval sat entirely above zero, together with an expectancy per unit of max risk that also sat entirely above zero, on at least 30 spreads. There is no winner to take out of sample. The live book on the untouched test window (2025-07-01 to 2026-09-30), capped at 5 spreads and 10% open risk, returned -0.46% [-0.62%, -0.33%] per month on the $100k sleeve, max drawdown $7,001 (7.0%), 8.20 trades/month, n=123. On the 2026-10-02 CBOE chain, a 10–20 delta $5 or $10 vertical clears the 20% natural-credit gate 0% of the time (median natural credit about 5–13% of width). A 25 delta $5 wing clears only about 4–25% of quotes. A 30 delta $5 wing often clears on SPY, QQQ, and IWM (about 55–72%), but the replay still skips 96.7% of those ready signals, because the short also has to sit at least $1 past invalidation and within 0.08 delta of the target, and the booked 30-delta strategy still loses money. The one order that did clear the gate was a day limit at an indicative natural credit, and it expired.

## Why the paper bot does not get filled

The supervisor's ~1,815 `credit_below_min_pct` skips and the single unfilled order are the same gate.

The live rule sells the natural credit, short bid minus long ask, and refuses the spread unless that credit is at least 20% of the width. On a $5 width that is $1.00. The limit sent to Alpaca is that same natural credit (`credit_limit_price` is the negative of the credit, which means "this much credit or more"), time in force day. The quote feed in `config/default.yaml` is `indicative`, not OPRA. An indicative natural that is richer than the real NBBO never trades, and a day order then expires. The September 30 MSFT 490/485 put, 1 lot, limit credit 1.12, is 22.4% of a $5 width: it cleared the gate by twelve cents and was not filled.

The strike the bot actually picks is the listed strike just beyond invalidation, not a chosen delta. When that strike is near the money the credit can clear $1.00, and the prior replay's filled baseline sat near a 0.37 delta. When the shelf is further out, the same $5 width does not pay $1.00 after the bid/ask, and the skip token is `credit_below_min_pct`. A redesign that aims at 10, 16, or 20 delta keeps the shelf entry and then finds the credit gate shut. Twenty-five delta clears only a minority of quotes. Thirty delta clears more often on index $5 wings, and that book still loses.

Replay skip counts on the full tape (a credit skip is a ready signal whose modeled quote failed the gate or whose listed delta was more than 0.08 through the target):

| Design | Ready | Filter reject | Credit or strike skip | Opened | Gate share of unfiltered |
| --- | ---: | ---: | ---: | ---: | ---: |
| base | 4469 | 0 | 4111 | 358 | 92.0% |
| d16_c20 | 5370 | 0 | 5370 | 0 | 100.0% |
| d20_c20 | 5367 | 0 | 5364 | 3 | 99.9% |
| d30_c20 | 4955 | 0 | 4792 | 163 | 96.7% |
| d16_c10 | 4675 | 0 | 4398 | 277 | 94.1% |
| base_dte7 | 5222 | 0 | 5136 | 86 | 98.4% |
| idx_base | 5309 | 5215 | 62 | 32 | 66.0% |

MSFT's Yahoo daily close on 2026-09-30 was $512.90. The 490 short was 4.5% out of the money versus that close. A $5-wide put at that distance is a low-delta vertical. A $1.12 natural credit is only just over the $1.00 floor, so the order was the rare quote that passed and it still did not trade.

## The 3% ceiling under the locked rules

With 50% take-profit and risk sized at 0.5% of equity, the account return is an identity of the credit fraction and the number of round trips. A win pays `(0.50 × credit) / (width − credit)` times the risk budget. At a credit of 20% of width that multiple is 0.125, so a win adds 0.0625% of the account. Forty-eight wins and zero losses in a month are what it takes to make 3% at that credit. Five open spreads cannot turn over that fast unless they are closed in a couple of days, and a book of 20% credits does not win every time.

The table is the monthly return if every trade wins and none loses. It is the ceiling, not a forecast. A single full loss costs 0.5% of the account and wipes out eight of those 20%-credit wins.

| Credit / width | Win, as a fraction of max loss | 8 perfect trades | 15 perfect trades | 48 perfect trades |
| ---: | ---: | ---: | ---: | ---: |
| 10% | 0.056 | 0.22% | 0.42% | 1.33% |
| 20% | 0.125 | 0.50% | 0.94% | 3.00% |
| 30% | 0.214 | 0.86% | 1.61% | 5.14% |
| 40% | 0.333 | 1.33% | 2.50% | 8.00% |
| 50% | 0.500 | 2.00% | 3.75% | 12.00% |

A $10-wide spread does not loosen this. At 0.5% of $100k the risk budget is $500. A $10 wing with a $2 credit still has $800 of max loss, so the sizer takes zero contracts. Widening the wing under the locked risk budget does not create a position unless the credit is at least half the width.

## How a design was allowed to win

Train entries are 2024-01-02 through 2025-06-30. The test window is 2025-07-01 through 2026-09-30 and was frozen before the replay. October 2026 is a partial month and is in neither window. Walk-forward folds, also frozen, are: train through 2024-06-30 then 2024-07-01–2024-12-31, train through 2024-12-31 then 2025-01-02–2025-06-30, train through 2025-06-30 then 2025-07-01–2025-12-31, train through 2025-12-31 then 2026-01-02–2026-09-30.

A searchable row needs at least 30 train spreads. The 95% bootstrap interval on its mean monthly return (month P&L divided by the fixed $100,000, months with no exit included as zero) and the interval on expectancy divided by max loss both have to sit entirely above zero. The winner is the qualifying row with the highest train monthly return. The test window is then one look. Decision rows (a 10% credit gate, a mid fill, a 25% take-profit) are not eligible.

The entry is the live one: a daily strict confirm, an arm, then the first hourly pullback into the daily volume-profile shelf and an hourly reconfirm. No chase. One spread per name inside the replay. The reported book then keeps at most 5 names, 0.5% of realized equity, and 10% open risk. A skipped signal does not invent a later replacement.

The position path is one continuous replay. A spread opened before a window can still occupy that name, so the first signal of a test window is not a flat start. That is the same path the paper bot would have if it had been running. It is not a look at test P&L. The selector's inputs are train rows only; the function has no test argument.

Intervals are 5,000 percentile-bootstrap resamples, seed 20261002, taking the sorted sample at 2.5% and 97.5%. Fees are Alpaca's September 1, 2026 retail schedule: no commission on equity and ETF options, plus ORF $0.015, OCC $0.025, and CAT $0.0003 per contract-side, TAF $0.00329 per contract on sells, and the SEC fee at $0.0000206 of an approximated sell principal. Each spread's fee is rounded up to the next cent.

Primary fills are the natural credit (short bid minus long ask) and the natural debit on a stop or a structure break. The take-profit fill is a debit of half the credit. The half-spread on each leg is 6% of the mid, at least $0.05 and at most $0.25. IV is the last 20 sessions of close-to-close realized vol times 1.15, frozen at entry, clamped from 15% to 125%. Rate 4%, dividend zero. Earnings blackout is off, matching the empty `config/calendar.yaml`.

## Train window

| Design | Trades | Trades/month | Monthly return (95% CI) | Worst month | Positive months | Max drawdown | Win rate (95% CI) | Expectancy $ (95% CI) | Expectancy / max risk (95% CI) |
| --- | ---: | ---: | --- | ---: | ---: | --- | --- | --- | --- |
| base | 148 | 8.22 | -0.45% [-0.58%, -0.31%] | -1.07% | 11% | $8,323 (8.3%) | 41.2% [33.1%, 49.3%] | -$55 [-$71, -$38] | -14.6% [-18.9%, -10.1%] |
| base_dte7 | 48 | 2.67 | -0.14% [-0.21%, -0.06%] | -0.45% | 11% | $2,436 (2.4%) | 39.6% [25.0%, 54.2%] | -$51 [-$79, -$24] | -13.7% [-21.0%, -6.6%] |
| base_dte21 | 113 | 6.28 | -0.31% [-0.45%, -0.18%] | -0.90% | 11% | $5,661 (5.7%) | 44.2% [35.4%, 53.1%] | -$49 [-$67, -$30] | -13.3% [-18.1%, -8.2%] |
| base_w10 | 0 | 0.00 | n/a | n/a | n/a | $0 (0.0%) | n/a | n/a | n/a |
| d10_c20 | 0 | 0.00 | n/a | n/a | n/a | $0 (0.0%) | n/a | n/a | n/a |
| d16_c20 | 0 | 0.00 | n/a | n/a | n/a | $0 (0.0%) | n/a | n/a | n/a |
| d20_c20 | 1 | 0.06 | 0.00% [0.00%, 0.01%] | 0.00% | 6% | $0 (0.0%) | 100.0% [100.0%, 100.0%] | $54 [$54, $54] | 13.8% [13.8%, 13.8%] |
| d25_c20 | 12 | 0.67 | 0.02% [-0.01%, 0.05%] | -0.13% | 33% | $158 (0.2%) | 83.3% [58.3%, 100.0%] | $27 [-$11, $55] | 7.0% [-2.9%, 14.0%] |
| d30_c20 | 84 | 4.67 | -0.15% [-0.25%, -0.05%] | -0.68% | 28% | $3,016 (3.0%) | 51.2% [40.5%, 61.9%] | -$33 [-$54, -$14] | -8.4% [-13.6%, -3.4%] |
| d20_c20_dte7 | 0 | 0.00 | n/a | n/a | n/a | $0 (0.0%) | n/a | n/a | n/a |
| d20_c20_dte21 | 0 | 0.00 | n/a | n/a | n/a | $0 (0.0%) | n/a | n/a | n/a |
| d30_c20_dte7 | 17 | 0.94 | -0.03% [-0.07%, 0.01%] | -0.21% | 17% | $584 (0.6%) | 47.1% [23.5%, 70.6%] | -$28 [-$71, $11] | -7.2% [-18.0%, 2.8%] |
| d30_c20_dte21 | 54 | 3.00 | -0.08% [-0.17%, -0.00%] | -0.56% | 39% | $1,760 (1.8%) | 51.9% [38.9%, 64.8%] | -$27 [-$51, -$3] | -6.8% [-13.1%, -0.7%] |
| d20_c20_w10 | 0 | 0.00 | n/a | n/a | n/a | $0 (0.0%) | n/a | n/a | n/a |
| d30_c20_w10 | 0 | 0.00 | n/a | n/a | n/a | $0 (0.0%) | n/a | n/a | n/a |
| idx_base | 17 | 0.94 | -0.02% [-0.06%, 0.02%] | -0.19% | 33% | $510 (0.5%) | 58.8% [35.3%, 82.4%] | -$21 [-$70, $26] | -5.5% [-18.5%, 6.9%] |
| mega_base | 41 | 2.28 | -0.16% [-0.25%, -0.07%] | -0.51% | 22% | $2,911 (2.9%) | 39.0% [24.4%, 53.7%] | -$70 [-$102, -$36] | -18.2% [-26.6%, -9.4%] |
| idx_d30_c20 | 4 | 0.22 | -0.01% [-0.03%, 0.01%] | -0.12% | 11% | $225 (0.2%) | 50.0% [0.0%, 100.0%] | -$31 [-$113, $51] | -7.9% [-28.7%, 12.8%] |
| mega_d30_c20 | 30 | 1.67 | -0.07% [-0.14%, 0.00%] | -0.49% | 33% | $1,340 (1.3%) | 50.0% [33.3%, 66.7%] | -$41 [-$77, -$6] | -10.4% [-19.4%, -1.3%] |
| base_ema | 113 | 6.28 | -0.33% [-0.48%, -0.19%] | -1.07% | 11% | $6,394 (6.4%) | 41.6% [32.7%, 50.4%] | -$53 [-$71, -$34] | -14.0% [-18.8%, -9.2%] |
| base_iv50 | 108 | 6.00 | -0.35% [-0.44%, -0.23%] | -0.65% | 6% | $6,353 (6.3%) | 38.0% [28.7%, 47.2%] | -$58 [-$77, -$39] | -15.4% [-20.5%, -10.4%] |
| d30_c20_ema | 64 | 3.56 | -0.12% [-0.22%, -0.02%] | -0.68% | 33% | $2,417 (2.4%) | 50.0% [37.5%, 62.5%] | -$34 [-$57, -$11] | -8.6% [-14.5%, -2.8%] |
| d30_c20_iv50 | 60 | 3.33 | -0.12% [-0.20%, -0.04%] | -0.49% | 28% | $2,427 (2.4%) | 50.0% [36.7%, 61.7%] | -$36 [-$61, -$13] | -9.2% [-15.6%, -3.2%] |
| base_stop2 | 149 | 8.28 | -0.43% [-0.56%, -0.30%] | -1.02% | 11% | $8,076 (8.1%) | 40.3% [32.9%, 47.7%] | -$53 [-$68, -$38] | -14.0% [-18.0%, -10.1%] |
| d30_c20_stop2 | 84 | 4.67 | -0.16% [-0.26%, -0.05%] | -0.64% | 28% | $3,069 (3.1%) | 50.0% [39.3%, 60.7%] | -$33 [-$53, -$14] | -8.5% [-13.5%, -3.6%] |

### What each searchable row is

- `base` — Live book. Nearest short past the $1 gap, $5 wide, 30–45 DTE, natural credit at least 20% of width, 50% take-profit, no price stop, structure break.
- `base_dte7` — Same live short, $5 wide, Friday expiration in 7–14 DTE, 20% credit gate, 50% take-profit.
- `base_dte21` — Same live short, $5 wide, Friday expiration in 21–30 DTE, 20% credit gate, 50% take-profit.
- `base_w10` — Same live short, $10 wide, 30–45 DTE, 20% credit gate, 50% take-profit.
- `d10_c20` — Short nearest 10 delta beyond invalidation, $5 wide, 30–45 DTE, 20% credit gate, 50% take-profit. A listed strike more than 0.08 above the target is skipped.
- `d16_c20` — Short nearest 16 delta beyond invalidation, $5 wide, 30–45 DTE, 20% credit gate, 50% take-profit. A listed strike more than 0.08 above the target is skipped.
- `d20_c20` — Short nearest 20 delta beyond invalidation, $5 wide, 30–45 DTE, 20% credit gate, 50% take-profit. A listed strike more than 0.08 above the target is skipped.
- `d25_c20` — Short nearest 25 delta beyond invalidation, $5 wide, 30–45 DTE, 20% credit gate, 50% take-profit. A listed strike more than 0.08 above the target is skipped.
- `d30_c20` — Short nearest 30 delta beyond invalidation, $5 wide, 30–45 DTE, 20% credit gate, 50% take-profit. A listed strike more than 0.08 above the target is skipped.
- `d20_c20_dte7` — 20 delta, $5 wide, 7 DTE window, 20% credit gate, 50% take-profit.
- `d20_c20_dte21` — 20 delta, $5 wide, 21 DTE window, 20% credit gate, 50% take-profit.
- `d30_c20_dte7` — 30 delta, $5 wide, 7 DTE window, 20% credit gate, 50% take-profit.
- `d30_c20_dte21` — 30 delta, $5 wide, 21 DTE window, 20% credit gate, 50% take-profit.
- `d20_c20_w10` — 20 delta, $10 wide, 30–45 DTE, 20% credit gate, 50% take-profit.
- `d30_c20_w10` — 30 delta, $10 wide, 30–45 DTE, 20% credit gate, 50% take-profit.
- `idx_base` — Live book restricted to SPY, QQQ, and IWM.
- `mega_base` — Live book restricted to AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA.
- `idx_d30_c20` — 30 delta, 20% credit gate, SPY, QQQ, and IWM only.
- `mega_d30_c20` — 30 delta, 20% credit gate, the seven liquid single names only.
- `base_ema` — Live book, bull puts only above the daily EMA50 and bear calls only below it.
- `base_iv50` — Live book, sell only when the 20-day IV proxy is at or above its prior-252-session percentile of 50.
- `d30_c20_ema` — 30 delta and the 20% gate, plus the daily EMA50 trend filter.
- `d30_c20_iv50` — 30 delta and the 20% gate, plus IV-proxy percentile at least 50.
- `base_stop2` — Live book with a 2× credit stop judged on the hourly close. Take-profit stays 50%. Structure break stays.
- `d30_c20_stop2` — 30 delta, 20% gate, 2× credit stop on the hourly close, 50% take-profit.

## Selection

No searchable design had 30 or more train trades and both the monthly-return interval and the expectancy/max-risk interval entirely above zero.

No train row cleared the bar. The test table is descriptive. It is not a second chance to pick.

## Out-of-sample test

Entries from 2025-07-01 through 2026-09-30. These rows were not used to pick a winner. A test row that looks better than the train rule is not adopted.

| Design | Trades | Trades/month | Monthly return (95% CI) | Worst month | Positive months | Max drawdown | Win rate (95% CI) | Expectancy $ (95% CI) | Expectancy / max risk (95% CI) |
| --- | ---: | ---: | --- | ---: | ---: | --- | --- | --- | --- |
| base | 123 | 8.20 | -0.46% [-0.62%, -0.33%] | -1.23% | 0% | $7,001 (7.0%) | 43.9% [35.0%, 52.8%] | -$57 [-$76, -$38] | -15.2% [-20.2%, -10.1%] |
| base_dte7 | 36 | 2.40 | -0.14% [-0.21%, -0.07%] | -0.45% | 13% | $2,181 (2.2%) | 41.7% [25.0%, 58.3%] | -$56 [-$95, -$19] | -14.6% [-24.6%, -5.1%] |
| base_dte21 | 104 | 6.93 | -0.26% [-0.42%, -0.08%] | -0.77% | 20% | $4,213 (4.2%) | 51.0% [41.3%, 60.6%] | -$38 [-$57, -$18] | -10.3% [-15.6%, -5.0%] |
| base_w10 | 1 | 0.07 | -0.00% [-0.01%, 0.00%] | -0.07% | 0% | $74 (0.1%) | 0.0% [0.0%, 0.0%] | -$74 [-$74, -$74] | -21.5% [-21.5%, -21.5%] |
| d10_c20 | 0 | 0.00 | n/a | n/a | n/a | $0 (0.0%) | n/a | n/a | n/a |
| d16_c20 | 0 | 0.00 | n/a | n/a | n/a | $0 (0.0%) | n/a | n/a | n/a |
| d20_c20 | 2 | 0.13 | -0.01% [-0.04%, 0.00%] | -0.14% | 0% | $203 (0.2%) | 0.0% [0.0%, 0.0%] | -$101 [-$136, -$67] | -25.7% [-34.5%, -16.9%] |
| d25_c20 | 11 | 0.73 | -0.06% [-0.13%, 0.01%] | -0.45% | 13% | $966 (1.0%) | 27.3% [0.0%, 54.5%] | -$78 [-$124, -$24] | -20.6% [-32.4%, -6.4%] |
| d30_c20 | 63 | 4.20 | -0.21% [-0.34%, -0.08%] | -0.75% | 13% | $3,289 (3.3%) | 44.4% [31.7%, 57.1%] | -$49 [-$74, -$25] | -12.8% [-19.3%, -6.5%] |
| d20_c20_dte7 | 0 | 0.00 | n/a | n/a | n/a | $0 (0.0%) | n/a | n/a | n/a |
| d20_c20_dte21 | 0 | 0.00 | n/a | n/a | n/a | $0 (0.0%) | n/a | n/a | n/a |
| d30_c20_dte7 | 11 | 0.73 | -0.03% [-0.09%, 0.03%] | -0.32% | 27% | $489 (0.5%) | 54.5% [27.3%, 81.8%] | -$35 [-$105, $27] | -9.1% [-27.2%, 6.8%] |
| d30_c20_dte21 | 46 | 3.07 | -0.09% [-0.20%, 0.01%] | -0.58% | 33% | $1,821 (1.8%) | 54.3% [41.3%, 69.6%] | -$30 [-$58, -$1] | -7.6% [-15.0%, -0.3%] |
| d20_c20_w10 | 0 | 0.00 | n/a | n/a | n/a | $0 (0.0%) | n/a | n/a | n/a |
| d30_c20_w10 | 0 | 0.00 | n/a | n/a | n/a | $0 (0.0%) | n/a | n/a | n/a |
| idx_base | 15 | 1.00 | -0.07% [-0.12%, -0.02%] | -0.23% | 7% | $1,078 (1.1%) | 40.0% [13.3%, 66.7%] | -$69 [-$123, -$12] | -18.1% [-32.9%, -2.8%] |
| mega_base | 35 | 2.33 | -0.18% [-0.27%, -0.09%] | -0.51% | 20% | $2,733 (2.7%) | 40.0% [22.9%, 57.1%] | -$77 [-$116, -$38] | -20.6% [-30.9%, -10.2%] |
| idx_d30_c20 | 2 | 0.13 | -0.01% [-0.02%, 0.00%] | -0.07% | 0% | $111 (0.1%) | 0.0% [0.0%, 0.0%] | -$56 [-$71, -$40] | -14.0% [-17.8%, -10.2%] |
| mega_d30_c20 | 15 | 1.00 | -0.05% [-0.12%, 0.00%] | -0.35% | 27% | $1,048 (1.0%) | 46.7% [20.0%, 73.3%] | -$54 [-$110, $2] | -13.8% [-28.1%, 0.7%] |
| base_ema | 103 | 6.87 | -0.30% [-0.53%, -0.12%] | -1.52% | 20% | $4,650 (4.7%) | 48.5% [38.8%, 58.3%] | -$44 [-$65, -$24] | -12.0% [-17.6%, -6.6%] |
| base_iv50 | 81 | 5.40 | -0.28% [-0.48%, -0.13%] | -1.23% | 20% | $4,638 (4.6%) | 44.4% [33.3%, 55.6%] | -$53 [-$76, -$30] | -14.3% [-20.6%, -8.2%] |
| d30_c20_ema | 50 | 3.33 | -0.16% [-0.27%, -0.06%] | -0.69% | 13% | $2,458 (2.5%) | 46.0% [32.0%, 60.0%] | -$47 [-$76, -$20] | -12.3% [-19.8%, -5.1%] |
| d30_c20_iv50 | 42 | 2.80 | -0.12% [-0.26%, 0.00%] | -0.75% | 27% | $2,178 (2.2%) | 47.6% [33.3%, 61.9%] | -$43 [-$71, -$14] | -11.4% [-19.0%, -3.9%] |
| base_stop2 | 127 | 8.47 | -0.42% [-0.57%, -0.29%] | -1.19% | 0% | $6,321 (6.3%) | 44.9% [36.2%, 53.5%] | -$50 [-$68, -$31] | -13.3% [-18.2%, -8.6%] |
| d30_c20_stop2 | 64 | 4.27 | -0.20% [-0.35%, -0.06%] | -0.76% | 20% | $3,184 (3.2%) | 43.8% [31.2%, 56.2%] | -$47 [-$70, -$24] | -12.2% [-18.4%, -6.3%] |
| d10_c10 | 2 | 0.13 | -0.01% [-0.03%, 0.00%] | -0.13% | 0% | $138 (0.1%) | 0.0% [0.0%, 0.0%] | -$69 [-$128, -$10] | -16.1% [-30.0%, -2.2%] |
| d16_c10 | 104 | 6.93 | -0.14% [-0.27%, -0.02%] | -0.63% | 40% | $2,315 (2.3%) | 61.5% [51.9%, 71.2%] | -$20 [-$34, -$7] | -4.6% [-7.6%, -1.6%] |
| d20_c10 | 167 | 11.13 | -0.35% [-0.48%, -0.21%] | -0.92% | 13% | $5,235 (5.2%) | 55.7% [47.9%, 63.5%] | -$31 [-$44, -$20] | -7.2% [-10.1%, -4.5%] |
| d30_c10 | 174 | 11.60 | -0.47% [-0.68%, -0.27%] | -1.42% | 13% | $7,019 (7.0%) | 54.0% [46.6%, 61.5%] | -$40 [-$55, -$26] | -9.7% [-13.1%, -6.1%] |
| d16_c10_dte7 | 59 | 3.93 | -0.01% [-0.08%, 0.06%] | -0.27% | 67% | $807 (0.8%) | 83.1% [72.9%, 91.5%] | -$2 [-$22, $15] | -0.3% [-4.8%, 3.4%] |
| d16_c10_dte21 | 99 | 6.60 | -0.11% [-0.23%, 0.01%] | -0.61% | 40% | $1,938 (1.9%) | 70.7% [61.6%, 79.8%] | -$16 [-$31, -$2] | -3.6% [-6.9%, -0.5%] |
| d16_c10_idx | 5 | 0.33 | -0.01% [-0.03%, 0.01%] | -0.13% | 13% | $131 (0.1%) | 40.0% [0.0%, 80.0%] | -$25 [-$80, $18] | -5.6% [-17.9%, 4.0%] |
| d16_c10_stop2 | 112 | 7.47 | -0.16% [-0.30%, -0.04%] | -0.62% | 40% | $2,697 (2.7%) | 52.7% [43.8%, 61.6%] | -$22 [-$32, -$12] | -5.0% [-7.3%, -2.7%] |
| base_mid | 141 | 9.40 | 0.09% [0.00%, 0.18%] | -0.21% | 73% | $378 (0.4%) | 53.9% [45.4%, 61.7%] | $9 [-$4, $21] | 2.7% [-1.1%, 6.4%] |
| base_nickel | 119 | 7.93 | -0.50% [-0.67%, -0.35%] | -1.37% | 0% | $7,550 (7.6%) | 45.4% [36.1%, 54.6%] | -$63 [-$83, -$43] | -16.7% [-22.0%, -11.4%] |
| d16_c10_mid | 115 | 7.67 | 0.07% [-0.02%, 0.16%] | -0.32% | 60% | $512 (0.5%) | 71.3% [62.6%, 79.1%] | $9 [$1, $18] | 2.2% [0.1%, 4.2%] |
| base_tp25 | 144 | 9.60 | -0.53% [-0.71%, -0.36%] | -1.41% | 7% | $7,953 (8.0%) | 52.8% [44.4%, 60.4%] | -$55 [-$71, -$40] | -14.7% [-18.9%, -10.7%] |

## Walk-forward

Each fold selects from entries on or before the train end, using the same rule, then scores entries inside the fold. When nothing qualifies, the fold still reports the live `base` book so an empty selection is not an empty month.

| Fold test | Selected | OOS trades | OOS monthly return (95% CI) | OOS max drawdown | OOS trades/month | Base monthly return |
| --- | --- | ---: | --- | --- | ---: | --- |
| 2024-07-01 to 2024-12-31 | none | 0 | n/a | n/a | 0.00 | -0.60% [-0.80%, -0.33%] |
| 2025-01-02 to 2025-06-30 | none | 0 | n/a | n/a | 0.00 | -0.40% [-0.57%, -0.17%] |
| 2025-07-01 to 2025-12-31 | none | 0 | n/a | n/a | 0.00 | -0.45% [-0.63%, -0.28%] |
| 2026-01-02 to 2026-09-30 | none | 0 | n/a | n/a | 0.00 | -0.47% [-0.69%, -0.31%] |

## Decisions for Christian

These rows need a locked rule changed, or they assume a fill the paper account did not get. They are not implemented. Drawdown is the peak-to-trough of the fee-adjusted book on the test window, starting from $100,000.

| Design | Trades | Trades/month | Monthly return (95% CI) | Worst month | Positive months | Max drawdown | Win rate (95% CI) | Expectancy $ (95% CI) | Expectancy / max risk (95% CI) |
| --- | ---: | ---: | --- | ---: | ---: | --- | --- | --- | --- |
| base_risk1 | 123 | 8.20 | -0.94% [-1.25%, -0.66%] | -2.45% | 0% | $14,147 (14.1%) | 43.9% [35.0%, 52.8%] | -$115 [-$153, -$76] | -15.2% [-20.2%, -10.1%] |
| base_cap10 | 159 | 10.60 | -0.63% [-0.84%, -0.43%] | -1.61% | 7% | $9,617 (9.6%) | 42.8% [35.2%, 50.3%] | -$59 [-$76, -$43] | -15.9% [-20.4%, -11.5%] |
| d16_c10_risk1 | 104 | 6.93 | -0.28% [-0.53%, -0.04%] | -1.25% | 40% | $4,630 (4.6%) | 61.5% [51.9%, 71.2%] | -$40 [-$67, -$14] | -4.6% [-7.6%, -1.6%] |
| d16_c10_cap10 | 116 | 7.73 | -0.15% [-0.30%, -0.01%] | -0.65% | 40% | $2,607 (2.6%) | 62.1% [52.6%, 70.7%] | -$19 [-$33, -$7] | -4.4% [-7.4%, -1.6%] |
| d16_c10 | 104 | 6.93 | -0.14% [-0.27%, -0.02%] | -0.63% | 40% | $2,315 (2.3%) | 61.5% [51.9%, 71.2%] | -$20 [-$34, -$7] | -4.6% [-7.6%, -1.6%] |
| base_tp25 | 144 | 9.60 | -0.53% [-0.71%, -0.36%] | -1.41% | 7% | $7,953 (8.0%) | 52.8% [44.4%, 60.4%] | -$55 [-$71, -$40] | -14.7% [-18.9%, -10.7%] |
| base_mid | 141 | 9.40 | 0.09% [0.00%, 0.18%] | -0.21% | 73% | $378 (0.4%) | 53.9% [45.4%, 61.7%] | $9 [-$4, $21] | 2.7% [-1.1%, 6.4%] |

Reading the cost:

- Lowering the credit gate from 20% to 10% is what lets a 16-delta short exist. That is a weaker credit, which is a lower signal on the rule this sleeve already uses as a quality floor. The `d16_c10` and `d10_c10` rows are that change. Their monthly intervals are the evidence. They are not a 3% book.
- `base_tp25` cuts the locked 50% take-profit to 25%. The win is smaller. The ceiling table gets worse, not better, because the win multiple is linear in the take-profit fraction.
- `base` at 1% risk (`base_risk1`) doubles the dollars on the same signals. Drawdown scales with the size. The monthly percentage does not become 3% just because each lot is bigger; the per-trade R multiple is unchanged and the sizer often goes from one contract to two.
- `base` with 10 concurrent names (`base_cap10`) relaxes the 5-spread cap. It can only add trades the 5-spread book dropped. It does not create a new edge.
- A second position in the same symbol is not in the tape. The replay keeps one arm and clears it on a fill, so there is no second independent signal whose drawdown could be measured. It is not a path to 3%.
- The $99,750 gross cap does not bind. A one-lot $5 credit spread uses a few hundred dollars of defined risk, not the underlying notional. Raising the gross cap does not add a fill.
- `base_mid` assumes you are filled at the mid. The unfilled MSFT order is the live evidence that a day limit at the natural, which is worse than the mid, already fails to trade. A mid fill is not an implementable order on this feed.

## Chain evidence

Snapshot date 2026-10-02. CBOE delayed quotes (bid/ask and exchange delta), one cross-section. Names that returned a chain: SPY, QQQ, IWM, AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, PFE, T, KRE, XLF. Missing: none. Two-sided verticals in the delta and DTE buckets: 1982 on SPY/QQQ/IWM and 1521 on the single names.

A cell is a listed short whose exchange |delta| is within 0.02 of the target, with the long strike one width away, both sides bid and ask positive and uncrossed. `Clears 20%` is natural credit (short bid − long ask) of at least 20% of width.

| Group | Delta | DTE | Width | N | Median natural/width | Median mid/width | Clears 20% |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| SPY/QQQ/IWM | 0.10 | 21-30 | 5 | 79 | 4.8% | 5.8% | 0.0% |
| SPY/QQQ/IWM | 0.10 | 21-30 | 10 | 75 | 4.6% | 5.0% | 0.0% |
| SPY/QQQ/IWM | 0.10 | 30-45 | 5 | 39 | 5.2% | 6.2% | 0.0% |
| SPY/QQQ/IWM | 0.10 | 30-45 | 10 | 28 | 4.9% | 5.4% | 0.0% |
| SPY/QQQ/IWM | 0.10 | 7-14 | 5 | 130 | 5.0% | 5.5% | 0.0% |
| SPY/QQQ/IWM | 0.10 | 7-14 | 10 | 124 | 4.2% | 4.4% | 0.0% |
| SPY/QQQ/IWM | 0.16 | 21-30 | 5 | 50 | 9.6% | 10.7% | 0.0% |
| SPY/QQQ/IWM | 0.16 | 21-30 | 10 | 50 | 8.7% | 9.2% | 0.0% |
| SPY/QQQ/IWM | 0.16 | 30-45 | 5 | 62 | 9.0% | 10.8% | 0.0% |
| SPY/QQQ/IWM | 0.16 | 30-45 | 10 | 54 | 8.8% | 9.4% | 0.0% |
| SPY/QQQ/IWM | 0.16 | 7-14 | 5 | 85 | 9.6% | 10.1% | 0.0% |
| SPY/QQQ/IWM | 0.16 | 7-14 | 10 | 80 | 7.8% | 8.2% | 0.0% |
| SPY/QQQ/IWM | 0.20 | 21-30 | 5 | 38 | 12.8% | 13.6% | 0.0% |
| SPY/QQQ/IWM | 0.20 | 21-30 | 10 | 38 | 11.6% | 12.0% | 0.0% |
| SPY/QQQ/IWM | 0.20 | 30-45 | 5 | 55 | 11.8% | 14.1% | 0.0% |
| SPY/QQQ/IWM | 0.20 | 30-45 | 10 | 54 | 11.6% | 12.7% | 0.0% |
| SPY/QQQ/IWM | 0.20 | 7-14 | 5 | 81 | 12.6% | 13.4% | 0.0% |
| SPY/QQQ/IWM | 0.20 | 7-14 | 10 | 73 | 10.6% | 10.8% | 0.0% |
| SPY/QQQ/IWM | 0.25 | 21-30 | 5 | 31 | 16.6% | 17.9% | 19.4% |
| SPY/QQQ/IWM | 0.25 | 21-30 | 10 | 31 | 15.1% | 15.7% | 3.2% |
| SPY/QQQ/IWM | 0.25 | 30-45 | 5 | 45 | 15.8% | 18.4% | 22.2% |
| SPY/QQQ/IWM | 0.25 | 30-45 | 10 | 45 | 15.2% | 16.8% | 13.3% |
| SPY/QQQ/IWM | 0.25 | 7-14 | 5 | 69 | 16.8% | 17.7% | 24.6% |
| SPY/QQQ/IWM | 0.25 | 7-14 | 10 | 64 | 14.3% | 14.6% | 0.0% |
| SPY/QQQ/IWM | 0.30 | 21-30 | 5 | 31 | 20.8% | 22.3% | 67.7% |
| SPY/QQQ/IWM | 0.30 | 21-30 | 10 | 28 | 18.9% | 19.7% | 35.7% |
| SPY/QQQ/IWM | 0.30 | 30-45 | 5 | 42 | 20.0% | 23.0% | 54.8% |
| SPY/QQQ/IWM | 0.30 | 30-45 | 10 | 41 | 19.3% | 20.7% | 39.0% |
| SPY/QQQ/IWM | 0.30 | 7-14 | 5 | 58 | 21.6% | 22.5% | 72.4% |
| SPY/QQQ/IWM | 0.30 | 7-14 | 10 | 58 | 18.5% | 18.9% | 32.8% |
| SPY/QQQ/IWM | 0.40 | 21-30 | 5 | 30 | 31.4% | 33.0% | 100.0% |
| SPY/QQQ/IWM | 0.40 | 21-30 | 10 | 30 | 28.8% | 29.4% | 100.0% |
| SPY/QQQ/IWM | 0.40 | 30-45 | 5 | 43 | 29.8% | 34.0% | 100.0% |
| SPY/QQQ/IWM | 0.40 | 30-45 | 10 | 37 | 28.4% | 30.3% | 100.0% |
| SPY/QQQ/IWM | 0.40 | 7-14 | 5 | 52 | 30.4% | 31.5% | 100.0% |
| SPY/QQQ/IWM | 0.40 | 7-14 | 10 | 52 | 26.4% | 27.1% | 90.4% |
| single names | 0.10 | 21-30 | 5 | 51 | 4.0% | 7.1% | 0.0% |
| single names | 0.10 | 21-30 | 10 | 52 | 4.4% | 6.2% | 0.0% |
| single names | 0.10 | 30-45 | 5 | 53 | 1.0% | 7.8% | 0.0% |
| single names | 0.10 | 30-45 | 10 | 60 | 3.1% | 6.7% | 0.0% |
| single names | 0.10 | 7-14 | 5 | 98 | 3.6% | 6.2% | 0.0% |
| single names | 0.10 | 7-14 | 10 | 90 | 3.4% | 5.0% | 0.0% |
| single names | 0.16 | 21-30 | 5 | 37 | 7.2% | 12.1% | 0.0% |
| single names | 0.16 | 21-30 | 10 | 36 | 8.5% | 10.9% | 0.0% |
| single names | 0.16 | 30-45 | 5 | 41 | 2.2% | 13.0% | 0.0% |
| single names | 0.16 | 30-45 | 10 | 41 | 7.0% | 11.5% | 0.0% |
| single names | 0.16 | 7-14 | 5 | 64 | 7.0% | 11.4% | 0.0% |
| single names | 0.16 | 7-14 | 10 | 62 | 6.8% | 9.1% | 0.0% |
| single names | 0.20 | 21-30 | 5 | 31 | 11.0% | 16.0% | 0.0% |
| single names | 0.20 | 21-30 | 10 | 31 | 11.5% | 14.7% | 0.0% |
| single names | 0.20 | 30-45 | 5 | 39 | 4.0% | 16.5% | 0.0% |
| single names | 0.20 | 30-45 | 10 | 39 | 8.5% | 16.1% | 0.0% |
| single names | 0.20 | 7-14 | 5 | 59 | 10.0% | 14.0% | 0.0% |
| single names | 0.20 | 7-14 | 10 | 58 | 8.9% | 11.5% | 0.0% |
| single names | 0.25 | 21-30 | 5 | 29 | 14.0% | 22.0% | 6.9% |
| single names | 0.25 | 21-30 | 10 | 27 | 16.0% | 19.1% | 7.4% |
| single names | 0.25 | 30-45 | 5 | 33 | 7.0% | 21.0% | 6.1% |
| single names | 0.25 | 30-45 | 10 | 33 | 11.5% | 19.6% | 9.1% |
| single names | 0.25 | 7-14 | 5 | 50 | 14.0% | 19.5% | 4.0% |
| single names | 0.25 | 7-14 | 10 | 46 | 12.5% | 16.6% | 2.2% |
| single names | 0.30 | 21-30 | 5 | 20 | 20.0% | 25.5% | 60.0% |
| single names | 0.30 | 21-30 | 10 | 20 | 20.0% | 26.0% | 50.0% |
| single names | 0.30 | 30-45 | 5 | 31 | 9.0% | 25.5% | 22.6% |
| single names | 0.30 | 30-45 | 10 | 31 | 17.0% | 24.3% | 22.6% |
| single names | 0.30 | 7-14 | 5 | 41 | 16.0% | 25.0% | 24.4% |
| single names | 0.30 | 7-14 | 10 | 41 | 15.5% | 20.0% | 17.1% |
| single names | 0.40 | 21-30 | 5 | 24 | 28.0% | 34.7% | 83.3% |
| single names | 0.40 | 21-30 | 10 | 24 | 27.5% | 32.2% | 95.8% |
| single names | 0.40 | 30-45 | 5 | 27 | 21.0% | 37.0% | 59.3% |
| single names | 0.40 | 30-45 | 10 | 27 | 23.0% | 34.7% | 59.3% |
| single names | 0.40 | 7-14 | 5 | 38 | 24.6% | 34.0% | 65.8% |
| single names | 0.40 | 7-14 | 10 | 37 | 23.8% | 29.1% | 70.3% |

Historical last trades. Yahoo daily last trades of contracts still listed on the CBOE chain. Short and long closes are not a simultaneous print. Delta bucket is the strike's exchange delta on the snapshot day, not the delta on the historical day. Expired contracts are absent. Sample size 353 contract-days.

| Current delta bucket | DTE on that day | Width | N | Median last/width | Last trade clears 20% |
| ---: | --- | ---: | ---: | ---: | ---: |
| 0.16 | 21-30 | 5 | 54 | 13.8% | 11.1% |
| 0.16 | 30-45 | 5 | 71 | 17.2% | 40.8% |
| 0.30 | 21-30 | 5 | 54 | 26.2% | 79.6% |
| 0.30 | 30-45 | 5 | 54 | 28.8% | 70.4% |
| 0.40 | 21-30 | 5 | 56 | 35.4% | 98.2% |
| 0.40 | 30-45 | 5 | 64 | 34.4% | 79.7% |

## Model surface

Black-Scholes on an exact target delta, not a listed strike. Spots $30, $50, $100, $200, $500, and $770. Volatilities 12%, 18%, 25%, 40%, and 80%. Puts and calls. The half-spread is the replay's 6% clamp. This grid does not know which names trended; it only asks whether the credit exists.

| Delta | DTE | Width | N | Median natural/width | Share clearing 20% |
| ---: | --- | ---: | ---: | ---: | ---: |
| 0.10 | 21-30 | 2.5 | 120 | 1.7% | 0.0% |
| 0.10 | 21-30 | 5.0 | 120 | 3.1% | 0.0% |
| 0.10 | 21-30 | 10.0 | 120 | 3.0% | 0.0% |
| 0.10 | 30-45 | 2.5 | 60 | 1.5% | 0.0% |
| 0.10 | 30-45 | 5.0 | 60 | 3.4% | 0.0% |
| 0.10 | 30-45 | 10.0 | 60 | 3.5% | 0.0% |
| 0.10 | 7-14 | 2.5 | 120 | 1.8% | 0.0% |
| 0.10 | 7-14 | 5.0 | 120 | 2.5% | 0.0% |
| 0.10 | 7-14 | 10.0 | 120 | 2.2% | 0.0% |
| 0.16 | 21-30 | 2.5 | 120 | 4.5% | 0.0% |
| 0.16 | 21-30 | 5.0 | 120 | 6.6% | 0.0% |
| 0.16 | 21-30 | 10.0 | 120 | 6.0% | 0.0% |
| 0.16 | 30-45 | 2.5 | 60 | 4.4% | 0.0% |
| 0.16 | 30-45 | 5.0 | 60 | 7.1% | 0.0% |
| 0.16 | 30-45 | 10.0 | 60 | 6.6% | 0.0% |
| 0.16 | 7-14 | 2.5 | 120 | 4.9% | 0.0% |
| 0.16 | 7-14 | 5.0 | 120 | 5.9% | 0.0% |
| 0.16 | 7-14 | 10.0 | 120 | 4.6% | 0.0% |
| 0.20 | 21-30 | 2.5 | 120 | 6.6% | 0.0% |
| 0.20 | 21-30 | 5.0 | 120 | 8.8% | 0.0% |
| 0.20 | 21-30 | 10.0 | 120 | 8.4% | 1.7% |
| 0.20 | 30-45 | 2.5 | 60 | 7.1% | 0.0% |
| 0.20 | 30-45 | 5.0 | 60 | 9.1% | 0.0% |
| 0.20 | 30-45 | 10.0 | 60 | 8.8% | 5.0% |
| 0.20 | 7-14 | 2.5 | 120 | 6.3% | 0.0% |
| 0.20 | 7-14 | 5.0 | 120 | 8.0% | 0.0% |
| 0.20 | 7-14 | 10.0 | 120 | 6.4% | 0.0% |
| 0.25 | 21-30 | 2.5 | 120 | 9.7% | 2.5% |
| 0.25 | 21-30 | 5.0 | 120 | 11.8% | 6.7% |
| 0.25 | 21-30 | 10.0 | 120 | 11.2% | 8.3% |
| 0.25 | 30-45 | 2.5 | 60 | 9.6% | 3.3% |
| 0.25 | 30-45 | 5.0 | 60 | 11.8% | 10.0% |
| 0.25 | 30-45 | 10.0 | 60 | 11.7% | 11.7% |
| 0.25 | 7-14 | 2.5 | 120 | 9.4% | 0.0% |
| 0.25 | 7-14 | 5.0 | 120 | 11.4% | 0.0% |
| 0.25 | 7-14 | 10.0 | 120 | 8.7% | 4.2% |
| 0.30 | 21-30 | 2.5 | 120 | 13.0% | 7.5% |
| 0.30 | 21-30 | 5.0 | 120 | 15.7% | 20.0% |
| 0.30 | 21-30 | 10.0 | 120 | 14.6% | 19.2% |
| 0.30 | 30-45 | 2.5 | 60 | 13.2% | 10.0% |
| 0.30 | 30-45 | 5.0 | 60 | 15.8% | 26.7% |
| 0.30 | 30-45 | 10.0 | 60 | 15.4% | 26.7% |
| 0.30 | 7-14 | 2.5 | 120 | 12.6% | 4.2% |
| 0.30 | 7-14 | 5.0 | 120 | 14.9% | 10.8% |
| 0.30 | 7-14 | 10.0 | 120 | 11.1% | 12.5% |
| 0.40 | 21-30 | 2.5 | 120 | 20.2% | 52.5% |
| 0.40 | 21-30 | 5.0 | 120 | 23.5% | 64.2% |
| 0.40 | 21-30 | 10.0 | 120 | 21.0% | 54.2% |
| 0.40 | 30-45 | 2.5 | 60 | 20.7% | 55.0% |
| 0.40 | 30-45 | 5.0 | 60 | 23.9% | 66.7% |
| 0.40 | 30-45 | 10.0 | 60 | 23.4% | 56.7% |
| 0.40 | 7-14 | 2.5 | 120 | 19.9% | 48.3% |
| 0.40 | 7-14 | 5.0 | 120 | 22.9% | 58.3% |
| 0.40 | 7-14 | 10.0 | 120 | 16.5% | 43.3% |

## Fill, fees, and assignment

The primary book is natural fills plus the regulatory fee. `base_nickel` worsens both sides by another $0.05. `base_mid` removes the bid/ask. The gap between those two rows is the spread, not an edge.

Early assignment: 358 baseline spreads were rechecked on daily closes between entry and the modeled exit. 0 would have been assigned under the five-cent extrinsic rule. Test-window book P&L moves from -$6,971 to -$6,971 when those exits replace the modeled ones and the 5-spread book is rebuilt. A call that is assigned early because of a dividend, while it still has extrinsic, is not in this check.

## Data limits

There is no stored OPRA tape. Alpaca historical option quotes were not pulled: the options API keys are not in this environment, and `config/paper-live.yaml` is not in the repo. The backtest marks are Black-Scholes on Yahoo underlying bars. The chain section is the check on that model, and it is a delayed cross-section plus last trades of contracts that have not expired yet.

Yahoo hourly bars are regular-session bars only. A decision uses the bar that has already closed. Daily structure uses the session whose 16:00 close is already known. The IV proxy is a markup on realized vol, not a listed implied vol. The CBOE delta is the listed one; the historical last-trade panel reuses today's delta label for that strike, so a day from months ago is not a true 16-delta observation. Last trades of the two legs are not simultaneous.

The replay's $2.50 width is not in the candidate list. On a $1 strike grid a $2.50 wing is not a listed spread, and the pricer would have to invent the long strike. $5 and $10 are on the $1, $2.50, and $5 grids.

SPY hourly bars in this run run from 2023-11-03 through 2026-10-02. Entries before 2024-01-02 are warmup. Entries after 2026-09-30 are excluded.

## Reproduce

```bash
PYTHONPATH=src python3 -m alpaca_options_credit.research_3pct.run \
  --cache var/replay-cache --chain-cache var/research-3pct/chains \
  --out docs/research/options-3pct-redesign.md
```

Bootstrap is 5,000 resamples, seed 20261002. The Yahoo underlying cache and the CBOE/Yahoo option cache live under `var/` (gitignored). A warm cache does not hit the network. No config default changes with this report.
