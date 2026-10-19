# Cross-asset trend: pre-registration (2026-10-20)

This record is committed **before any return from this sleeve exists**. Results
are appended in a later commit, under "Results". Nothing above that heading
changes after the first backtest runs.

## Why
No incumbent can clear D10. Measured 2026-10-19 on the KAN-120 bars:

- **The sleeves are mostly market exposure.** Buying and holding the S&P 500
  (Sharpe 0.79) beat every sleeve over the same ten years. Each sleeve's
  return beyond its market beta is an information ratio of 0.0–0.26.
- **Combining them does not help.** All six equally weighted reach 0.73, and
  the five without the hedge 0.76.
- **The bar is a Sharpe of about 1.16.** SR* is 0.64 at 9 trials, and a sleeve
  needs about 0.52 above it over 10 years of daily returns.
- **More momentum tweaks cannot get there** (`momentum-v2-2026-10.md`).

So the open question is whether a return source that is *not* US large-cap
equity beta can reach the bar. Trend-following across asset classes is the
best-documented one that a long-only ETF account can trade. It holds an asset
only while its price trend is up, so it spends equity bear markets partly in
bonds, commodities or cash.

## The candidate (exactly one)
`cross_asset_trend` is Faber's *A Quantitative Approach to Tactical Asset
Allocation* (2007) timing model, with one data-driven substitution:

| | Specification |
|---|---|
| Universe | `VTI` (US equities), `EFA` (developed ex-US equities), `AGG` (US aggregate bonds), `VNQ` (US REITs), `DBC` (commodities) |
| Signal | at each decision, hold an asset when its close is above its 210-session simple moving average (≈ 10 months), else hold none of it |
| Decision day | the **first** trading session of each calendar month, on that session's close. A date counts as the first session when the previous session in the bar series falls in another month, which needs no knowledge of future dates. Faber decides at month-end, so this is one session later. |
| Orders | on the decision day only. Sell a held asset whose signal is off; buy an unheld asset whose signal is on. Nothing in between. |
| Sizing | 19% of the sleeve's initial capital per asset (`_entry_quantity`, as every sleeve sizes). Five assets fill at most 95%, which leaves room for costs. |
| Exits | the trend rule only: **no trailing stop and no max-loss stop**. The moving average is the exit. |
| Uninvested capital | cash at 0%. A USD 3.7k IBKR account earns no interest. |
| Risk engine | `position_entry_limit_pct` 20, `sector_concentration_pct` 100, `total_exposure_limit_pct` 150, `max_lots_per_ticker` 1 |

**`AGG` replaces Faber's `IEF`, for data availability only.** Through `SMART`,
IB returns no `IEF` bars before 2017-08-04, a gap that is probably its exchange
move. `AGG` has the full history. The swap was made on 2026-10-20 from bar
counts alone, before any return from either ETF was computed.

**Nothing here is tuned.** The 210 sessions, the five assets, the equal weights
and monthly timing are Faber's published choices, not values picked from our
data. None of them is swept.

### The declared second trial
`cross_asset_tsmom` is the same sleeve with a different signal: hold when the
close is above the close 252 sessions earlier, the sign rule of 12-month
time-series momentum (Moskowitz, Ooi & Pedersen 2012). It is run beside the
candidate because the deflation needs at least two candidates from this
thread to estimate their spread. **It is not eligible for selection.** If it
beats the candidate, the candidate is still the candidate. Adopting the
second trial instead would be a new trial with its own registry entry.

## Evaluation protocol
- **Search size.** `research/trial_registry.json` gains one entry (n_trials 2)
  in this commit. The deflation counts every sleeve-level trial on record:
  - 8 for the original sleeve selection;
  - 3 for momentum v2 (1 candidate + 2 diagnostics, registered in PR #253);
  - these 2;
  - **n = 13 in total.**

  If #253 is not merged, the 3 are counted anyway. They were run.
- **Data.**
  - The backtest window is that of the KAN-120 record: sessions 2016-10-17 to
    2026-10-12 (2,510 days). Warm-up bars from 2015-10-23 feed the first
    signals, and trading starts on 2016-10-17.
  - The bars are IB `TRADES` daily bars fetched 2026-10-20 with
    `fetch_bars_from_ib` (client 77), the same series and adjustment as every
    other sleeve.
    Bars after 2026-10-12 are dropped, so both windows end on the same day.
  - The incumbents' returns come from the momentum-v2 control run: v1, run on
    the bars of `output/backtest_multi_20261013_114528.json` with the refresh's
    flags. It reproduces KAN-120's figures exactly.
  - Costs, fills and sizing are the refresh's: `build_cost_model()` defaults,
    next-open fills, USD 100,000 capital.
- **The verdict (primary) is D10 exactly as the incumbents were judged.**
  - `research.evaluation.multiple_testing.control(..., n_trials=13)` is called
    over 8 candidates: the six incumbent sleeves' daily returns from the
    KAN-120-equivalent artifact, plus both trials here.
  - The candidate passes when DSR ≥ 0.95 **and** it passes BH-FDR at
    q = 0.10. `ic_p` is `1 − PSR(sr, n, skew, kurt, 0)`, as in
    `run_sleeve_evaluation.py`.
- **Reported beside the verdict, and never the verdict:**
  1. **SR\* without the insurance sleeve.** The same call, with
     `tail_risk_hedge` left out of the spread. Its −0.45 Sharpe is most of
     today's SR\*. Whether an insurance sleeve belongs in that estimate is a
     framework question the operator has not decided. This figure is
     pre-registered here so that, if the rule changes, this sleeve's number
     under it was fixed in advance rather than computed afterwards.
  2. **Total return.** The same run on IB `ADJUSTED_LAST` (dividend-adjusted)
     bars. `TRADES` bars omit distributions, which matters more for `AGG`
     (about 3% a year) than for the equity sleeves.
  3. **Diversification.**
     - the candidate's correlation to each incumbent and to the S&P proxy
       (−`SH`);
     - its beta and beta-adjusted information ratio;
     - the Sharpe of an equal-weight blend with momentum and with the five
       return-seeking incumbents.
  4. **Rung 0 fit.** The same run at USD 3,700 with `--whole-shares`: how many
     signals cannot buy one share, and the commission drag.
  5. **Drawdown.** Max drawdown, peak-to-recovery time and the longest time
     underwater.
- **Holdout.** No existing split covers this sleeve. `cross_asset_trend_forward`
  is registered in this commit:
  - boundary 2026-10-20, the US session that has not happened yet;
  - horizon 21, embargo 21;
  - min 60 sessions, so not before about mid-January 2027.

  It is spent on the candidate and nothing else.

## Decision rule
- **The D10 verdict is reported as is.** A FAIL is not re-run with other
  parameters, assets or decision days.
- **A PASS does not put the sleeve in paper.** That is the operator's call,
  through the promotion pipeline (gate S, then P). It needs:
  - a `CAPITAL_ALLOCATIONS` change under the D9 funding rule;
  - written kill criteria;
  - a decision on the live risk service's stop-loss and its 20% sector limit,
    neither of which this spec uses.
- **Nothing changes in paper or live in this work.** It is a research script
  and a signal function; `run_paper.py` is untouched.

## What the author had already seen
Disclosed because it bears on how out-of-sample this is:

- **Market and gold/bond figures (2026-10-19).** Over this window: the S&P
  proxy's Sharpe (0.79), `GLD`'s (0.79) and `TLT`'s (−0.28), and their
  correlations to the market. Neither `GLD` nor `TLT` is in this universe.
- **General knowledge of 2016–2026.** The 2018 Q4 sell-off, the 2020 crash,
  the 2022 stock-and-bond drawdown. Any trend rule is known to have been
  tested on the 2022 kind of year.
- **No return from `VTI`, `EFA`, `AGG`, `VNQ` or `DBC` had been computed** when
  this was written.
