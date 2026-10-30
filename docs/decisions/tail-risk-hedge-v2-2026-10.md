# Tail-risk hedge v2: pre-registration (2026-10-30)

This record is committed **before any v2 result exists**. Results are appended
in a later commit, under "Results", and nothing above that heading changes
after the first v2 backtest runs.

## Why
`tail_risk_hedge` holds a basket chosen by the breadth regime (the share of
the universe above its 200-day average): bull GLD/TLT, neutral GLD/TLT/SH,
bear SH/PSQ/SDS/GLD. Any change of regime sells every holding whose entry
regime differs from today's and rebuys the new basket. Two defects follow,
both measured against the sleeve's own documented design.

1. **No hysteresis, so it churns on noise.** The regime has hard thresholds at
   60% and 40%. Breadth that sits near a threshold flips the label one ticker
   at a time. Live, breadth read 60.7% (bull) on 10-26 and 10-27, 60.0%
   (neutral) on 10-28 and 62.1% (bull) on 10-29, and the sleeve sold GLD and
   TLT and bought them back on successive runs at nearly the same prices.
   In the 10-27 refresh artifact (`backtest_multi_20261027_114332.json`):
   - 205 regime flips in 9.2 years; 115 regime spells lasted 3 sessions or
     fewer;
   - 252 of the sleeve's 367 exits were followed by a rebuy of the same ticker
     within 4 days;
   - about USD 552k of notional was sold and rebought on a USD 12,830 sleeve,
     and commissions alone were USD 734 (5.7% of its capital) before
     slippage.
2. **It sells the hedge in a crash.** The regime function has a fourth label,
   `crash` (breadth below 10%), and the sleeve has no basket for it, so a
   crash day reads as "nothing belongs in the allocation" and sells
   everything. `run_backtest.py` exempts the sleeve from the crash entry
   freeze with the comment "Tail-risk hedge operates during crashes", which
   is the opposite of what it does. In the artifact the sleeve held no
   inverse ETF from 2020-03-13 to 2020-03-31, the worst of the COVID crash,
   and GLD on only two of those sessions. Through the whole crash it held
   its bear basket only in one- to three-day bursts between crash days.

## The candidate (exactly one)
`tail_risk_hedge_v2` = v1 with both fixes, and nothing else:

| | v1 (incumbent) | v2 |
|---|---|---|
| Regime source | `compute_regime_by_date` labels | the same breadth series, labelled by `compute_hedge_regime_by_date` |
| Crash (breadth < 10%) | no basket: sells everything | bear basket (crash is a deeper bear) |
| Thresholds | bull > 60%, bear < 40% | the same |
| Hysteresis | none | **5 percentage points**: the regime changes only when breadth is beyond the current regime's edge by the band. Leaving bull needs < 55%; leaving bear needs > 45%; leaving neutral needs > 65% or < 35%. The first session takes its plain label |
| Baskets, weights, sizing, entry/exit mechanics, risk engine | unchanged | unchanged |

The other sleeves keep the plain regime series. Only the hedge reads the
hysteretic one.

**The band is fixed at 5 points before any P&L run, and is not swept.** Before
this record was written, flip counts (not returns) were measured at 0, 2, 3
and 5 points on the 10-27 artifact's breadth: 205, 103, 83 and 57 flips, with
115, 27, 14 and 3 spells of three sessions or fewer. 5 points is the smallest
of those bands that leaves almost no short spells, and it is a quarter of the
20-point neutral bucket. That measurement is disclosed here because it was a
look at the data; it saw no P&L.

**Diagnostic, not a candidate.** `diag-crash` = v1 plus the crash fix only (no
band). It attributes the change between v1 and v2. It is not eligible for
selection: if it beats v2, v2 is still the candidate.

## Evaluation protocol
- **Data.** v1, v2 and `diag-crash` all run from the bars of
  `output/backtest_multi_20261027_114332.json` (`--bars-from-json`) with the
  refresh's flags (`--years 10 --capital 100000 --universe-snapshots
  data/universe/sp500_membership.json --allow-degraded-data`) and
  `--sleeves tail_risk_hedge --capital 12830`, so the sleeve trades the same
  capital as in the six-sleeve run. The v1 run must reproduce the
  artifact's hedge figures (367 trades, end equity USD 10,517.22) before any
  other figure is read; if it does not, the comparison stops there.
- **Objective.** The sleeve is declared `insurance` (D22), as v1 is. Its job is
  the account's drawdown profile (`docs/operations/sleeve-kill-criteria.md`),
  so Sharpe is reported but is not the gate.
- **Stress windows** (SPY peak to trough, fixed here): 2018-09-20 → 2018-12-24,
  2020-02-19 → 2020-03-23, 2022-01-03 → 2022-10-12, 2025-02-19 → 2025-04-08.
- **Account curve.** The six-sleeve aggregate with v2's hedge curve in place of
  v1's; the other five sleeves' curves are taken from the 10-27 artifact
  unchanged. This is exact only if the sleeves share no cash, which the
  KAN-116 per-sleeve books guarantee; the splice is checked by rebuilding the
  artifact's own aggregate from v1 first.

## Decision rule
v2 is adopted in paper when **all three** hold:
1. **Churn.** Regime-change exits rebought within 4 days fall by at least
   half (v1: 252).
2. **Crash payoff.** The sleeve's summed return over the four stress windows
   is no lower than v1's.
3. **Account drawdown.** The aggregate's maximum drawdown is no more than
   0.5 points worse than with v1.

Also reported, not gating: Sharpe, total return, sleeve max drawdown, trade
count, commissions, and each stress window separately. A FAIL is not re-run
with another band.

**Adoption is a correctness change, not an edge claim.** All six incumbents
FAIL D10 and the hedge is not judged by it. If v2 passes, a separate commit
switches the paper run, its shadow replay and the weekly refresh to v2
together, so the live sleeve, the divergence monitor's replay and the
refreshed baseline keep trading the same strategy.

## Known limits
- **The live regime is computed from fewer names.** The paper run computes
  breadth from its own universe (about 140 tickers); the backtest uses the
  point-in-time S&P 500 union (about 680 with bars). Both read the same rule,
  but the live series is noisier. That gap predates v2 and is not addressed
  here.
- **Hysteresis is path-dependent.** The label on a date depends on where the
  series started. A paper run with one or two years of bars and a ten-year
  backtest can disagree until breadth makes its first decisive crossing, after
  which they agree.

## Results (2026-10-30)

All runs use the bars of `output/backtest_multi_20261027_114332.json` and the
flags above. **v1 reproduces the artifact exactly**: 367 trades, end equity
USD 10,517.22, an identical daily curve. Rebuilding the artifact's six-sleeve
aggregate from its sleeve curves matches to 3e-11, so the account splice is
exact.

### Verdict: v2 PASSES all three rules
| | Trades | Rebought ≤ 4 d | Commissions | Total return | Sharpe | Sleeve max DD | Stress sum | Account max DD |
|---|---|---|---|---|---|---|---|---|
| v1 (incumbent) | 367 | 252 | $734 | −18.0% | −0.465 | 19.9% | −2.0% | 15.62% |
| **v2 (candidate)** | **93** | **45** | **$186** | **+1.4%** | **0.053** | **16.3%** | **+4.9%** | **15.23%** |
| diag-crash | 254 | 153 | $508 | −7.6% | −0.090 | 21.4% | −0.7% | 15.33% |

1. Churn: 252 → 45 (−82%; the rule needs −50%). **Pass.**
2. Crash payoff: stress sum −2.0% → +4.9%. **Pass.**
3. Account drawdown: 15.62% → 15.23% (the rule allows up to 16.12%). **Pass.**

### The registered runs are confounded, and a clean window still passes
v2's GLD is one lot bought on 2016-11-03 and **never sold**: 0 GLD trades in
ten years, while GLD went from about $123 to about $380. This comes from
existing behaviour, not from the band:

- Breadth needs 200 bars, so the regime series starts on 2017-08-16. Before
  that, the sleeve reads the missing regime as `bull` and buys GLD and TLT.
- A held lot is sold on a regime change when the regime *at its entry date*
  differs from today's. A lot entered before the series starts has no entry
  regime, so the lookup falls back to today's regime and never registers a
  change.
- GLD is in every basket, so only a day with no basket sells it. In v1 that
  was the crash day 2018-12-24. v2 has no such day, so the 2016 lot is held
  to the end.

This affects the backtest only: every live lot's entry date falls inside the
paper run's regime series. To separate it from the change being tested, all
three variants were run again with `--start-date 2017-08-16`, so that no lot
is opened before the regime exists. This is a **post-hoc diagnostic**, run
after the registered results were seen. It is reported beside the verdict
and does not replace it.

| From 2017-08-16 | Trades | Rebought ≤ 4 d | Commissions | Total return | Sharpe | Sleeve max DD | Stress sum | Account max DD |
|---|---|---|---|---|---|---|---|---|
| v1 | 404 | 287 | $808 | −19.0% | −0.495 | 20.2% | −2.4% | 15.63% |
| **v2** | **151** | **101** | **$302** | **−11.1%** | **−0.243** | **18.4%** | **+6.9%** | **15.20%** |
| diag-crash | 390 | 282 | $780 | −24.2% | −0.560 | 25.4% | +0.1% | 15.32% |

On the clean window v2 still passes all three rules (churn −65%, stress sum
+9.3 points, account drawdown −0.43 points). It is a little less than half
as costly as v1 (−11% against −19%), not the break-even the registered run
shows.

### Per stress window
| Sleeve return | 2018-09-20 → 12-24 | 2020-02-19 → 03-23 | 2022-01-03 → 10-12 | 2025-02-19 → 04-08 |
|---|---|---|---|---|
| v1 (clean) | +2.4% | +0.9% | −5.8% | +0.2% |
| v2 (clean) | +0.1% | +4.5% | −1.6% | +3.8% |
| diag-crash (clean) | +2.4% | +3.7% | −6.2% | +0.2% |

- **The band does most of the work.** diag-crash alone does help the COVID
  window (+3.7% against +0.9%), because it keeps the bear basket. But it
  also holds that basket through the April 2020 rebound, and its total
  return and drawdown are the worst of the three.
- **The band costs speed in a slow slide.** In 2018 Q4 v2 entered bear later,
  because it needed breadth below 35%, and it made +0.1% where v1 made +2.4%.
  In the two fast drawdowns (2020, 2025) and the long one (2022) it did
  better.
- **The account effect is small** (−0.4 points of drawdown), because the hedge
  is 12.8% of the book. Most of the gain is in the sleeve's own cost: about
  250 fewer round trips, and about $500 less commission on a $12,830 sleeve.

### Decision
Adopted in paper, per the decision rule. Adopting v2 is a correctness fix,
not a claim that the sleeve has an edge. Its objective stays `insurance`.
`tail_risk_hedge_v2` is entered in `research/trial_registry.json` at its
registered Sharpe (0.053), and diag-crash at its own (−0.090).

**Follow-up, not done here:** a lot entered before the regime series begins
is never sold by a regime change, as described above. In the weekly refresh
this makes v2's ten-year hedge figure look better than the strategy is,
because the 2016 GLD lot is held throughout. Fixing it would change v1's and
v2's backtests, so it needs its own record.
