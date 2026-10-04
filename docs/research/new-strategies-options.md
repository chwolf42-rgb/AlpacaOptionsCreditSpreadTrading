# New defined-risk options strategies

Results are filled in after the walk-forward run. The split dates, grids, pricing, and selection rule below were written down before that run. Holdout is evaluated once, after the walk-forward choices are frozen. This study does not change the live credit-spread bot.

## Splits (frozen)

| Window | Dates | Role |
| --- | --- | --- |
| Warmup | 2016-01-04 through the session before 2018-01-02 | Indicators only. No fills. |
| In-sample | 2018-01-02 through 2022-12-30 | 60 months. Grid is scored here. The reported IS number for the selected row is optimistic. The textbook default (grid index 0) is reported beside it. |
| Walk-forward | 2023-01-03 through 2025-09-30 | 33 months. Every quarter, refit on the trailing window in `FOLDS` inside `backtests/new_strategies/specs.py`, then trade the next quarter with those parameters frozen. Open positions keep the exit rules they were opened with. |
| Holdout fit | entries 2023-10-02 through 2025-09-30 | Chooses the holdout parameters. No holdout price is an input. |
| Holdout | 2025-10-01 through 2026-09-30 | 12 months. One look. Log: `docs/research/new_strategies_options/holdout_log.json`. |

October 2026 is outside the holdout. The account is $100,000. Monthly return is that month's closed-trade P&L divided by $100,000, not by a compounding denominator. Months with no exit are zero.

Selection, also frozen: highest mean monthly return on the fit window. Ties break to lower mark-to-market max drawdown, then to the earlier grid index. A variant with fewer than 8 closed trades is not eligible. If none is eligible, the textbook default is kept.

Bootstrap, frozen: 3-month circular block bootstrap, 5,000 resamples, seed 20261004, 90% interval at the empirical 5th and 95th percentiles of the resampled means.

## What was tested

Each candidate is its own strategy. Grids are the lists in `specs.py`. Count of unique configs: debit momentum 5, short-dated 4, condor 4, butterfly 4, calendar 4, diagonal 3. Every fit, including dropped configs and the flat-RV stress, is counted in "variants tried."

1. **Debit momentum.** Call debit vertical when the close is above an SMA and the lookback return is positive; put debit vertical when both are negative. Long delta about 0.50–0.55, short about 0.25–0.30, 21–42 DTE. One extra pre-declared filter, `cheap_iv`: only enter when the VIX percentile is below 50. Reason: a debit spread overpays when implied vol is rich. Take 50% of the max profit. Stop at a loss of half the debit. Exit with 7 DTE left.
2. **Short-dated debit verticals.** SPY, QQQ, IWM only. Same debit structure, triggered by a 3- or 5-day return beyond 0.3% or 0.5% and the same side of the 20-day average. Target 5 or 7 DTE. Daily bars cannot mark a 0 DTE or 1 DTE path, so the book is 2–9 calendar days to a Friday. Exit with 1 DTE left. An intraday trigger was not tested: Yahoo 5-minute history is about 60 days, which cannot support this walk-forward.
3. **Iron condor, two orders.** Put credit vertical on the next open, call credit vertical on the open after that. Each wing is one spread toward the cap of 5 and is sized to its own 0.5% max loss. Open risk adds both, and the second wing's 0.5% is reserved while it is waiting. If the next open gaps more than 1.5 ATR from the signal close, or the credit is under max($0.20, 8% of the $5 width), the second wing is skipped and the first wing is an orphan. That orphan is the leg-timing risk. Short strike within 0.08 of the target delta or the order is skipped. Package take-profit is 50% of the combined credit. Package stop is a loss equal to the combined credit. A touch of either short strike closes both. Exit with 21 DTE left. Index ETFs only.
4. **Butterfly, two orders.** Long call fly, body at the rounded spot, wings one width below and above. Debit vertical (lower wing) on the next open, credit vertical (upper wing) on the open after that, at the strikes locked by the first fill. Same reservation, gap filter, and orphan rule as the condor. A complete fly takes 50% of (width − net debit) and stops at half the net debit. A print through either wing closes both.
5. **Put calendar, one two-leg order.** Short the front ATM put, long the back ATM put, same strike. Enter when VIX9D/VIX3M is at least the grid ratio (front rich) and the close is inside a band of the 20-day average. Max loss for sizing is the debit. Take +25% of the debit, stop at −50%, or exit with 2 front DTE left.
6. **Diagonal, one two-leg order.** With the trend (close versus SMA, 20-day return the same sign): long the back option near 0.50–0.60 delta, short the front option near 0.25–0.35 delta, short strike further out of the money. Max loss for sizing is the debit. A one-lot debit above $5 cannot be bought: 0.5% of $100,000 is $500. Take +50% of the debit, stop at −50%, or exit with 2 front DTE left.

Universe for debit momentum: SPY, QQQ, IWM, AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA. The other strategies use SPY, QQQ, IWM only. One structure per symbol. A condor or fly is that symbol's structure and occupies two spread slots. No second idea was added.

## Pricing

There is no free OPRA history. Marks are Black-Scholes-Merton on the underlying bar.

- Spot is the Yahoo chart close/open/high/low. That close is already split-adjusted and is not dividend-adjusted. A constant dividend yield (SPY 1.3%, QQQ 0.6%, IWM 1.2%, single names 0–0.8%) is in the pricer. The discount rate is the FRED 3-month yield (DGS3MO) known at the prior close, or 2% before the series starts.
- ATM level: SPY = VIX, QQQ = VXN, IWM = RVX. Single names = 20-day realized vol times the SPY variance premium (VIX / SPY 20-day realized), clamped between 0.8 and 2.0, then clamped to 12–150%. VIX is a variance swap, so it sits above ATM implied vol. Using it raw makes debit spreads more expensive and credit spreads richer. The flat-RV stress (below) is the check on that choice. It is not allowed to change the holdout parameters, and it is not run on the holdout.
- Term structure: total variance is interpolated between VIX9D (9 days), VIX (30), and VIX3M (93). Outside that span the nearest knot's vol is used. QQQ, IWM, and single names take that shape as a ratio applied to their own 30-day level.
- Skew, not fitted: standardized moneyness m = log(K/S) / (atm √T), iv = atm × (1 − 0.10 m), clamped. OTM puts are richer than ATM. OTM calls are cheaper.
- Fill: each leg trades at mid ± 25% of the full bid-ask width, against the order. Index full width is $0.04 to $0.20 depending on |delta| and DTE (tighter when |delta| ≥ 0.30 and DTE > 21; $0.20 when DTE ≤ 2). Single names are 2.5× that, capped at $1, and the width cannot exceed the mid. A short leg with a bid under $0.01 is not sold. Regulatory fee is $0.02 per contract per leg at entry and again at exit.
- Decisions use the closed bar. The fill is the next session's open. Vol used on session T is the curve from T−1. A stop and a target in the same bar: the stop wins. A gap through the open fills at the open. An intrabar touch fills at the stop or target limit. Expiration is not held through the last `exit_dte` days; the time exit is the close.
- Size is floor(0.5% of realized equity / max loss per contract), at most 10 contracts. The 10-lot cap is the one-lot quote: the width model does not walk the book. Unused risk budget is not a rule break. Open risk, including a reserved second wing, stays ≤ 10% of realized equity and ≤ $99,750. Gross exposure is defined-risk buying power (the max loss), which is what a spread uses. At most 5 spreads. One structure per symbol.

Flat-RV stress, not a candidate: implied vol is 1.15 × 20-day realized, clamped 15–125%, no term structure and no skew. Run on the textbook default for the IS window and the OOS window only.

## Data

| Series | Source | Limit |
| --- | --- | --- |
| Underlying OHLC | Yahoo chart API, daily, 2016-01-04 through 2026-10-01 | Split-adjusted close. No options volume, so the universe is today's liquid names. Single names are survivorship-biased. SPY, QQQ, and IWM are not. |
| VIX, DGS3MO | FRED VIXCLS, DGS3MO | VIX is a variance swap, not a strike. |
| VIX9D, VIX3M, VXN, RVX | CBOE daily history CSVs | VIX9D starts in 2011, which covers this warmup. |

No Alpaca key is used. Cache: `docs/research/new_strategies_options/data/`.

Live data budget if one of these replaced the credit scan, once per day after the close (the cadence the backtest actually is): 2 batched stock-bar requests plus one option-chain snapshot per name. Debit momentum is 12 Alpaca requests in that minute, 6% of the shared 200/min, and one external vol request. Each index strategy is 5 Alpaca requests, 2.5% of 200. They are not additive if they share the chain snapshot. An hourly short-dated scan was not validated.

## Results

Pending the walk-forward run. The headline table will replace this paragraph: OOS and holdout mean monthly return with 90% CI, max drawdown, trades per month, average R, profit factor, variants tried, and a verdict against +3% a month (+6R net).
