# Win-rate study

On the long window the baseline wins 19.8% [16.3%, 23.7%] and expects -$72 [-$79, -$65] per spread (-19.2% [-20.9%, -17.4%] of max risk). 301 of those closes are the 1.5× credit stop. 171 of those stops are through on the hourly close mid. Another 127 are back inside on the mid but still through on the natural debit. Only 3 are back inside even after crossing the spread. The loss is not a wick or a bid/ask artifact. Change the tracked default to No price stop — close only on a structure break (take-profit still 50%). It improves expectancy per unit of risk beyond sampling noise. It does not turn the sleeve into a profit. These also cleared that bar and were not stacked on top: Stop at 2× credit, take-profit still 50%; Stop at 2.5× credit, take-profit still 50%; Stop at 3× credit, take-profit still 50%. No price stop — close only on a structure break (take-profit still 50%) expects -$56 [-$68, -$45] per spread (-15.1% [-18.0%, -12.1%] of max risk). The interval sits entirely below zero. The strategy should not keep running. The entry filters from the equity and crypto bots also stay inside noise. `config/paper-live.yaml` is not in this repo. Recommended values, where any differ from today, are in the decision below and in `config/default.yaml`.

## Is the 1.5× stop a pricing artifact?

The stop is judged on the spread mid, which is how the live mark works. Inside the model the natural debit (short ask minus long bid) is always at least that mid, because the half-spread is a smooth 6% of the mid clamped between $0.05 and $0.25. There is no NBBO flicker to trip the stop. A mid that is through 1.5× credit means the debit you would pay to cross the market is through it as well.

Of 301 baseline credit stops on the long window, 171 are through on the hourly close mid, 116 fire only on the high or low (38.5%), and 14 gap through the open and recover on the mid by the close. Of the stops whose close mid is back inside 1.5× credit, 127 still show a natural debit (short ask minus long bid) through the stop. That leaves 3 stops where even crossing the spread at the hourly close is back inside the stop.

Alpaca historical option quotes were not pulled: OPTIONS_APCA_API_KEY_ID / OPTIONS_APCA_API_SECRET_KEY are not set in this environment, and config/paper-live.yaml is not in the repo.

Yahoo daily option bars are last trades for contracts that are still listed. Expired contracts return no bars. The daily close is not an NBBO mid, and the adverse bound (short high − long low) is not a simultaneous print.

Yahoo matched 13 of 40 sampled spreads on the entry session and 16 on the exit session. Median Yahoo daily close minus model mid, in premium points: entry 0.02, exit -0.45. Of 7 modeled stops with a Yahoo exit day, 3 have a Yahoo spread close through 1.5× credit, 4 stay inside on the close but the short-high/long-low bound crosses, and 0 never reach 1.5× credit inside that daily range.


## Exit, distance, and DTE levers

Same entries as the baseline. One knob changes at a time. A wider stop or a further short changes how long the name stays busy, so the trade list is not a paired subset of the baseline. The difference column is still mean expectancy/max-risk minus the baseline, with a 5,000-draw percentile interval. Stop multiples and the close-versus-wick test use the intrabar mid unless the row says the close. A take-profit fills at a debit of (1 − fraction) × credit, which is half the credit at 50%. Structure break is unchanged.

Distance and delta use the same strike grid and the same 20% credit gate. A 30/20/15-delta short is the listed strike past the 1-point gap whose Black-Scholes |delta| is closest to the target. DTE is the Friday the live picker already chooses, inside a different window. Avg |delta| is the mean absolute short delta at entry.

| Variant | Trades | Trades/week | Win rate (95% CI) | Expectancy $ (95% CI) | Expectancy / max risk (95% CI) | Avg win | Avg loss | Avg |delta| |
| --- | ---: | ---: | --- | --- | --- | ---: | ---: | ---: |
| Baseline — daily confirm, one HVN shelf, 60-minute reconfirm | 430 | 3.02 | 19.8% [16.3%, 23.7%] | -$72 [-$79, -$65] | -19.2% [-20.9%, -17.4%] | $61 | -$105 | 0.37 |
| Stop at 2× credit, take-profit still 50% | 358 | 2.51 | 39.9% [34.9%, 45.0%] | -$58 [-$69, -$48] | -15.6% [-18.3%, -12.7%] | $59 | -$136 | 0.37 |
| Stop at 2.5× credit, take-profit still 50% | 347 | 2.43 | 41.8% [36.6%, 47.0%] | -$57 [-$68, -$46] | -15.2% [-18.1%, -12.2%] | $59 | -$140 | 0.37 |
| Stop at 3× credit, take-profit still 50% | 347 | 2.43 | 41.8% [36.6%, 47.0%] | -$57 [-$68, -$45] | -15.1% [-18.1%, -12.2%] | $59 | -$140 | 0.37 |
| No price stop — close only on a structure break (take-profit still 50%) | 347 | 2.43 | 41.8% [36.6%, 47.0%] | -$56 [-$68, -$45] | -15.1% [-18.0%, -12.1%] | $59 | -$139 | 0.37 |
| 1.5× stop checked on the hourly close, not the wick | 409 | 2.87 | 24.0% [19.8%, 28.1%] | -$63 [-$70, -$56] | -16.8% [-18.6%, -15.0%] | $61 | -$102 | 0.37 |
| Take-profit at 25% of credit, stop still 1.5× | 438 | 3.07 | 25.3% [21.2%, 29.5%] | -$71 [-$77, -$66] | -19.0% [-20.5%, -17.4%] | $30 | -$106 | 0.37 |
| Take-profit at 65% of credit, stop still 1.5× | 426 | 2.99 | 18.1% [14.6%, 21.8%] | -$72 [-$79, -$64] | -19.0% [-20.9%, -17.1%] | $80 | -$105 | 0.37 |
| Short strike at least 2 points beyond invalidation | 338 | 2.37 | 18.0% [13.9%, 22.2%] | -$75 [-$82, -$68] | -19.8% [-21.7%, -17.9%] | $60 | -$105 | 0.37 |
| Short strike at least 5 points beyond invalidation | 157 | 1.10 | 12.1% [7.6%, 17.2%] | -$86 [-$95, -$77] | -22.7% [-25.0%, -20.2%] | $63 | -$107 | 0.36 |
| Friday expiration in 21–35 DTE (target 28) | 334 | 2.34 | 22.2% [18.0%, 26.6%] | -$68 [-$75, -$60] | -18.0% [-20.0%, -16.0%] | $59 | -$104 | 0.37 |
| Friday expiration in 45–60 DTE (target 52) | 557 | 3.91 | 20.3% [16.9%, 23.7%] | -$72 [-$78, -$66] | -19.3% [-20.9%, -17.6%] | $62 | -$106 | 0.38 |
| Short strike nearest 30 delta, still past the 1-point gap | 195 | 1.37 | 22.1% [16.4%, 28.2%] | -$65 [-$75, -$56] | -16.8% [-19.2%, -14.2%] | $57 | -$100 | 0.30 |
| Short strike nearest 20 delta, still past the 1-point gap | 3 | 0.02 | 0.0% [0.0%, 0.0%] | -$103 [-$110, -$95] | -26.1% [-28.2%, -23.9%] | n/a | -$103 | 0.20 |
| Short strike nearest 15 delta, still past the 1-point gap | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a | n/a |

| Variant | Trades | Take-profit | Credit stop | Structure break | Expiration | Open mark |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline — daily confirm, one HVN shelf, 60-minute reconfirm | 430 | 84 | 301 | 40 | 0 | 5 |
| Stop at 2× credit, take-profit still 50% | 358 | 142 | 97 | 113 | 0 | 6 |
| Stop at 2.5× credit, take-profit still 50% | 347 | 144 | 17 | 180 | 0 | 6 |
| Stop at 3× credit, take-profit still 50% | 347 | 144 | 4 | 193 | 0 | 6 |
| No price stop — close only on a structure break (take-profit still 50%) | 347 | 144 | 0 | 197 | 0 | 6 |
| 1.5× stop checked on the hourly close, not the wick | 409 | 97 | 267 | 41 | 0 | 4 |
| Take-profit at 25% of credit, stop still 1.5× | 438 | 111 | 288 | 37 | 0 | 2 |
| Take-profit at 65% of credit, stop still 1.5× | 426 | 76 | 305 | 40 | 0 | 5 |
| Short strike at least 2 points beyond invalidation | 338 | 60 | 240 | 33 | 0 | 5 |
| Short strike at least 5 points beyond invalidation | 157 | 19 | 120 | 18 | 0 | 0 |
| Friday expiration in 21–35 DTE (target 28) | 334 | 73 | 225 | 33 | 0 | 3 |
| Friday expiration in 45–60 DTE (target 52) | 557 | 113 | 385 | 52 | 0 | 7 |
| Short strike nearest 30 delta, still past the 1-point gap | 195 | 43 | 138 | 13 | 0 | 1 |
| Short strike nearest 20 delta, still past the 1-point gap | 3 | 0 | 3 | 0 | 0 | 0 |
| Short strike nearest 15 delta, still past the 1-point gap | 0 | 0 | 0 | 0 | 0 | 0 |

### Difference vs baseline, long window (expectancy / max risk)

| Variant | Difference | 95% CI | Reads as |
| --- | ---: | --- | --- |
| Stop at 2× credit, take-profit still 50% | 0.036 | [0.004, 0.069] | above noise |
| Stop at 2.5× credit, take-profit still 50% | 0.040 | [0.006, 0.074] | above noise |
| Stop at 3× credit, take-profit still 50% | 0.041 | [0.007, 0.074] | above noise |
| No price stop — close only on a structure break (take-profit still 50%) | 0.041 | [0.007, 0.075] | above noise |
| 1.5× stop checked on the hourly close, not the wick | 0.024 | [-0.002, 0.049] | inside noise |
| Take-profit at 25% of credit, stop still 1.5× | 0.002 | [-0.022, 0.025] | inside noise |
| Take-profit at 65% of credit, stop still 1.5× | 0.002 | [-0.024, 0.026] | inside noise |
| Short strike at least 2 points beyond invalidation | -0.006 | [-0.032, 0.019] | inside noise |
| Short strike at least 5 points beyond invalidation | -0.035 | [-0.064, -0.004] | worse than noise |
| Friday expiration in 21–35 DTE (target 28) | 0.012 | [-0.015, 0.039] | inside noise |
| Friday expiration in 45–60 DTE (target 52) | -0.001 | [-0.025, 0.023] | inside noise |
| Short strike nearest 30 delta, still past the 1-point gap | 0.024 | [-0.006, 0.054] | inside noise |
| Short strike nearest 20 delta, still past the 1-point gap | -0.069 | [-0.096, -0.043] | worse than noise |
| Short strike nearest 15 delta, still past the 1-point gap | n/a | n/a | too few trades |

### Recent window (2026-07-06 to 2026-09-25)

| Variant | Trades | Trades/week | Win rate (95% CI) | Expectancy $ (95% CI) | Expectancy / max risk (95% CI) | Avg win | Avg loss |
| --- | ---: | ---: | --- | --- | --- | ---: | ---: |
| Baseline — daily confirm, one HVN shelf, 60-minute reconfirm | 55 | 4.70 | 20.0% [10.9%, 30.9%] | -$68 [-$86, -$50] | -18.2% [-23.0%, -13.1%] | $61 | -$101 |
| Stop at 2× credit, take-profit still 50% | 42 | 3.59 | 38.1% [23.8%, 52.4%] | -$58 [-$87, -$27] | -15.3% [-23.2%, -7.2%] | $59 | -$129 |
| Stop at 2.5× credit, take-profit still 50% | 38 | 3.24 | 42.1% [26.3%, 57.9%] | -$53 [-$87, -$20] | -14.1% [-23.3%, -5.2%] | $59 | -$135 |
| Stop at 3× credit, take-profit still 50% | 38 | 3.24 | 42.1% [26.3%, 57.9%] | -$54 [-$90, -$20] | -14.4% [-23.8%, -5.3%] | $59 | -$137 |
| No price stop — close only on a structure break (take-profit still 50%) | 38 | 3.24 | 42.1% [26.3%, 57.9%] | -$53 [-$87, -$19] | -14.0% [-23.2%, -5.1%] | $59 | -$134 |
| 1.5× stop checked on the hourly close, not the wick | 50 | 4.27 | 28.0% [16.0%, 40.0%] | -$55 [-$75, -$33] | -14.6% [-19.9%, -8.8%] | $61 | -$100 |
| Take-profit at 25% of credit, stop still 1.5× | 57 | 4.87 | 28.1% [17.5%, 40.4%] | -$66 [-$83, -$49] | -17.7% [-22.1%, -13.1%] | $32 | -$105 |
| Take-profit at 65% of credit, stop still 1.5× | 54 | 4.61 | 16.7% [7.4%, 27.8%] | -$70 [-$89, -$50] | -18.7% [-23.8%, -13.3%] | $78 | -$100 |
| Short strike at least 2 points beyond invalidation | 47 | 4.01 | 14.9% [6.4%, 25.5%] | -$74 [-$91, -$55] | -19.6% [-24.3%, -14.4%] | $63 | -$98 |
| Short strike at least 5 points beyond invalidation | 24 | 2.05 | 16.7% [4.2%, 33.3%] | -$80 [-$105, -$49] | -21.2% [-28.3%, -12.6%] | $75 | -$111 |
| Friday expiration in 21–35 DTE (target 28) | 42 | 3.59 | 26.2% [14.3%, 40.5%] | -$59 [-$81, -$36] | -15.7% [-21.6%, -9.4%] | $59 | -$101 |
| Friday expiration in 45–60 DTE (target 52) | 72 | 6.15 | 18.1% [9.7%, 27.8%] | -$71 [-$86, -$55] | -19.2% [-23.4%, -14.9%] | $59 | -$100 |
| Short strike nearest 30 delta, still past the 1-point gap | 17 | 1.45 | 29.4% [11.8%, 52.9%] | -$48 [-$81, -$13] | -12.7% [-21.5%, -3.6%] | $56 | -$91 |
| Short strike nearest 20 delta, still past the 1-point gap | 1 | 0.09 | 0.0% [0.0%, 0.0%] | -$103 [-$103, -$103] | -26.1% [-26.1%, -26.1%] | n/a | -$103 |
| Short strike nearest 15 delta, still past the 1-point gap | 0 | 0.00 | n/a | n/a | n/a | n/a | n/a |

### Difference vs baseline, recent window (expectancy / max risk)

| Variant | Difference | 95% CI | Reads as |
| --- | ---: | --- | --- |
| Stop at 2× credit, take-profit still 50% | 0.029 | [-0.067, 0.124] | inside noise |
| Stop at 2.5× credit, take-profit still 50% | 0.041 | [-0.068, 0.144] | inside noise |
| Stop at 3× credit, take-profit still 50% | 0.038 | [-0.073, 0.143] | inside noise |
| No price stop — close only on a structure break (take-profit still 50%) | 0.042 | [-0.067, 0.145] | inside noise |
| 1.5× stop checked on the hourly close, not the wick | 0.036 | [-0.038, 0.113] | inside noise |
| Take-profit at 25% of credit, stop still 1.5× | 0.005 | [-0.063, 0.072] | inside noise |
| Take-profit at 65% of credit, stop still 1.5× | -0.006 | [-0.078, 0.068] | inside noise |
| Short strike at least 2 points beyond invalidation | -0.014 | [-0.085, 0.056] | inside noise |
| Short strike at least 5 points beyond invalidation | -0.030 | [-0.118, 0.066] | inside noise |
| Friday expiration in 21–35 DTE (target 28) | 0.025 | [-0.054, 0.104] | inside noise |
| Friday expiration in 45–60 DTE (target 52) | -0.010 | [-0.076, 0.055] | inside noise |
| Short strike nearest 30 delta, still past the 1-point gap | 0.055 | [-0.051, 0.161] | inside noise |
| Short strike nearest 20 delta, still past the 1-point gap | -0.079 | [-0.131, -0.031] | worse than noise |
| Short strike nearest 15 delta, still past the 1-point gap | n/a | n/a | too few trades |

### Decision

- **Adopt — Stop at 2× credit, take-profit still 50%.** On the long window, expectancy per unit of risk beats the baseline and the 95% interval on that difference sits entirely above zero. Jul–Sep difference in expectancy/risk is 0.029 [-0.067, 0.124].
- **Adopt — Stop at 2.5× credit, take-profit still 50%.** On the long window, expectancy per unit of risk beats the baseline and the 95% interval on that difference sits entirely above zero. Jul–Sep difference in expectancy/risk is 0.041 [-0.068, 0.144].
- **Adopt — Stop at 3× credit, take-profit still 50%.** On the long window, expectancy per unit of risk beats the baseline and the 95% interval on that difference sits entirely above zero. Jul–Sep difference in expectancy/risk is 0.038 [-0.073, 0.143].
- **Adopt — No price stop — close only on a structure break (take-profit still 50%).** On the long window, expectancy per unit of risk beats the baseline and the 95% interval on that difference sits entirely above zero. Jul–Sep difference in expectancy/risk is 0.042 [-0.067, 0.145].
- **Do not adopt — 1.5× stop checked on the hourly close, not the wick.** Expectancy per unit of risk differs by 0.024 [-0.002, 0.049]. That interval covers zero. Win rate differs by 0.042 [-0.014, 0.097].
- **Do not adopt — Take-profit at 25% of credit, stop still 1.5×.** Expectancy per unit of risk differs by 0.002 [-0.022, 0.025]. That interval covers zero. Average win falls from $61 to $30.
- **Do not adopt — Take-profit at 65% of credit, stop still 1.5×.** Expectancy per unit of risk differs by 0.002 [-0.024, 0.026]. That interval covers zero. Win rate differs by -0.017 [-0.068, 0.032].
- **Do not adopt — Short strike at least 2 points beyond invalidation.** Expectancy per unit of risk differs by -0.006 [-0.032, 0.019]. That interval covers zero. Win rate differs by -0.017 [-0.073, 0.037].
- **Do not adopt — Short strike at least 5 points beyond invalidation.** Expectancy per unit of risk differs by -0.035 [-0.064, -0.004]. That interval sits entirely below zero. Win rate differs by -0.077 [-0.138, -0.011].
- **Do not adopt — Friday expiration in 21–35 DTE (target 28).** Expectancy per unit of risk differs by 0.012 [-0.015, 0.039]. That interval covers zero. Win rate differs by 0.024 [-0.035, 0.082].
- **Do not adopt — Friday expiration in 45–60 DTE (target 52).** Expectancy per unit of risk differs by -0.001 [-0.025, 0.023]. That interval covers zero. Win rate differs by 0.005 [-0.047, 0.055].
- **Do not adopt — Short strike nearest 30 delta, still past the 1-point gap.** Expectancy per unit of risk differs by 0.024 [-0.006, 0.054]. That interval covers zero. Win rate differs by 0.023 [-0.047, 0.092].
- **Do not adopt — Short strike nearest 20 delta, still past the 1-point gap.** 3 trades on the long window is below 30, so the bootstrap interval cannot justify a default change.
- **Do not adopt — Short strike nearest 15 delta, still past the 1-point gap.** 0 trades on the long window is below 30, so the bootstrap interval cannot justify a default change.

The tracked default follows **No price stop — close only on a structure break (take-profit still 50%)**. Copy the same values into `config/paper-live.yaml` on the machine that places paper orders. Other rows that also cleared the bar were not stacked on top.


## What was held fixed

Entries stay a daily strict confirm plus a volume-profile shelf, then the first timing-bar pullback and a timing-bar reconfirm. The applied exit change is No price stop — close only on a structure break (take-profit still 50%). Every other exit knob stays at the baseline. Sizing stays 0.5% of equity per spread and 10% open risk, one spread per name, $5 wide, natural credit at least 20% of width.

Checked-in `config/default.yaml` allows 20 concurrent spreads. The request described a 5-spread cap and a `config/paper-live.yaml` that is not in the tree. The decision uses the per-spread book (no cross-name cap). The 5-spread and 20-spread books are reported beside it; a filter that only looks good because of the cap does not get a default change.

## How the tape was priced

There is no stored option tape, so each vertical is priced with Black-Scholes on the underlying bars. Implied vol is the last 20 sessions of close-to-close realized vol times 1.15, frozen at entry, and clamped between 15% and 125%. Rates are 4% with no dividend. Each leg's half-spread is 6% of its mid, at least $0.05 and at most $0.25. The entry credit is short bid minus long ask, and it has to clear the live 20% width gate. Take-profit and stop are judged on the mid, which is what the live mark uses. A take-profit fills at exactly 50% of the credit (the poll catches the cross; the far side of an hourly wick is not a bigger winner). A stop that gaps through the open fills at the open's natural debit. A stop that trades through fills at 1.5× credit plus the bid/ask, and no better than that bar's worst debit. If both a stop and a take-profit are inside the same bar, the stop wins. A structure break pays the natural debit at the daily close. Expiration, if nothing else fired, settles at intrinsic. Anything still open on the last bar is marked at the natural debit and kept in the averages, so the recent window is not only the trades that happened to finish.

Strike grid: $1 on the ETFs in the full-A list; stocks use $0.50 under $50, $1 under $200, $2.50 under $500, and $5 above that. The short is the listed strike that clears the 1-point gap, via the same `build_proposal` path as the engine. Earnings and FOMC blackouts are empty in `config/calendar.yaml`, so the replay does not skip them either.

Hourly bars run from 2023-10-27 through 2026-09-25 (57 names with both a daily and an hourly tape). The long window starts 2024-01-02 so the hourly history (which begins around 2023-10-27) can warm up. 15-minute bars are only available from 2026-07-02 (Yahoo's intraday limit), so 15-minute versus 60-minute confirmation is scored on Jul 6–Sep 25 2026 only. The 15-minute book still uses the full daily history for the shelf and the invalidation; only the timing series is short.

## Filters

A filter either allows the entry or leaves the arm up for a later bar. It does not change the exit.

- **Skip the open and the midday lull.** The confirmation bar overlaps 09:30–10:00 ET or 11:30–13:30 ET. On a 60-minute tape the 09:30 bar overlaps the first half hour, and the 11:30 and 12:30 bars overlap the lull.
- **Relative strength.** Last 10 completed sessions: the underlying beat SPY for a bull put, or lagged SPY for a bear call. A tie fails. SPY versus itself never passes.
- **Two HVN shelves.** At least two high-volume nodes from the same 25-day profile sit within max(ATR, $1) of the short strike. One shelf is already required by the baseline zone.
- **Daily EMA50.** Bull put only if the last completed close is above the daily EMA50; bear call only if it is below.
- **4-hour EMA50.** Same test on session blocks (09:30–13:30 and 13:30–16:00) rather than a clock-aligned 4-hour bar that would mix the cash close into the next premarket.
- **Confirmation volume.** The timing bar that reconfirms has more volume than the average of the prior 20 timing bars.
- **15-minute vs 60-minute.** The timing series is 15-minute bars instead of 60-minute bars. Lookback stays 120 bars, matching `market_data.bar_lookback`. That is about two sessions of 15-minute data, not 120 hours.
- **The three small point-estimate bumps together.** Relative strength, the 4-hour EMA50, and confirmation volume each had a slightly higher win rate on the point estimate. They are also tested as one stack, all three required.

## Long window (2024-01-02 to 2026-09-25)

Per spread, one name at a time, sized at 0.5% of $100,000. Intervals are 5,000-draw percentile bootstraps. Avg loss is the mean of losing trades (negative).

| Filter | Trades | Trades/week | Win rate (95% CI) | Expectancy $ (95% CI) | Expectancy / max risk (95% CI) | Avg win | Avg loss |
| --- | ---: | ---: | --- | --- | --- | ---: | ---: |
| Baseline — daily confirm, one HVN shelf, 60-minute reconfirm | 430 | 3.02 | 19.8% [16.3%, 23.7%] | -$72 [-$79, -$65] | -19.2% [-20.9%, -17.4%] | $61 | -$105 |
| Skip the first 30 minutes and the midday lull (11:30–13:30 ET) | 319 | 2.24 | 20.1% [15.7%, 24.8%] | -$73 [-$80, -$65] | -19.4% [-21.5%, -17.2%] | $61 | -$106 |
| Relative strength vs SPY (bull put) / weakness (bear call), 10 sessions | 282 | 1.98 | 20.2% [15.6%, 24.8%] | -$71 [-$79, -$63] | -18.8% [-21.0%, -16.6%] | $60 | -$104 |
| Two HVN shelves aligned at the short strike | 289 | 2.03 | 16.6% [12.5%, 21.1%] | -$79 [-$87, -$71] | -21.2% [-23.2%, -19.1%] | $62 | -$108 |
| Daily EMA50 aligned with the spread | 296 | 2.08 | 18.6% [14.5%, 23.0%] | -$74 [-$81, -$66] | -19.8% [-21.7%, -17.7%] | $60 | -$105 |
| 4-hour EMA50 aligned with the spread | 324 | 2.27 | 20.1% [15.7%, 24.7%] | -$72 [-$79, -$64] | -19.0% [-21.0%, -16.9%] | $60 | -$105 |
| Confirmation bar volume above its prior 20-bar average | 225 | 1.58 | 21.3% [16.0%, 26.7%] | -$69 [-$79, -$60] | -18.6% [-21.0%, -16.0%] | $61 | -$105 |
| Relative strength, 4-hour EMA50, and confirmation volume together | 116 | 0.81 | 23.3% [16.4%, 31.0%] | -$63 [-$76, -$50] | -16.8% [-20.3%, -13.3%] | $59 | -$100 |

| Filter | Trades | Take-profit | Stop 1.5× | Structure break | Expiration | Open mark |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline — daily confirm, one HVN shelf, 60-minute reconfirm | 430 | 84 | 301 | 40 | 0 | 5 |
| Skip the first 30 minutes and the midday lull (11:30–13:30 ET) | 319 | 63 | 222 | 31 | 0 | 3 |
| Relative strength vs SPY (bull put) / weakness (bear call), 10 sessions | 282 | 56 | 196 | 25 | 0 | 5 |
| Two HVN shelves aligned at the short strike | 289 | 47 | 216 | 22 | 0 | 4 |
| Daily EMA50 aligned with the spread | 296 | 54 | 206 | 31 | 0 | 5 |
| 4-hour EMA50 aligned with the spread | 324 | 64 | 230 | 25 | 0 | 5 |
| Confirmation bar volume above its prior 20-bar average | 225 | 47 | 153 | 21 | 0 | 4 |
| Relative strength, 4-hour EMA50, and confirmation volume together | 116 | 26 | 76 | 10 | 0 | 4 |

### Difference vs baseline (expectancy / max risk)

| Filter | Difference | 95% CI | Reads as |
| --- | ---: | --- | --- |
| Skip the first 30 minutes and the midday lull (11:30–13:30 ET) | -0.002 | [-0.029, 0.025] | inside noise |
| Relative strength vs SPY (bull put) / weakness (bear call), 10 sessions | 0.004 | [-0.024, 0.033] | inside noise |
| Two HVN shelves aligned at the short strike | -0.020 | [-0.047, 0.007] | inside noise |
| Daily EMA50 aligned with the spread | -0.006 | [-0.033, 0.020] | inside noise |
| 4-hour EMA50 aligned with the spread | 0.002 | [-0.026, 0.028] | inside noise |
| Confirmation bar volume above its prior 20-bar average | 0.006 | [-0.023, 0.037] | inside noise |
| Relative strength, 4-hour EMA50, and confirmation volume together | 0.024 | [-0.016, 0.062] | inside noise |

## Recent window (2026-07-06 to 2026-09-25)

| Filter | Trades | Trades/week | Win rate (95% CI) | Expectancy $ (95% CI) | Expectancy / max risk (95% CI) | Avg win | Avg loss |
| --- | ---: | ---: | --- | --- | --- | ---: | ---: |
| Baseline — daily confirm, one HVN shelf, 60-minute reconfirm | 55 | 4.70 | 20.0% [10.9%, 30.9%] | -$68 [-$86, -$50] | -18.2% [-23.0%, -13.1%] | $61 | -$101 |
| Skip the first 30 minutes and the midday lull (11:30–13:30 ET) | 43 | 3.67 | 18.6% [7.0%, 30.2%] | -$73 [-$92, -$52] | -19.6% [-24.8%, -13.8%] | $58 | -$103 |
| Relative strength vs SPY (bull put) / weakness (bear call), 10 sessions | 38 | 3.24 | 28.9% [15.8%, 44.7%] | -$49 [-$73, -$24] | -13.1% [-19.5%, -6.1%] | $61 | -$94 |
| Two HVN shelves aligned at the short strike | 45 | 3.84 | 20.0% [8.9%, 31.1%] | -$70 [-$90, -$49] | -18.7% [-24.1%, -13.0%] | $61 | -$103 |
| Daily EMA50 aligned with the spread | 41 | 3.50 | 22.0% [9.8%, 36.6%] | -$63 [-$84, -$40] | -16.9% [-22.5%, -10.7%] | $56 | -$97 |
| 4-hour EMA50 aligned with the spread | 40 | 3.41 | 22.5% [10.0%, 35.0%] | -$61 [-$82, -$39] | -16.3% [-21.9%, -10.3%] | $58 | -$96 |
| Confirmation bar volume above its prior 20-bar average | 29 | 2.48 | 24.1% [10.3%, 41.4%] | -$58 [-$82, -$31] | -15.7% [-22.3%, -8.4%] | $54 | -$94 |
| Relative strength, 4-hour EMA50, and confirmation volume together | 16 | 1.37 | 43.8% [18.8%, 68.8%] | -$15 [-$48, $18] | -3.9% [-12.7%, 4.7%] | $54 | -$68 |

| Filter | Trades | Take-profit | Stop 1.5× | Structure break | Expiration | Open mark |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline — daily confirm, one HVN shelf, 60-minute reconfirm | 55 | 10 | 39 | 1 | 0 | 5 |
| Skip the first 30 minutes and the midday lull (11:30–13:30 ET) | 43 | 7 | 32 | 1 | 0 | 3 |
| Relative strength vs SPY (bull put) / weakness (bear call), 10 sessions | 38 | 10 | 22 | 1 | 0 | 5 |
| Two HVN shelves aligned at the short strike | 45 | 8 | 32 | 1 | 0 | 4 |
| Daily EMA50 aligned with the spread | 41 | 8 | 27 | 1 | 0 | 5 |
| 4-hour EMA50 aligned with the spread | 40 | 8 | 26 | 1 | 0 | 5 |
| Confirmation bar volume above its prior 20-bar average | 29 | 6 | 18 | 1 | 0 | 4 |
| Relative strength, 4-hour EMA50, and confirmation volume together | 16 | 6 | 5 | 1 | 0 | 4 |

### Difference vs baseline (expectancy / max risk)

| Filter | Difference | 95% CI | Reads as |
| --- | ---: | --- | --- |
| Skip the first 30 minutes and the midday lull (11:30–13:30 ET) | -0.014 | [-0.088, 0.062] | inside noise |
| Relative strength vs SPY (bull put) / weakness (bear call), 10 sessions | 0.051 | [-0.033, 0.138] | inside noise |
| Two HVN shelves aligned at the short strike | -0.005 | [-0.081, 0.073] | inside noise |
| Daily EMA50 aligned with the spread | 0.012 | [-0.063, 0.091] | inside noise |
| 4-hour EMA50 aligned with the spread | 0.019 | [-0.060, 0.095] | inside noise |
| Confirmation bar volume above its prior 20-bar average | 0.025 | [-0.057, 0.113] | inside noise |
| Relative strength, 4-hour EMA50, and confirmation volume together | 0.143 | [0.041, 0.242] | above noise |

Relative strength, 4-hour EMA50, and confirmation volume together is the recent-window row whose expectancy interval sits above zero. It still does not clear the rule: the same filter on the long window covers zero, and this recent sample is a handful of spreads. The point estimate is still a loss per spread.

## 15-minute vs 60-minute confirmation

Same daily shelf and the same exits. Only the timing bar changes. Both rows are entries from Jul 6 through Sep 25 2026. The 15-minute tape itself starts 2026-07-02, so early-July timing swings are thin.

| Filter | Trades | Trades/week | Win rate (95% CI) | Expectancy $ (95% CI) | Expectancy / max risk (95% CI) | Avg win | Avg loss |
| --- | ---: | ---: | --- | --- | --- | ---: | ---: |
| Baseline — daily confirm, one HVN shelf, 60-minute reconfirm | 55 | 4.70 | 20.0% [10.9%, 30.9%] | -$68 [-$86, -$50] | -18.2% [-23.0%, -13.1%] | $61 | -$101 |
| Same rules, 15-minute reconfirm instead of 60-minute | 90 | 7.68 | 18.9% [11.1%, 27.8%] | -$77 [-$91, -$62] | -20.5% [-24.4%, -16.6%] | $60 | -$108 |

15-minute minus 60-minute expectancy/risk: -0.023 [-0.088, 0.039]. inside noise.

## Book caps

Same signals, then a greedy book: max concurrent spreads, 0.5% of *realized* equity, 10% open risk, exits freeing a slot before a same-time entry. This can only drop trades. It does not create a later signal on a name whose earlier signal was skipped.

| Book | Filter | Trades | Trades/week | Win rate | Expectancy $ | Expectancy / max risk |
| --- | --- | ---: | ---: | --- | --- | --- |
| max 5, 2024-01-02–2026-09-25 | Baseline — daily confirm, one HVN shelf, 60-minute reconfirm | 347 | 2.43 | 18.4% [14.4%, 22.5%] | -$75 [-$82, -$67] | -20.0% [-21.9%, -18.0%] |
| max 5, 2024-01-02–2026-09-25 | Skip the first 30 minutes and the midday lull (11:30–13:30 ET) | 295 | 2.07 | 19.0% [14.6%, 23.4%] | -$74 [-$82, -$67] | -19.9% [-22.0%, -17.8%] |
| max 5, 2024-01-02–2026-09-25 | Relative strength vs SPY (bull put) / weakness (bear call), 10 sessions | 267 | 1.87 | 20.2% [15.7%, 25.1%] | -$71 [-$79, -$63] | -19.0% [-21.1%, -16.7%] |
| max 5, 2024-01-02–2026-09-25 | Two HVN shelves aligned at the short strike | 272 | 1.91 | 15.1% [11.0%, 19.1%] | -$82 [-$90, -$75] | -22.0% [-24.0%, -19.9%] |
| max 5, 2024-01-02–2026-09-25 | Daily EMA50 aligned with the spread | 284 | 1.99 | 18.3% [13.7%, 22.9%] | -$75 [-$83, -$67] | -20.0% [-22.1%, -17.8%] |
| max 5, 2024-01-02–2026-09-25 | 4-hour EMA50 aligned with the spread | 300 | 2.10 | 19.0% [14.7%, 23.3%] | -$74 [-$81, -$66] | -19.5% [-21.6%, -17.4%] |
| max 5, 2024-01-02–2026-09-25 | Confirmation bar volume above its prior 20-bar average | 219 | 1.54 | 20.5% [15.5%, 26.0%] | -$71 [-$80, -$62] | -19.0% [-21.5%, -16.4%] |
| max 5, 2024-01-02–2026-09-25 | Relative strength, 4-hour EMA50, and confirmation volume together | 115 | 0.81 | 23.5% [15.7%, 31.3%] | -$63 [-$77, -$50] | -16.9% [-20.4%, -13.4%] |
| max 5, 2026-07-06–2026-09-25 | Baseline — daily confirm, one HVN shelf, 60-minute reconfirm | 47 | 4.01 | 23.4% [10.6%, 36.2%] | -$65 [-$86, -$44] | -17.4% [-23.0%, -11.4%] |
| max 5, 2026-07-06–2026-09-25 | Skip the first 30 minutes and the midday lull (11:30–13:30 ET) | 38 | 3.24 | 21.1% [7.9%, 34.2%] | -$72 [-$94, -$49] | -19.2% [-25.2%, -12.9%] |
| max 5, 2026-07-06–2026-09-25 | Relative strength vs SPY (bull put) / weakness (bear call), 10 sessions | 36 | 3.07 | 30.6% [16.7%, 44.4%] | -$51 [-$75, -$25] | -13.4% [-20.0%, -6.6%] |
| max 5, 2026-07-06–2026-09-25 | Two HVN shelves aligned at the short strike | 39 | 3.33 | 23.1% [10.3%, 35.9%] | -$69 [-$91, -$44] | -18.4% [-24.6%, -11.7%] |
| max 5, 2026-07-06–2026-09-25 | Daily EMA50 aligned with the spread | 37 | 3.16 | 24.3% [10.8%, 40.5%] | -$62 [-$84, -$38] | -16.6% [-22.6%, -9.9%] |
| max 5, 2026-07-06–2026-09-25 | 4-hour EMA50 aligned with the spread | 36 | 3.07 | 25.0% [11.1%, 38.9%] | -$62 [-$85, -$38] | -16.5% [-22.6%, -10.0%] |
| max 5, 2026-07-06–2026-09-25 | Confirmation bar volume above its prior 20-bar average | 27 | 2.30 | 25.9% [11.1%, 44.4%] | -$56 [-$83, -$28] | -15.3% [-22.5%, -7.6%] |
| max 5, 2026-07-06–2026-09-25 | Relative strength, 4-hour EMA50, and confirmation volume together | 15 | 1.28 | 46.7% [20.0%, 73.3%] | -$13 [-$48, $22] | -3.5% [-12.8%, 5.9%] |
| max 20, 2024-01-02–2026-09-25 | Baseline — daily confirm, one HVN shelf, 60-minute reconfirm | 367 | 2.57 | 19.6% [15.5%, 23.7%] | -$73 [-$80, -$66] | -19.5% [-21.4%, -17.5%] |
| max 20, 2024-01-02–2026-09-25 | Skip the first 30 minutes and the midday lull (11:30–13:30 ET) | 311 | 2.18 | 19.6% [15.4%, 24.1%] | -$73 [-$81, -$65] | -19.6% [-21.7%, -17.5%] |
| max 20, 2024-01-02–2026-09-25 | Relative strength vs SPY (bull put) / weakness (bear call), 10 sessions | 282 | 1.98 | 20.2% [15.6%, 24.8%] | -$71 [-$79, -$63] | -18.8% [-21.0%, -16.6%] |
| max 20, 2024-01-02–2026-09-25 | Two HVN shelves aligned at the short strike | 281 | 1.97 | 16.0% [12.1%, 20.3%] | -$80 [-$88, -$73] | -21.5% [-23.5%, -19.4%] |
| max 20, 2024-01-02–2026-09-25 | Daily EMA50 aligned with the spread | 291 | 2.04 | 18.2% [13.7%, 22.7%] | -$75 [-$83, -$67] | -20.0% [-22.0%, -17.9%] |
| max 20, 2024-01-02–2026-09-25 | 4-hour EMA50 aligned with the spread | 312 | 2.19 | 19.9% [15.4%, 24.4%] | -$72 [-$80, -$64] | -19.2% [-21.2%, -17.1%] |
| max 20, 2024-01-02–2026-09-25 | Confirmation bar volume above its prior 20-bar average | 225 | 1.58 | 21.3% [16.0%, 26.7%] | -$69 [-$79, -$60] | -18.6% [-21.0%, -16.0%] |
| max 20, 2024-01-02–2026-09-25 | Relative strength, 4-hour EMA50, and confirmation volume together | 116 | 0.81 | 23.3% [16.4%, 31.0%] | -$63 [-$76, -$50] | -16.8% [-20.3%, -13.3%] |
| max 20, 2026-07-06–2026-09-25 | Baseline — daily confirm, one HVN shelf, 60-minute reconfirm | 55 | 4.70 | 20.0% [10.9%, 30.9%] | -$68 [-$86, -$50] | -18.2% [-23.0%, -13.1%] |
| max 20, 2026-07-06–2026-09-25 | Skip the first 30 minutes and the midday lull (11:30–13:30 ET) | 43 | 3.67 | 18.6% [7.0%, 30.2%] | -$73 [-$92, -$52] | -19.6% [-24.8%, -13.8%] |
| max 20, 2026-07-06–2026-09-25 | Relative strength vs SPY (bull put) / weakness (bear call), 10 sessions | 38 | 3.24 | 28.9% [15.8%, 44.7%] | -$49 [-$73, -$24] | -13.1% [-19.5%, -6.1%] |
| max 20, 2026-07-06–2026-09-25 | Two HVN shelves aligned at the short strike | 45 | 3.84 | 20.0% [8.9%, 31.1%] | -$70 [-$90, -$49] | -18.7% [-24.1%, -13.0%] |
| max 20, 2026-07-06–2026-09-25 | Daily EMA50 aligned with the spread | 41 | 3.50 | 22.0% [9.8%, 36.6%] | -$63 [-$84, -$40] | -16.9% [-22.5%, -10.7%] |
| max 20, 2026-07-06–2026-09-25 | 4-hour EMA50 aligned with the spread | 40 | 3.41 | 22.5% [10.0%, 35.0%] | -$61 [-$82, -$39] | -16.3% [-21.9%, -10.3%] |
| max 20, 2026-07-06–2026-09-25 | Confirmation bar volume above its prior 20-bar average | 29 | 2.48 | 24.1% [10.3%, 41.4%] | -$58 [-$82, -$31] | -15.7% [-22.3%, -8.4%] |
| max 20, 2026-07-06–2026-09-25 | Relative strength, 4-hour EMA50, and confirmation volume together | 16 | 1.37 | 43.8% [18.8%, 68.8%] | -$15 [-$48, $18] | -3.9% [-12.7%, 4.7%] |

## Decision

- **Do not adopt — Skip the first 30 minutes and the midday lull (11:30–13:30 ET).** Expectancy per unit of risk differs by -0.002 [-0.029, 0.025]. That interval covers zero. Win rate differs by 0.003 [-0.055, 0.062].
- **Do not adopt — Relative strength vs SPY (bull put) / weakness (bear call), 10 sessions.** Expectancy per unit of risk differs by 0.004 [-0.024, 0.033]. That interval covers zero. Win rate differs by 0.004 [-0.055, 0.066].
- **Do not adopt — Two HVN shelves aligned at the short strike.** Expectancy per unit of risk differs by -0.020 [-0.047, 0.007]. That interval covers zero. Win rate differs by -0.032 [-0.087, 0.025].
- **Do not adopt — Daily EMA50 aligned with the spread.** Expectancy per unit of risk differs by -0.006 [-0.033, 0.020]. That interval covers zero. Win rate differs by -0.012 [-0.070, 0.043].
- **Do not adopt — 4-hour EMA50 aligned with the spread.** Expectancy per unit of risk differs by 0.002 [-0.026, 0.028]. That interval covers zero. Win rate differs by 0.003 [-0.057, 0.059].
- **Do not adopt — Confirmation bar volume above its prior 20-bar average.** Expectancy per unit of risk differs by 0.006 [-0.023, 0.037]. That interval covers zero. Win rate differs by 0.016 [-0.047, 0.081].
- **Do not adopt — Relative strength, 4-hour EMA50, and confirmation volume together.** Expectancy per unit of risk differs by 0.024 [-0.016, 0.062]. That interval covers zero. Win rate differs by 0.035 [-0.052, 0.122].

- **Do not adopt — 15-minute confirmation.** 15-minute confirmation does not beat 60-minute confirmation on expectancy or on win rate without giving expectancy back.

Nothing here beat the baseline by enough to move a default. The playbook in `config/default.yaml` stays as it is.

## Reproduce

```bash
python -m alpaca_options_credit.replay.study --cache var/replay-cache --out docs/win-rate-study.md
```

The cache is Yahoo chart JSON under `var/` (gitignored). Rerunning with the cache warm does not hit the network. Bootstrap seed is 20260925, 5,000 resamples.

## Signal counts

Ready means the hybrid entry fired while that variant was flat. A filter reject keeps the arm. A credit skip means the modeled quote failed the live gate (missing bid, debit, or under 20% of width). Opened includes the short warmup before 2024-01-02; the tables above do not.

| Filter | Ready | Filter reject | Credit skip | Underwater block | Opened |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline — daily confirm, one HVN shelf, 60-minute reconfirm | 4604 | 0 | 4163 | 0 | 441 |
| Skip the first 30 minutes and the midday lull (11:30–13:30 ET) | 4835 | 2085 | 2422 | 0 | 328 |
| Relative strength vs SPY (bull put) / weakness (bear call), 10 sessions | 4880 | 2034 | 2556 | 0 | 290 |
| Two HVN shelves aligned at the short strike | 4959 | 345 | 4319 | 0 | 295 |
| Daily EMA50 aligned with the spread | 4849 | 1093 | 3451 | 0 | 305 |
| 4-hour EMA50 aligned with the spread | 4789 | 931 | 3531 | 0 | 327 |
| Confirmation bar volume above its prior 20-bar average | 4952 | 3049 | 1674 | 0 | 229 |
| Relative strength, 4-hour EMA50, and confirmation volume together | 5170 | 4151 | 902 | 0 | 117 |
