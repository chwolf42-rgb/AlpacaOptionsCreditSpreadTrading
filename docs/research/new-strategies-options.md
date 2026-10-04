# New defined-risk options strategies

None of the six candidates reaches +3% a month. The best holdout reading is an iron condor at **+0.083% a month** (90% CI +0.037% to +0.138%, 12 months). That is about 36 times smaller than 3%, and it is about **+0.22R net a month** against the +6R a +3% month would require at 0.5% risk. Walk-forward on the same condor was +0.011% a month with a CI that includes zero. Replacing the VIX-level vol with 1.15× realized vol, which was not used to pick parameters and was not run on the holdout, loses money. The other five are flat or lose. Short-dated debit verticals lose about 0.7% a month out of sample and in the holdout, with a 27% walk-forward drawdown.

+3% a month was not close. The locked caps are not what stopped it. Peak defined-risk exposure stayed near $2,400 against a $10,000 open-risk cap and a $99,750 gross cap.

## Ranked results

Account is $100,000. Monthly return is closed-trade P&L divided by $100,000. The 90% interval is a 3-month circular block bootstrap, 5,000 resamples, seed 20261004. IS is 60 months (2018-01 through 2022-12). Walk-forward OOS is 33 months (2023-01 through 2025-09). Holdout is 12 months (2025-10 through 2026-09), one look, logged in `holdout_log.json` at 2026-10-04T01:16:52Z.

IS below is the textbook default (grid index 0), except the "IS selected" note. Ranking uses the worse of the OOS and holdout means, then penalizes a CI that crosses zero or a drawdown above a few percent. A one-trade holdout does not count as confirmation.

| Rank | Candidate | OOS monthly | Holdout monthly | Max DD (OOS / holdout) | Trades/mo (OOS) | Avg R (OOS) | PF (OOS) | Variants | Verdict |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | Condor, two orders | +0.011% [−0.016%, +0.036%] | +0.083% [+0.037%, +0.138%] | 0.8% / 0.2% | 2.91 | +0.009 | 1.16 | 54 | Holdout CI is above zero and still far from 3%. OOS CI includes zero. Flat-RV stress loses. |
| 2 | Butterfly, two orders | +0.011% [−0.050%, +0.071%] | −0.005% [−0.087%, +0.083%] | 1.3% / 0.7% | 12.88 | +0.004 | 1.03 | 54 | Both intervals cover zero. Holdout sign does not match OOS. |
| 3 | Put calendar | −0.013% [−0.043%, +0.011%] | +0.003% [0.000%, +0.008%] | 0.6% / 0.0% | 0.24 | −0.151 | 0.50 | 54 | Eight OOS trades. Holdout is one winner. No edge to confirm. |
| 4 | Diagonal | −0.018% [−0.054%, +0.016%] | 0.000% (0 trades) | 1.2% / 0% | 1.36 | −0.027 | 0.79 | 41 | Holdout parameter set never fit inside a $500 max loss. OOS lost. |
| 5 | Debit momentum | +0.026% [−0.194%, +0.240%] | −0.073% [−0.184%, +0.033%] | 5.5% / 1.9% | 7.09 | +0.008 | 1.03 | 67 | OOS point is positive, holdout point is negative. Wide CI. |
| 6 | Short-dated debit | −0.787% [−1.064%, −0.513%] | −0.669% [−1.412%, +0.130%] | 26.6% / 10.7% | 17.97 | −0.119 | 0.65 | 54 | Loses in both windows. Worst drawdown in the set. |

Worst months, OOS then holdout: condor −0.267% / −0.106%; butterfly −0.373% / −0.278%; calendar −0.552% / 0%; diagonal −0.841% / 0%; debit momentum −1.835% / −0.480%; short-dated −3.028% / −2.698%.

Win rates, OOS: condor 75%, butterfly 48%, calendar 50% (n=8), diagonal 47%, debit momentum 45%, short-dated 35%. Holdout win rates: condor 64% (n=72), butterfly 36% (n=137), calendar 100% (n=1), diagonal n/a, debit momentum 42% (n=26), short-dated 34% (n=206). Calendar holdout profit factor is undefined because that single trade won.

Exposure versus the caps. Gross here is defined-risk buying power (max loss of open spreads plus a reserved second wing), which is the buying power a vertical uses. Peak gross was about $2,400, against $99,750. Peak open risk was the same figure, against 10% of equity (about $10,000). Peak spread count hit the cap of 5 for the debit book, the condor, and the butterfly. Average spread counts were lower (debit OOS 2.9, condor 1.7, butterfly 2.4, short-dated 1.4, diagonal 0.3, calendar 0.1).

| Candidate | Alpaca requests per daily scan | Share of 200/min at that burst |
| --- | ---: | ---: |
| Debit momentum (10 names) | 12 | 6% |
| Each index strategy (3 names) | 5 | 2.5% |

One external HTTP get covers VIX, VIX9D, VIX3M, VXN, and RVX. It is not part of the Alpaca budget. A combined book can share the two batched stock-bar requests, so the strategies are not additive. Cadence is one scan after the cash close. That is the cadence this backtest is. An hourly short-dated scan was not tested.

### Notes on each candidate

**Condor.** Put credit on the next open, call credit on the open after that. Each wing is its own 0.5% and its own spread slot. The second wing is reserved overnight. OOS left 22 of 96 wing-trades as orphans (the other wing never filled). Holdout left 12 of 72. Orphans are the leg-timing cost, and they are in the P&L. The holdout parameter set was 25-delta, 35 DTE, VIX above its 20-day average. Many OOS quarters had instead picked 45 DTE. High win rate with average R near zero is a book of small credits. The flat-RV stress on the textbook default (16-delta, same VIX filter, no skew, no term structure) was −0.054% a month in sample and −0.042% a month over the OOS dates. The small primary gain is sensitive to treating the VIX variance swap as the ATM vol, which makes credits richer. I would not trade this as a 3% candidate.

**Butterfly.** Body at the rounded spot, wings $5 or $10, debit wing first, credit wing the next session at locked strikes. OOS left 73 of 425 wing-trades unpaired. A large share of exits are wing touches: a $5 wing on SPY or QQQ is about a 1% move, and a daily bar often trades that far. Holdout flipped the sign. Average R is indistinguishable from zero. Flat-RV stress stayed near zero (+0.006% OOS on the default).

**Calendar.** Front rich versus VIX3M, close near the 20-day average, long put calendar, one two-leg order. The signal is rare (about one trade every four OOS months). Holdout fired once and won. That is not confirmation. Debit marks move with the term-structure model. The flat-RV stress, which sets front and back vol equal, was also about zero (−0.003% on the OOS dates for the default). There is no stable carry here after the 25% width cost.

**Diagonal.** Long back option, short front option, trend-aligned, one two-leg order. A 0.60-delta back leg on SPY is often a debit above $5, so one contract already exceeds the $500 max loss and the order is skipped. The holdout choice was the 0.55-delta, 7/35 DTE set, and it did not fill once in the holdout year. Walk-forward, which sometimes picked a 0.50-delta back leg, lost −0.018% a month. The flat-RV stress on the default was slightly positive in sample (+0.027%) and on the OOS dates (+0.006%). The sign moves when the vol model changes. Calendars and diagonals are the model-sensitive pair, and neither survived that check with a real sample.

**Debit momentum.** Ten names, including single stocks that are today's survivors (see data). Textbook default lost −0.290% a month in sample (CI entirely below zero). The in-sample grid pick was the cheap-IV variant at −0.175% a month, still below zero. That same cheap-IV variant was the holdout choice and lost −0.073% a month in the holdout. Walk-forward's +0.026% has a CI from −0.19% to +0.24%. Flat-RV stress on the default was +0.025% over the OOS dates, close to the primary walk-forward number, so this one is less sensitive to the VIX-versus-realized choice than the condor. It is still not a 3% book. About +0.06R a month in the walk-forward versus +6R required.

**Short-dated.** Daily momentum on SPY, QQQ, and IWM, 2–9 DTE because a daily bar cannot mark 0 DTE. Every fit window picked 7 DTE. Losses are consistent: in-sample default −0.851% a month, walk-forward −0.787%, holdout −0.669%. In-sample max drawdown was 51% ($51,752). Average R is about −0.12. More size would scale that loss. The flat-RV stress is the same loss. This is the clearest negative in the set.

### In-sample grid, for the record

These IS-selected means were fit on the same 60 months they are scored on. They are optimistic, and they were not used as the holdout parameters (the holdout fit is the trailing 24 months through 2025-09-30).

| Candidate | IS default | IS selected |
| --- | ---: | ---: |
| Debit momentum | −0.290% [−0.548%, −0.053%] | −0.175% [−0.310%, −0.037%] |
| Short-dated | −0.851% [−1.062%, −0.653%] | −0.578% [−0.789%, −0.372%] |
| Condor | −0.005% [−0.025%, +0.015%] | +0.008% [−0.016%, +0.032%] |
| Butterfly | +0.027% [−0.016%, +0.072%] | same as default |
| Calendar | −0.036% [−0.068%, −0.010%] | −0.010% [−0.038%, +0.017%] |
| Diagonal | −0.010% [−0.054%, +0.030%] | same as default |

## Locked-rule changes

No candidate gets near 3% by breaking a locked rule, so nothing is proposed.

Short-dated average R is negative. Raising the 0.5% budget or the 5-spread cap multiplies a losing trade. The in-sample path already drew down 51% at the locked size.

Debit-momentum average R is about zero. Ten trades a month at +0.3R would be 3R a month, half of the +6R target, and the measured R is +0.008 out of sample. The cap of 5 spreads was hit at peaks and the book still did not produce that R.

Condor and butterfly peak at 5 spreads with average R under +0.01. Filling more wings does not turn a hundredth of an R into 6R.

A 0.60-delta SPY diagonal is often a one-lot debit above $500, so the locked budget skips it. The diagonals that did fit inside $500 lost money out of sample. Letting the budget rise to about 1.5% would buy the skipped contract. That change was not simulated. The contracts that already fit are the evidence, and they do not pay for a 3% month.

## Splits (frozen before the run)

| Window | Dates | Role |
| --- | --- | --- |
| Warmup | 2016-01-04 through the session before 2018-01-02 | Indicators only. No fills. |
| In-sample | 2018-01-02 through 2022-12-30 | 60 months. |
| Walk-forward | 2023-01-03 through 2025-09-30 | 33 months. Refit each quarter on the trailing window in `FOLDS` in `backtests/new_strategies/specs.py`. Trade the next quarter frozen. Open positions keep the exit rules from entry. |
| Holdout fit | 2023-10-02 through 2025-09-30 | Chooses holdout parameters. No holdout price is an input. |
| Holdout | 2025-10-01 through 2026-09-30 | 12 months. One look. |

October 2026 is outside the holdout. Selection: highest mean monthly return on the fit window. Ties break to lower mark-to-market max drawdown, then to the earlier grid index. A variant with fewer than 8 closed trades is not eligible. If none is eligible, the textbook default is kept.

Unique configs: debit momentum 5, short-dated 4, condor 4, butterfly 4, calendar 4, diagonal 3. "Variants tried" counts every fit evaluation, including dropped configs and the two flat-RV stress runs. It does not count the walk-forward path or the holdout path a second time.

The cheap-IV debit filter is the one extra idea. Reason, stated beforehand: a debit spread overpays when implied vol is rich. No other strategy was added.

## What each strategy is

1. **Debit momentum.** Call debit vertical when the close is above an SMA and the lookback return is positive; put debit when both are negative. Long delta about 0.50–0.55, short about 0.25–0.30, 21–42 DTE. Take 50% of max profit, stop at a loss of half the debit, exit with 7 DTE left. Universe: SPY, QQQ, IWM, AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA.
2. **Short-dated debit verticals.** SPY, QQQ, IWM. A 3- or 5-day return beyond 0.3% or 0.5%, same side of the 20-day average. Target 5 or 7 DTE. Daily bars cannot mark 0 DTE or 1 DTE, so entries are 2–9 calendar days from a Friday. Exit with 1 DTE left.
3. **Iron condor, two atomic orders.** Put credit, then call credit the next session. Short strike within 0.08 of the target delta or the order is skipped. Credit must be at least max($0.20, 8% of the $5 width). Gap filter: skip the second wing if the open is more than 1.5 ATR from the signal close. Package take-profit is 50% of the combined credit. Package stop loses the combined credit. A touch of either short strike closes both. Exit with 21 DTE left.
4. **Butterfly, two atomic orders.** Long call fly. Body at the rounded spot, wings one width below and above. Debit vertical first, credit vertical the next session at the locked strikes. Same reserve, gap filter, and orphan rule. Take 50% of (width − net debit), stop at half the net debit, or close on a print through either wing.
5. **Put calendar, one two-leg order.** Short front ATM put, long back ATM put. Enter when VIX9D/VIX3M exceeds the grid ratio and the close is inside the band of the 20-day average. Size to the debit. Take +25% of the debit, stop at −50%, or exit with 2 front DTE left.
6. **Diagonal, one two-leg order.** Trend (close versus SMA and the 20-day return the same sign). Long the back option, short a further-OTM front option. Size to the debit. Take +50%, stop at −50%, or exit with 2 front DTE left.

One structure per symbol. A condor or fly is that structure and uses two of the five spread slots. Fills are alphabetical when several names signal on the same close, and the caps apply to that batch, including working entries.

## Pricing

There is no free OPRA history. Marks are Black-Scholes-Merton on the underlying bar.

- Spot is the Yahoo chart open/high/low/close. That close is already split-adjusted and is not dividend-adjusted. Dividend yield is a constant (SPY 1.3%, QQQ 0.6%, IWM 1.2%, single names 0–0.8%). The discount rate is the FRED 3-month yield known at the prior close, or 2% before the series starts.
- ATM level: SPY = VIX, QQQ = VXN, IWM = RVX, each divided by 100. Single names = 20-day realized vol times the SPY variance premium (VIX / SPY 20-day realized), that premium clamped between 0.8 and 2.0, then the level clamped to 12–150%. VIX is a variance swap and sits above ATM implied vol. Using it raw makes debit spreads more expensive and credit spreads richer. The flat-RV stress is the check. It did not choose parameters, and it was not run on the holdout.
- Term structure: total variance interpolated between VIX9D (9 days), VIX (30), and VIX3M (93). Outside that span, the nearest knot's vol is used. Other underlyings take that shape as a ratio on their own 30-day level.
- Skew, not fitted: m = log(K/S) / (atm √T), iv = atm × (1 − 0.10 m), clamped. OTM puts are richer. OTM calls are cheaper.
- Fill: each leg at mid ± 25% of the full bid-ask width, against the order. Index full width is $0.04 to $0.20 by |delta| and DTE ($0.04 when |delta| ≥ 0.30 and DTE > 21; $0.20 when DTE ≤ 2). Single names are 2.5×, capped at $1, and the width cannot exceed the mid. A short leg bid under $0.01 is not sold. Fee is $0.02 per contract per leg at entry and at exit.
- A signal uses the closed bar. The fill is the next session's open. Vol on session T is the curve from T−1. Stop and target in the same bar: the stop wins. A gap through the open fills at the open. An intrabar touch fills at the stop or target limit.
- Size is floor(0.5% of realized equity / max loss per contract), at most 10 contracts. The 10-lot cap is because the width model is a one-lot quote. Open risk, including a reserved second wing, stays at or under 10% of realized equity and at or under $99,750. At most 5 spreads.

Flat-RV stress: implied vol is 1.15 × 20-day realized, clamped 15–125%, no term structure and no skew, textbook default only, IS window and OOS window only.

How much the results move when that stress replaces the primary surface:

| Candidate | Primary OOS (walk-forward or, for the stress column, the default path) | Flat-RV on the default, same OOS dates |
| --- | ---: | ---: |
| Condor | walk-forward +0.011%; the stress is a different path | −0.042% [−0.078%, −0.005%] |
| Butterfly | walk-forward +0.011% | +0.006% [−0.084%, +0.100%] |
| Calendar | walk-forward −0.013% | −0.003% [−0.010%, +0.004%] |
| Diagonal | walk-forward −0.018% | +0.006% [−0.031%, +0.044%] |
| Debit momentum | walk-forward +0.026% | +0.025% [−0.280%, +0.333%] |
| Short-dated | walk-forward −0.787% | −0.839% [−1.091%, −0.575%] |

The condor is the strategy whose sign depends on the vol level. The diagonal's sign also moves, on a small sample. Calendars stay near zero either way, on a small sample. Debit momentum and the short-dated book do not.

## Data

| Series | Source | Limit |
| --- | --- | --- |
| Underlying OHLC | Yahoo chart API, daily, 2016-01-04 through 2026-10-01 | Split-adjusted. No historical options volume, so the universe is today's liquid names. AAPL, MSFT, NVDA, AMZN, META, GOOGL, and TSLA are survivorship-biased. SPY, QQQ, and IWM are not. |
| VIX, DGS3MO | FRED VIXCLS, DGS3MO | VIX is a variance swap, not a listed strike. |
| VIX9D, VIX3M, VXN, RVX | CBOE daily history files | VIX9D starts in 2011, which covers this warmup. |

No Alpaca key is used. Cache: `docs/research/new_strategies_options/data/`. Early exercise is ignored (European marks on American options). That matters most for short-dated in-the-money legs; most shorts here are out of the money. Same-bar stop fills assume the stop limit is available inside the bar. A gap through the open does not.

## Files

- `backtests/new_strategies/` — research only. The live package does not import it.
- `docs/research/new_strategies_options/results.json` — metrics, fold choices, variant log.
- `docs/research/new_strategies_options/monthly_returns.csv`
- `docs/research/new_strategies_options/trades.csv`
- `docs/research/new_strategies_options/holdout_log.json` — one holdout evaluation, 2026-10-04T01:16:52Z.
- `tests/test_new_strategies.py` — no-lookahead, fill worse than mid, caps, bootstrap seed, condor wings on different sessions.
