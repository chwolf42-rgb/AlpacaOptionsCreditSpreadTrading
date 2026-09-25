# Strategy redesign

Keep the bot paused. No precommitted design has a positive expectancy out of sample beyond sampling noise. No design cleared the train window, so nothing was sent to the test as a winner. The live book is still the PR #10 sleeve with the price stop off, and that sleeve loses money. Appendix rows that look less bad on the test window were not the selection.

## How a design was allowed to win

Train is 2024-01-02 through 2025-06-30. Test is 2025-07-01 through 2026-09-25. The recent window is 2026-07-06 through 2026-09-25. The long window is 2024-01-02 through 2026-09-25 and is reported for continuity with the earlier study. It includes the train data, so it is not the adoption test.

A train row needs at least 50 trades. Both the dollar expectancy and the expectancy divided by that spread's max loss need a 95% bootstrap interval entirely above zero. The winner is the eligible row with the highest expectancy per unit of risk. That one row then has to clear the same bar on the test window with at least 30 trades, show a positive point estimate on 2025 H2 and on 2026 when those slices have at least 10 trades, not have a Jul–Sep 2026 interval entirely below zero when that slice has at least 15 trades, and show a positive dollar point estimate on the 5-spread book.

There are 53 searchable designs. The train intervals are not adjusted for that search. The test interval is one look at the precommitted winner. A different design that looks good only on the test window is listed in the appendix and is not implemented.

## Pricing

Same model as `docs/win-rate-study.md`. There is no stored option tape. Alpaca historical option quotes were not reachable: `OPTIONS_APCA_API_KEY_ID` and `OPTIONS_APCA_API_SECRET_KEY` are unset, and `config/paper-live.yaml` is not in the repo. Each vertical is Black-Scholes on the underlying bars.

The IV used to price a spread is the last 20 sessions of close-to-close realized vol times 1.15, frozen at entry, clamped between 15% and 125%. Rates are 4% with no dividend. Because that IV is defined as a markup on 20-day realized vol, comparing it with the same 20-day realized vol is not a filter. The IV filter compares it with 60-day realized vol, and the IV percentile compares it with the prior 252 readings of itself. VIX is the Yahoo `^VIX` close. VIX richness means the VIX percentile is high and VIX/100 is above SPY's 20-day realized vol. That is a market-regime filter, not a listed implied vol for the single name.

Each leg's half-spread is 6% of its mid, at least $0.05 and at most $0.25. The entry credit in the primary book is the natural credit: short bid minus long ask. Take-profit and the credit stop are judged on the mid. A take-profit fills at a debit of (1 − fraction) × credit. A stop that gaps through the open fills at the open's natural debit. A stop that trades through fills at the multiple times the credit plus the bid/ask, and no better than that bar's worst debit. A close through structure, the short, or the shelf pays the natural debit at that daily close. Expiration settles at intrinsic. A position still open on the last bar is marked at the natural debit and kept in the averages.

The mid-fill sensitivity books the spread mid on entry and the mid on every debit, so it pays no bid/ask. The nickel sensitivity takes the natural credit minus $0.05 and adds $0.05 to every debit, including the formula take-profit fill. Stops are judged on the mid in every sensitivity. The primary tables are natural fills.

A close through the short strike or the HVN shelf is checked before take-profit, because it is a stop. The legacy structure-break (a daily close through swing invalidation) is still checked after take-profit, which is the order in the earlier replay. The live engine checks structure break before the mark. That difference is unchanged from PR #10 for the baseline.

Strike grid: $1 on the ETFs in the full-A list; stocks use $0.50 under $50, $1 under $200, $2.50 under $500, and $5 above that. A delta short has to clear the anchor by the $1 gap. If the closest listed delta is more than 0.08 closer to the money than the target, the entry is skipped. A strike further out than the target is kept, because the shelf is what protects the short. The anchor is swing invalidation on a confirm entry, the shelf edge on an extension, and the prior 20-day high or low on a condor. Earnings dates are the Nasdaq announcement calendar. Five of 771 weekday requests failed, so an announcement that fell on one of those days is not blacked out. ETFs had no earnings rows. The blackout is three calendar days around the entry and around expiration, which is the live `earnings_blackout_days` rule. The baseline row leaves the calendar empty so it can be compared with PR #10.

Sizing in the per-spread tables is 0.5% of a fixed $100,000, one spread per name. Max loss is (width − credit) × 100 × contracts. An iron condor is one slot: the credit is the sum of the two sides, and the max loss is one width minus that combined credit. The 5-spread book then applies the 10% open-risk cap on realized equity. Intervals are 5,000-draw percentile bootstraps from a NumPy generator seeded at 20260925. Max drawdown is the peak-to-trough of cumulative dollars ordered by exit time, starting from $100,000. It is a path fact, not a bootstrap interval.

## Train window (2024-01-02 to 2025-06-30)

| Design | Trades | Trades/week | Win rate (95% CI) | Expectancy $ (95% CI) | Expectancy / max risk (95% CI) | Avg win | Avg loss | Max drawdown | Avg |delta| |
| --- | ---: | ---: | --- | --- | --- | ---: | ---: | --- | ---: |
| baseline | 183 | 2.35 | 42.6% [35.5%, 49.7%] | -$52 [-$67, -$37] | -13.8% [-17.8%, -9.8%] | $60 | -$135 | $9643 (9.6%) | 0.37 |
| baseline_earn | 172 | 2.21 | 44.2% [36.6%, 51.7%] | -$49 [-$65, -$34] | -13.1% [-17.3%, -9.0%] | $60 | -$135 | $8618 (8.6%) | 0.37 |
| d10 | 2 | 0.03 | 50.0% [0.0%, 100.0%] | $3 [-$19, $25] | 0.7% [-4.3%, 5.6%] | $25 | -$19 | $19 (0.0%) | 0.10 |
| d16 | 142 | 1.82 | 66.2% [57.7%, 73.9%] | -$13 [-$25, -$3] | -3.0% [-5.5%, -0.6%] | $28 | -$95 | $2632 (2.6%) | 0.16 |
| d20 | 243 | 3.12 | 62.1% [56.0%, 67.9%] | -$20 [-$30, -$11] | -4.7% [-6.8%, -2.6%] | $32 | -$106 | $5440 (5.4%) | 0.20 |
| d25 | 322 | 4.13 | 56.2% [50.6%, 61.5%] | -$29 [-$38, -$20] | -6.7% [-8.8%, -4.6%] | $37 | -$114 | $9591 (9.6%) | 0.24 |
| d30 | 351 | 4.50 | 55.6% [50.4%, 60.7%] | -$31 [-$40, -$22] | -7.4% [-9.7%, -5.2%] | $41 | -$122 | $11260 (11.2%) | 0.28 |
| d16_c20 | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| d16_c33 | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| d20_c20 | 1 | 0.01 | 100.0% [100.0%, 100.0%] | $54 [$54, $54] | 13.8% [13.8%, 13.8%] | $54 | n/a | $0 (0.0%) | 0.20 |
| d16_w10 | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| d20_w10 | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| d16_w10_c20 | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| d16_pct2 | 120 | 1.54 | 58.3% [49.2%, 67.5%] | -$25 [-$36, -$14] | -7.4% [-10.7%, -4.3%] | $22 | -$90 | $3169 (3.2%) | 0.16 |
| d20_pct1 | 134 | 1.72 | 52.2% [44.0%, 60.4%] | -$61 [-$78, -$43] | -14.5% [-18.7%, -10.4%] | $26 | -$155 | $8226 (8.2%) | 0.20 |
| d16_iv50 | 92 | 1.18 | 63.0% [53.3%, 72.8%] | -$15 [-$29, -$2] | -3.4% [-6.5%, -0.5%] | $29 | -$90 | $1990 (2.0%) | 0.16 |
| d16_iv70 | 70 | 0.90 | 64.3% [52.9%, 75.7%] | -$11 [-$25, $2] | -2.6% [-5.7%, 0.6%] | $30 | -$85 | $1415 (1.4%) | 0.16 |
| d16_iv_rv | 104 | 1.33 | 65.4% [55.8%, 74.0%] | -$14 [-$27, -$2] | -3.1% [-6.2%, -0.4%] | $29 | -$95 | $2171 (2.2%) | 0.16 |
| d16_iv50_rv | 82 | 1.05 | 61.0% [50.0%, 70.7%] | -$17 [-$32, -$4] | -3.8% [-7.2%, -0.8%] | $30 | -$90 | $1986 (2.0%) | 0.16 |
| d16_vix50 | 58 | 0.74 | 65.5% [53.4%, 77.6%] | -$11 [-$28, $4] | -2.6% [-6.3%, 0.8%] | $28 | -$86 | $828 (0.8%) | 0.16 |
| d16_tp25 | 150 | 1.92 | 71.3% [64.0%, 78.0%] | -$16 [-$25, -$8] | -3.7% [-5.7%, -1.9%] | $14 | -$92 | $2968 (3.0%) | 0.16 |
| d16_tp35 | 147 | 1.88 | 70.1% [62.6%, 77.6%] | -$13 [-$22, -$4] | -3.0% [-5.0%, -1.0%] | $20 | -$90 | $2727 (2.7%) | 0.16 |
| d16_dte21 | 143 | 1.83 | 55.9% [47.6%, 63.6%] | -$20 [-$31, -$10] | -4.6% [-7.1%, -2.3%] | $27 | -$81 | $3339 (3.3%) | 0.16 |
| d16_spot_short | 141 | 1.81 | 83.7% [77.3%, 89.4%] | -$14 [-$30, $2] | -3.1% [-6.9%, 0.4%] | $29 | -$229 | $2945 (2.9%) | 0.16 |
| d16_spot_shelf | 153 | 1.96 | 41.8% [34.0%, 49.7%] | -$22 [-$30, -$15] | -5.0% [-6.8%, -3.3%] | $29 | -$59 | $3623 (3.6%) | 0.16 |
| d16_cat2 | 146 | 1.87 | 58.2% [50.0%, 65.8%] | -$15 [-$23, -$6] | -3.3% [-5.2%, -1.4%] | $29 | -$75 | $2754 (2.7%) | 0.16 |
| d16_cat3 | 142 | 1.82 | 66.2% [57.7%, 73.9%] | -$12 [-$22, -$2] | -2.7% [-5.1%, -0.4%] | $28 | -$91 | $2445 (2.4%) | 0.16 |
| d16_managed | 153 | 1.96 | 39.9% [32.0%, 47.7%] | -$22 [-$29, -$15] | -4.9% [-6.5%, -3.3%] | $28 | -$55 | $3536 (3.5%) | 0.16 |
| d16_ema | 112 | 1.44 | 67.9% [58.9%, 76.8%] | -$11 [-$23, $0] | -2.5% [-5.2%, 0.1%] | $28 | -$93 | $1800 (1.8%) | 0.16 |
| base_ema | 116 | 1.49 | 40.5% [31.9%, 49.1%] | -$56 [-$74, -$38] | -15.0% [-19.9%, -10.1%] | $59 | -$135 | $6938 (6.9%) | 0.37 |
| etf_d16 | 44 | 0.56 | 86.4% [75.0%, 95.5%] | $11 [-$4, $23] | 2.5% [-0.9%, 5.1%] | $28 | -$94 | $294 (0.3%) | 0.16 |
| stock_d16 | 98 | 1.26 | 57.1% [46.9%, 67.3%] | -$24 [-$39, -$11] | -5.5% [-8.7%, -2.4%] | $29 | -$95 | $2693 (2.7%) | 0.16 |
| etf_base | 44 | 0.56 | 54.5% [40.9%, 68.2%] | -$21 [-$49, $6] | -5.5% [-12.9%, 1.6%] | $60 | -$119 | $1334 (1.3%) | 0.36 |
| ext_d16 | 266 | 3.41 | 24.8% [19.5%, 29.7%] | -$38 [-$44, -$33] | -8.7% [-10.0%, -7.4%] | $29 | -$61 | $10208 (10.2%) | 0.16 |
| ext_d16_managed | 273 | 3.50 | 24.9% [20.1%, 30.0%] | -$38 [-$43, -$32] | -8.6% [-9.8%, -7.3%] | $28 | -$60 | $10316 (10.3%) | 0.16 |
| ext_d20_managed | 490 | 6.28 | 21.2% [17.8%, 24.9%] | -$46 [-$50, -$42] | -10.7% [-11.7%, -9.6%] | $32 | -$67 | $22665 (22.7%) | 0.20 |
| ext_d16_ema | 114 | 1.46 | 23.7% [15.8%, 31.6%] | -$35 [-$42, -$27] | -7.8% [-9.6%, -6.1%] | $27 | -$54 | $3946 (3.9%) | 0.16 |
| ext_d16_iv_ema | 60 | 0.77 | 21.7% [11.7%, 33.3%] | -$38 [-$49, -$28] | -8.7% [-11.1%, -6.3%] | $26 | -$56 | $2324 (2.3%) | 0.16 |
| ext_etf_h1 | 16 | 0.21 | 25.0% [6.2%, 50.0%] | -$29 [-$46, -$11] | -6.5% [-10.3%, -2.5%] | $21 | -$46 | $462 (0.5%) | 0.16 |
| ext_etf_d20 | 30 | 0.38 | 26.7% [10.0%, 43.3%] | -$33 [-$50, -$17] | -7.6% [-11.2%, -3.8%] | $30 | -$56 | $996 (1.0%) | 0.20 |
| ext_etf_d10_w10 | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| ext_etf_d30 | 39 | 0.50 | 17.9% [7.7%, 30.8%] | -$60 [-$77, -$41] | -14.5% [-18.8%, -9.7%] | $49 | -$84 | $2330 (2.3%) | 0.30 |
| ext_etf_tp25 | 16 | 0.21 | 37.5% [12.5%, 62.5%] | -$25 [-$42, -$9] | -5.7% [-9.5%, -2.0%] | $14 | -$49 | $418 (0.4%) | 0.16 |
| ext_etf_c20 | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| ext_etf_pct2 | 9 | 0.12 | 22.2% [0.0%, 55.6%] | -$26 [-$48, -$3] | -7.1% [-12.3%, -1.4%] | $28 | -$42 | $238 (0.2%) | 0.16 |
| ext_etf_cat3 | 16 | 0.21 | 31.2% [12.5%, 56.2%] | -$27 [-$45, -$10] | -6.1% [-10.1%, -2.2%] | $19 | -$49 | $459 (0.5%) | 0.16 |
| ext_stock_h1 | 44 | 0.56 | 20.5% [9.1%, 34.1%] | -$42 [-$55, -$29] | -9.5% [-12.4%, -6.6%] | $28 | -$60 | $1871 (1.9%) | 0.16 |
| conf_etf_h4 | 14 | 0.18 | 42.9% [21.4%, 71.4%] | -$10 [-$28, $8] | -2.2% [-6.4%, 1.8%] | $27 | -$37 | $168 (0.2%) | 0.16 |
| tasty_etf | 25 | 0.32 | 72.0% [52.0%, 88.0%] | $2 [-$17, $18] | 0.5% [-3.7%, 4.2%] | $27 | -$61 | $238 (0.2%) | 0.16 |
| tasty_all | 93 | 1.19 | 59.1% [49.5%, 68.8%] | -$12 [-$22, -$1] | -2.7% [-5.1%, -0.2%] | $28 | -$69 | $1325 (1.3%) | 0.16 |
| condor_etf_iv | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| condor_etf | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| condor_all_iv | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

### What each train row is

- `baseline` — Live book: confirm entry, nearest short, 20% of width, $5, 50% take-profit, no price stop, structure break. Earnings calendar empty, matching the book that was studied in PR #10.
- `baseline_earn` — Same live book, plus a 3-day earnings blackout around entry and expiration.
- `d10` — Confirm entry, short nearest 10 delta beyond invalidation, $5 wide, minimum credit 10% of width, live exits.
- `d16` — Confirm entry, short nearest 16 delta beyond invalidation, $5 wide, minimum credit 10% of width, live exits.
- `d20` — Confirm entry, short nearest 20 delta beyond invalidation, $5 wide, minimum credit 10% of width, live exits.
- `d25` — Confirm entry, short nearest 25 delta beyond invalidation, $5 wide, minimum credit 10% of width, live exits.
- `d30` — Confirm entry, short nearest 30 delta beyond invalidation, $5 wide, minimum credit 10% of width, live exits.
- `d16_c20` — 16 delta, $5, minimum credit raised to 20% of width, live exits.
- `d16_c33` — 16 delta, $5, minimum credit 33% of width, live exits.
- `d20_c20` — 20 delta, $5, minimum credit 20% of width, live exits.
- `d16_w10` — 16 delta, $10 wide, minimum credit 10%, live exits.
- `d20_w10` — 20 delta, $10 wide, minimum credit 10%, live exits.
- `d16_w10_c20` — 16 delta, $10 wide, minimum credit 20%, live exits.
- `d16_pct2` — 16 delta, width 2% of price snapped to the strike step, minimum credit 10%, live exits.
- `d20_pct1` — 20 delta, width 1% of price, minimum credit 10%, live exits.
- `d16_iv50` — 16 delta live book, sell only when the IV proxy percentile is at least 50.
- `d16_iv70` — 16 delta live book, IV proxy percentile at least 70.
- `d16_iv_rv` — 16 delta live book, sell only when the IV proxy is above 60-day realized vol.
- `d16_iv50_rv` — 16 delta live book, IV percentile at least 50 and IV proxy above 60-day realized.
- `d16_vix50` — 16 delta live book, VIX percentile at least 50 and VIX above SPY 20-day realized vol.
- `d16_tp25` — 16 delta, take-profit at 25% of credit, no price stop, structure break.
- `d16_tp35` — 16 delta, take-profit at 35% of credit, no price stop, structure break.
- `d16_dte21` — 16 delta, live exits plus a close when 21 calendar days remain.
- `d16_spot_short` — 16 delta, no price stop, close on a daily close through the short strike.
- `d16_spot_shelf` — 16 delta, no price stop, close on a daily close through the HVN shelf.
- `d16_cat2` — 16 delta, 2× credit catastrophe stop judged on the hourly close, structure break kept.
- `d16_cat3` — 16 delta, 3× credit catastrophe stop on the hourly close, structure break kept.
- `d16_managed` — 16 delta, 50% take-profit, close at 21 DTE, 2× close stop, daily close through the shelf.
- `d16_ema` — 16 delta live exits, bull puts only above the daily EMA50 and bear calls only below it.
- `base_ema` — Live nearest-short book with the EMA50 trend filter and the earnings blackout.
- `etf_d16` — 16 delta live exits, index and ETF names only.
- `stock_d16` — 16 delta live exits, single stocks only.
- `etf_base` — Live nearest-short book restricted to index and ETF names, with the earnings blackout.
- `ext_d16` — Sell the 16-delta spread into an HVN shelf after a one-ATR extension, live exits.
- `ext_d16_managed` — Extension entry, 16 delta, managed exits (50%, 21 DTE, 2× close, shelf).
- `ext_d20_managed` — Extension entry, 20 delta, managed exits.
- `ext_d16_ema` — Extension entry, 16 delta, EMA50 trend filter, managed exits.
- `ext_d16_iv_ema` — Extension entry, 16 delta, IV percentile 50, IV above 60-day realized, EMA50, managed exits. All names.
- `ext_etf_h1` — ETFs only. Extension into the shelf, 16 delta, $5, 10% minimum credit, IV percentile 50, IV above 60-day realized, EMA50, managed exits.
- `ext_etf_d20` — Same ETF extension stack at 20 delta.
- `ext_etf_d10_w10` — ETF extension stack at 10 delta and $10 wide.
- `ext_etf_d30` — ETF extension stack at 30 delta.
- `ext_etf_tp25` — ETF extension stack with the take-profit lowered to 25% of credit.
- `ext_etf_c20` — ETF extension stack with the minimum credit raised to 20% of width.
- `ext_etf_pct2` — ETF extension stack with the width set to 2% of price.
- `ext_etf_cat3` — ETF extension stack with a 35% take-profit and a 3× catastrophe stop.
- `ext_stock_h1` — Single stocks only. Same extension stack as the ETF version.
- `conf_etf_h4` — Confirm entry (not extension), ETFs, 16 delta, IV percentile 50, IV above realized, EMA50, managed exits.
- `tasty_etf` — ETFs, confirm entry, 16 delta, IV percentile 50, 50% take-profit, close at 21 DTE, 2× close stop, no underlying stop.
- `tasty_all` — Same 16-delta managed premium sale on the full list, IV percentile 50, no trend filter, no underlying stop.
- `condor_etf_iv` — Iron condor on ETFs when price is mid-range and within one ATR of the EMA50. 16 delta each side, IV percentile 50, managed exits, shorts beyond the 20-day range.
- `condor_etf` — Same ETF iron condor with no IV filter.
- `condor_all_iv` — Iron condor on the full list, IV percentile 50, managed exits.

## Selection

No searchable design had 50 or more train trades and both expectancy intervals entirely above zero.

No train row cleared the interval bar.

No design cleared the train window.

## Out-of-sample test (2025-07-01 to 2026-09-25)

| Design | Trades | Trades/week | Win rate (95% CI) | Expectancy $ (95% CI) | Expectancy / max risk (95% CI) | Avg win | Avg loss | Max drawdown | Avg |delta| |
| --- | ---: | ---: | --- | --- | --- | ---: | ---: | --- | ---: |
| baseline | 164 | 2.54 | 40.9% [33.5%, 48.2%] | -$62 [-$78, -$46] | -16.6% [-20.9%, -12.3%] | $58 | -$145 | $10129 (10.1%) | 0.38 |

## Walk-forward

Each fold picks from trades entered in its train window, then the next window is the out-of-sample slice of that same design. The design was simulated as if it ran the whole tape, so a position opened before the fold can still be busy.

| Fold train end | Selected | OOS trades | OOS expectancy $ | OOS expectancy / max risk |
| --- | --- | ---: | --- | --- |
| 2024-06-30 | none | 0 | n/a | n/a |
| 2024-12-31 | none | 0 | n/a | n/a |
| 2025-06-30 | none | 0 | n/a | n/a |
| 2025-12-31 | none | 0 | n/a | n/a |

## Recent window (2026-07-06 to 2026-09-25)

| Design | Trades | Trades/week | Win rate (95% CI) | Expectancy $ (95% CI) | Expectancy / max risk (95% CI) | Avg win | Avg loss | Max drawdown | Avg |delta| |
| --- | ---: | ---: | --- | --- | --- | ---: | ---: | --- | ---: |
| baseline | 38 | 3.24 | 42.1% [26.3%, 57.9%] | -$53 [-$88, -$19] | -14.0% [-23.3%, -5.1%] | $59 | -$134 | $2002 (2.0%) | 0.38 |

## Long window (2024-01-02 to 2026-09-25)

| Design | Trades | Trades/week | Win rate (95% CI) | Expectancy $ (95% CI) | Expectancy / max risk (95% CI) | Avg win | Avg loss | Max drawdown | Avg |delta| |
| --- | ---: | ---: | --- | --- | --- | ---: | ---: | --- | ---: |
| baseline | 347 | 2.43 | 41.8% [36.6%, 47.0%] | -$56 [-$68, -$45] | -15.1% [-18.1%, -12.2%] | $59 | -$139 | $19653 (19.6%) | 0.37 |

## Five-spread book

Same signals, then a greedy book: at most 5 names, 0.5% of realized equity, 10% open risk. A skipped signal does not invent a later replacement.

| Design | Trades | Trades/week | Win rate (95% CI) | Expectancy $ (95% CI) | Expectancy / max risk (95% CI) | Avg win | Avg loss | Max drawdown | Avg |delta| |
| --- | ---: | ---: | --- | --- | --- | ---: | ---: | --- | ---: |
| baseline test, max 5 | 123 | 1.90 | 42.3% [33.3%, 51.2%] | -$59 [-$78, -$39] | -15.8% [-20.8%, -10.6%] | $58 | -$145 | $7248 (7.2%) | 0.38 |
| baseline long, max 5 | 271 | 1.90 | 41.7% [36.2%, 47.6%] | -$56 [-$68, -$44] | -15.1% [-18.2%, -11.8%] | $59 | -$139 | $15427 (15.4%) | 0.37 |

## SPY buy and hold

SPY buy-and-hold is the close on the first session of the window to the close on the last, on $100,000. Max drawdown is the peak-to-trough of the daily closes. It is not a credit-spread expectancy.

| Window | SPY return | $100k P&L | Max drawdown |
| --- | ---: | ---: | ---: |
| Train 2024-01-02 to 2025-06-30 | 30.7% | $30720 | 19.0% |
| Test 2025-07-01 to 2026-09-25 | 24.9% | $24885 | 9.1% |
| Recent 2026-07-06 to 2026-09-25 | 2.7% | $2671 | 3.4% |
| Long 2024-01-02 to 2026-09-25 | 63.2% | $63197 | 19.0% |

## Fill sensitivity

| Design | Trades | Trades/week | Win rate (95% CI) | Expectancy $ (95% CI) | Expectancy / max risk (95% CI) | Avg win | Avg loss | Max drawdown | Avg |delta| |
| --- | ---: | ---: | --- | --- | --- | ---: | ---: | --- | ---: |
| baseline natural, train | 183 | 2.35 | 42.6% [35.5%, 49.7%] | -$52 [-$67, -$37] | -13.8% [-17.8%, -9.8%] | $60 | -$135 | $9643 (9.6%) | 0.37 |
| baseline natural, test | 164 | 2.54 | 40.9% [33.5%, 48.2%] | -$62 [-$78, -$46] | -16.6% [-20.9%, -12.3%] | $58 | -$145 | $10129 (10.1%) | 0.38 |
| baseline_mid, train | 183 | 2.35 | 46.4% [39.3%, 53.6%] | $7 [-$4, $17] | 2.1% [-1.1%, 5.2%] | $78 | -$55 | $1032 (1.0%) | 0.37 |
| baseline_mid, test | 170 | 2.63 | 51.8% [44.1%, 59.4%] | $8 [-$4, $19] | 2.3% [-1.2%, 5.6%] | $74 | -$63 | $460 (0.5%) | 0.38 |
| baseline_nickel, train | 183 | 2.35 | 42.1% [35.0%, 49.2%] | -$62 [-$78, -$47] | -16.3% [-20.4%, -12.3%] | $53 | -$145 | $11523 (11.5%) | 0.37 |
| baseline_nickel, test | 164 | 2.54 | 40.9% [33.5%, 48.2%] | -$71 [-$87, -$55] | -18.7% [-23.0%, -14.5%] | $51 | -$155 | $11614 (11.6%) | 0.38 |

## Exit mix for the baseline and the train selection

| Design | Window | Trades | Take-profit | Credit stop | Structure break | Underlying stop | 21 DTE | Expiration | Open mark |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | train | 183 | 78 | 0 | 105 | 0 | 0 | 0 | 0 |
| baseline | test | 164 | 66 | 0 | 92 | 0 | 0 | 0 | 6 |
| baseline | long | 347 | 144 | 0 | 197 | 0 | 0 | 0 | 6 |

## Appendix: test window for every searchable design

These numbers were not used to pick the winner. A row whose test interval sits above zero, and that was not the train selection, is not a candidate to ship.

| Design | Trades | Trades/week | Win rate (95% CI) | Expectancy $ (95% CI) | Expectancy / max risk (95% CI) | Avg win | Avg loss | Max drawdown | Avg |delta| |
| --- | ---: | ---: | --- | --- | --- | ---: | ---: | --- | ---: |
| baseline | 164 | 2.54 | 40.9% [33.5%, 48.2%] | -$62 [-$78, -$46] | -16.6% [-20.9%, -12.3%] | $58 | -$145 | $10129 (10.1%) | 0.38 |
| baseline_earn | 146 | 2.26 | 43.2% [34.9%, 51.4%] | -$58 [-$75, -$40] | -15.5% [-20.1%, -10.9%] | $58 | -$145 | $8425 (8.4%) | 0.38 |
| d10 | 1 | 0.02 | 0.0% [0.0%, 0.0%] | -$9 [-$9, -$9] | -2.2% [-2.2%, -2.2%] | n/a | -$9 | $9 (0.0%) | 0.10 |
| d16 | 101 | 1.56 | 67.3% [57.4%, 76.2%] | -$12 [-$25, $0] | -2.7% [-5.6%, 0.1%] | $28 | -$95 | $1708 (1.7%) | 0.16 |
| d20 | 205 | 3.17 | 60.5% [53.7%, 67.3%] | -$26 [-$37, -$15] | -5.9% [-8.4%, -3.4%] | $31 | -$112 | $5422 (5.4%) | 0.20 |
| d25 | 276 | 4.27 | 57.2% [51.4%, 63.0%] | -$33 [-$44, -$23] | -7.9% [-10.4%, -5.5%] | $35 | -$126 | $9246 (9.2%) | 0.24 |
| d30 | 308 | 4.77 | 55.5% [50.0%, 61.0%] | -$38 [-$49, -$27] | -9.1% [-11.8%, -6.5%] | $39 | -$134 | $11665 (11.7%) | 0.28 |
| d16_c20 | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| d16_c33 | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| d20_c20 | 1 | 0.02 | 0.0% [0.0%, 0.0%] | -$67 [-$67, -$67] | -16.8% [-16.8%, -16.8%] | n/a | -$67 | $67 (0.1%) | 0.20 |
| d16_w10 | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| d20_w10 | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| d16_w10_c20 | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| d16_pct2 | 73 | 1.13 | 61.6% [50.7%, 72.6%] | -$19 [-$33, -$6] | -5.5% [-9.2%, -2.0%] | $23 | -$86 | $1455 (1.5%) | 0.16 |
| d20_pct1 | 86 | 1.33 | 51.2% [40.7%, 61.6%] | -$59 [-$80, -$39] | -14.1% [-19.1%, -9.3%] | $26 | -$148 | $5191 (5.2%) | 0.20 |
| d16_iv50 | 55 | 0.85 | 63.6% [50.9%, 76.4%] | -$17 [-$36, $0] | -4.0% [-8.1%, 0.0%] | $29 | -$99 | $1626 (1.6%) | 0.16 |
| d16_iv70 | 35 | 0.54 | 62.9% [45.7%, 77.1%] | -$13 [-$36, $7] | -3.1% [-8.1%, 1.5%] | $29 | -$85 | $849 (0.8%) | 0.16 |
| d16_iv_rv | 82 | 1.27 | 67.1% [57.3%, 76.8%] | -$14 [-$29, -$0] | -3.2% [-6.5%, -0.1%] | $28 | -$100 | $1627 (1.6%) | 0.16 |
| d16_iv50_rv | 51 | 0.79 | 62.7% [49.0%, 76.5%] | -$17 [-$37, $0] | -3.9% [-8.4%, 0.1%] | $29 | -$94 | $1478 (1.5%) | 0.16 |
| d16_vix50 | 43 | 0.67 | 60.5% [46.5%, 74.4%] | -$21 [-$43, -$1] | -4.8% [-9.9%, -0.3%] | $28 | -$97 | $1158 (1.2%) | 0.16 |
| d16_tp25 | 110 | 1.70 | 75.5% [67.3%, 82.7%] | -$15 [-$26, -$5] | -3.5% [-6.0%, -1.2%] | $14 | -$106 | $1887 (1.9%) | 0.16 |
| d16_tp35 | 107 | 1.66 | 72.9% [64.5%, 81.3%] | -$13 [-$25, -$3] | -3.1% [-5.6%, -0.6%] | $20 | -$103 | $1796 (1.8%) | 0.16 |
| d16_dte21 | 101 | 1.56 | 61.4% [51.5%, 70.3%] | -$15 [-$27, -$3] | -3.3% [-6.1%, -0.7%] | $27 | -$81 | $1835 (1.8%) | 0.16 |
| d16_spot_short | 100 | 1.55 | 90.0% [84.0%, 95.0%] | $4 [-$11, $18] | 0.9% [-2.6%, 4.0%] | $28 | -$216 | $533 (0.5%) | 0.16 |
| d16_spot_shelf | 114 | 1.77 | 42.1% [33.3%, 50.9%] | -$23 [-$32, -$14] | -5.2% [-7.2%, -3.2%] | $28 | -$60 | $2718 (2.7%) | 0.16 |
| d16_cat2 | 103 | 1.60 | 55.3% [45.6%, 65.0%] | -$19 [-$30, -$8] | -4.3% [-6.7%, -1.8%] | $28 | -$78 | $2181 (2.2%) | 0.16 |
| d16_cat3 | 101 | 1.56 | 65.3% [55.4%, 74.3%] | -$15 [-$27, -$2] | -3.3% [-6.2%, -0.5%] | $28 | -$95 | $1772 (1.8%) | 0.16 |
| d16_managed | 116 | 1.80 | 38.8% [29.3%, 47.4%] | -$25 [-$34, -$16] | -5.6% [-7.6%, -3.7%] | $28 | -$59 | $2991 (3.0%) | 0.16 |
| d16_ema | 88 | 1.36 | 70.5% [61.4%, 79.5%] | -$10 [-$24, $3] | -2.3% [-5.5%, 0.7%] | $28 | -$102 | $1349 (1.3%) | 0.16 |
| base_ema | 104 | 1.61 | 48.1% [38.5%, 57.7%] | -$44 [-$64, -$24] | -12.1% [-17.5%, -6.7%] | $58 | -$139 | $4784 (4.8%) | 0.37 |
| etf_d16 | 32 | 0.50 | 59.4% [40.6%, 75.0%] | -$12 [-$31, $6] | -2.7% [-7.1%, 1.2%] | $27 | -$68 | $556 (0.6%) | 0.16 |
| stock_d16 | 69 | 1.07 | 71.0% [60.9%, 81.2%] | -$12 [-$29, $4] | -2.7% [-6.5%, 1.0%] | $29 | -$112 | $1326 (1.3%) | 0.16 |
| etf_base | 52 | 0.81 | 38.5% [25.0%, 51.9%] | -$69 [-$98, -$40] | -18.8% [-26.7%, -11.0%] | $57 | -$148 | $3704 (3.7%) | 0.37 |
| ext_d16 | 230 | 3.56 | 23.5% [17.8%, 29.1%] | -$40 [-$46, -$34] | -9.0% [-10.3%, -7.7%] | $29 | -$61 | $9135 (9.1%) | 0.16 |
| ext_d16_managed | 238 | 3.69 | 23.1% [18.1%, 28.6%] | -$41 [-$46, -$35] | -9.3% [-10.5%, -7.9%] | $29 | -$62 | $9692 (9.7%) | 0.16 |
| ext_d20_managed | 399 | 6.18 | 22.3% [18.3%, 26.3%] | -$48 [-$53, -$43] | -11.0% [-12.2%, -9.9%] | $32 | -$71 | $18999 (19.0%) | 0.20 |
| ext_d16_ema | 111 | 1.72 | 25.2% [17.1%, 33.3%] | -$40 [-$49, -$31] | -9.1% [-11.1%, -7.1%] | $28 | -$63 | $4444 (4.4%) | 0.16 |
| ext_d16_iv_ema | 68 | 1.05 | 19.1% [10.3%, 29.4%] | -$47 [-$58, -$36] | -10.7% [-13.2%, -8.2%] | $29 | -$65 | $3217 (3.2%) | 0.16 |
| ext_etf_h1 | 27 | 0.42 | 25.9% [11.1%, 44.4%] | -$36 [-$54, -$18] | -8.2% [-12.2%, -4.1%] | $29 | -$59 | $1008 (1.0%) | 0.16 |
| ext_etf_d20 | 43 | 0.67 | 20.9% [9.3%, 32.6%] | -$53 [-$68, -$37] | -12.3% [-15.8%, -8.6%] | $34 | -$76 | $2317 (2.3%) | 0.20 |
| ext_etf_d10_w10 | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| ext_etf_d30 | 49 | 0.76 | 18.4% [8.2%, 28.6%] | -$67 [-$84, -$49] | -17.2% [-21.6%, -12.6%] | $49 | -$93 | $3325 (3.3%) | 0.30 |
| ext_etf_tp25 | 28 | 0.43 | 39.3% [21.4%, 57.1%] | -$33 [-$51, -$16] | -7.5% [-11.5%, -3.6%] | $15 | -$65 | $991 (1.0%) | 0.16 |
| ext_etf_c20 | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| ext_etf_pct2 | 18 | 0.28 | 33.3% [11.1%, 55.6%] | -$25 [-$43, -$9] | -8.0% [-13.1%, -2.7%] | $20 | -$48 | $481 (0.5%) | 0.16 |
| ext_etf_cat3 | 26 | 0.40 | 34.6% [15.4%, 53.8%] | -$32 [-$50, -$15] | -7.3% [-11.3%, -3.4%] | $20 | -$60 | $895 (0.9%) | 0.16 |
| ext_stock_h1 | 41 | 0.63 | 14.6% [4.9%, 26.8%] | -$54 [-$67, -$40] | -12.4% [-15.5%, -9.2%] | $28 | -$68 | $2216 (2.2%) | 0.16 |
| conf_etf_h4 | 12 | 0.19 | 33.3% [8.3%, 58.3%] | -$32 [-$58, -$4] | -7.3% [-13.3%, -1.0%] | $27 | -$61 | $463 (0.5%) | 0.16 |
| tasty_etf | 15 | 0.23 | 40.0% [13.3%, 66.7%] | -$38 [-$67, -$10] | -8.9% [-15.5%, -2.3%] | $27 | -$82 | $709 (0.7%) | 0.16 |
| tasty_all | 55 | 0.85 | 49.1% [36.4%, 61.8%] | -$27 [-$42, -$11] | -6.1% [-9.6%, -2.6%] | $28 | -$80 | $1823 (1.8%) | 0.16 |
| condor_etf_iv | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| condor_etf | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| condor_all_iv | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

## Decision

No design cleared the train window.

No change to `config/default.yaml`. Leave `exits.credit_stop: false` as PR #10 set it, and do not run the sleeve. `config/paper-live.yaml` is not in this repo. If a copy is still placing orders, stop it. The hard locks stay in the code either way: options-native exits, atomic 2-leg opens and closes, paper only, and the risk caps the operator asked to keep (0.5% per spread, 10% open, and a 5-spread book on the machine that trades — the tracked default still says 20 concurrent, which is the dry-run list size, not a reason to keep trading).

Plain English: selling the near-the-money credit the 20% width rule demands, into a breakout retest, did not become a winner by moving the short to a listed delta, by waiting for a high vol-proxy rank, by closing at 21 DTE, by stopping on the shelf, or by switching to condors and ETFs. Nothing in the precommitted search cleared an untouched test window. Keep the bot paused.

## Reproduce

```bash
PYTHONPATH=src python3 -m alpaca_options_credit.replay.redesign --cache var/replay-cache --out docs/strategy-redesign.md
```


The Yahoo cache, the VIX cache, and the Nasdaq earnings cache live under `var/replay-cache` (gitignored). A warm cache does not hit the network. Bootstrap seed is 20260925, 5,000 resamples.
