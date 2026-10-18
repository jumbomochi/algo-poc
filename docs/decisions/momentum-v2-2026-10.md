# Momentum v2: pre-registration (2026-10-19)

This record is committed **before any v2 result exists**. Results are appended
in a later commit, under "Results", and nothing above that heading changes
after the first v2 backtest runs.

## Why
Momentum is the strongest incumbent sleeve (Sharpe 0.570 on the 10-13 bars
after KAN-120) and fails D10 (DSR 0.358). A code review on 2026-10-17 found two
defects in `make_momentum_signals_fn`. Both are implementation bugs measured
against the sleeve's own documented design, not tuning ideas:

1. **The regime never reaches the sleeve.** No call site passes
   `regime_by_date` (backtest, paper, drill), so `regime` is always
   `"neutral"`. The docstring's bear logic ("inverse ETFs naturally rank high
   and get selected; when the regime turns non-bear they are force-exited")
   therefore buys SH/PSQ whenever they rank top and force-sells them the next
   session: 10 PSQ round trips in the 2022 backtest. The regime-adaptive stops
   (`REGIME_PARAMS`) that the function implements are never used either.
2. **No rank exit.** A holding that falls out of the ranking is kept until a
   stop fires. The sleeve says it holds "the top N"; it only *buys* the top N.

A third finding (the trailing stop arms only in profit) does **not** apply to
momentum: it has an 8% max-loss stop. It is not part of v2.

## The candidate (exactly one)
`momentum_v2` = v1 with both fixes, and nothing else:

| | v1 (incumbent) | v2 |
|---|---|---|
| Regime | none (always neutral) | `compute_regime_by_date(bars_by_ticker)`, the series the other regime-aware sleeves already receive |
| Stops | trailing 10%, max loss 8% | `REGIME_PARAMS` by regime, as the function implements when given a regime |
| Inverse ETFs (`BEAR_TICKERS`) | bought on rank, force-sold next session | bought only while regime is `bear`; force-sold when it is not (unchanged) |
| Rank exit | none | sell when not in the top **10** (= 2 × top_n) of that date's 126-session ranking (membership-filtered, the same ranking entries use) |
| Everything else | top_n 5, lookback 126, size 12%, universe, risk engine | unchanged |

**The rank-exit band is fixed at 2 × top_n = 10 before any run, and is not
swept.** A buffer of top_n would churn on every rank swap at the boundary; 2×
is the conventional hysteresis. Picking it from results would be a second
trial.

## Evaluation protocol
- **Search size.** `research/trial_registry.json` gains one entry (n_trials 1).
  The D10 evaluation runs with `--n-trials 9`: the 8 sleeve-selection trials
  plus this one. (`run_sleeve_evaluation.py` defaults to the sleeve-selection
  entry alone, 8, and refuses a smaller count.)
- **Data.** v1 and v2 are both run from the bars of
  `output/backtest_multi_20261013_114528.json` (`--bars-from-json`, so the
  KAN-119 aliases and KAN-120 corrections apply to both), with the refresh's
  own flags (`--years 10 --capital 100000 --universe-snapshots
  data/universe/sp500_membership.json --allow-degraded-data`). Same code; the
  only difference is `--momentum-variant`.
- **Verdict.** D10 as for the incumbents: DSR ≥ 0.95 passes. Reported beside
  v1's: Sharpe, total return, max drawdown, trade count, inverse-ETF round
  trips, and exit reasons.
- **Diagnostics, not candidates.** Each fix alone (regime only; rank exit
  only) may be run to attribute the change. They are labelled diagnostic and
  are **not eligible for selection**: if one beats v2, v2 is still the
  candidate, and adopting a single fix instead would be a new trial with its
  own registry entry.
- **Holdout.** `forward_2026h2` is not valid for v2 (its scope is searches
  that predate 2026-08-29). A new split, `momentum_v2_forward`, is registered
  in this commit: boundary 2026-10-20 (the first session no one has seen),
  min 60 sessions, so no earlier than mid-January 2027. It is spent on v2 and
  nothing else.

## Decision rule
- **The D10 verdict is reported as is.** A FAIL is not re-run with other
  parameters.
- **Adopting v2 in paper is a separate decision and the operator's.** Both
  changes are bug fixes to the designed strategy, so adopting them is
  defensible on correctness grounds even on a D10 FAIL. That adoption would
  not be an edge claim, and the incumbent's D10 FAIL would stand for the
  sleeve until `momentum_v2_forward` is spent.
- **Nothing changes in paper or live in this work.** The variant is a backtest
  flag; `run_paper.py` is untouched.

## Results
(Appended after the runs.)
