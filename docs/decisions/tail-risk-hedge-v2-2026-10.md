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
