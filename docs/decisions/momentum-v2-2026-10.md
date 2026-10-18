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

## Results (2026-10-19)

All four runs use the bars of `output/backtest_multi_20261013_114528.json` and
the refresh's flags. v1 reproduces KAN-120's figures exactly (0.570 / 157.7% /
25.6%), so the comparison runs on the same footing as the record.

### Verdict: v2 FAILS D10
| | Sharpe | Return | Max DD | DSR (n=9) | Verdict |
|---|---|---|---|---|---|
| v1 (incumbent) | 0.570 | 157.7% | 25.6% | 0.41 | FAIL |
| **v2 (candidate)** | **0.548** | **135.4%** | **26.1%** | **0.39** | **FAIL** |

The evaluation is gate-invalid (coverage below the floor, no acceptance pinned
to these artifacts) and ran against a scratch copy of the holdout registry, so
no split was spent. A gate-valid run would change neither verdict: both DSRs are
less than half the 0.95 bar. Per the decision rule, v2 is not re-run with
other parameters.

### Where it went: the two fixes pull in opposite directions
| Momentum | Sharpe | Return | Max DD | Round trips | Win rate | Avg hold | Inverse-ETF trips (P&L) | Exits |
|---|---|---|---|---|---|---|---|---|
| v1 | 0.570 | 157.7% | 25.6% | 764 | 35.9% | 49.6 d | 10 (−$51) | trailing 611, max loss 142, regime change 10 |
| v2 | 0.548 | 135.4% | 26.1% | 951 | 41.3% | 23.3 d | 4 (−$279) | rank 571, trailing 249, max loss 130 |
| diag: regime only | 0.660 | 202.3% | 26.0% | 640 | 35.5% | 63.0 d | 3 (−$325) | trailing 480, max loss 159 |
| diag: rank exit only | 0.486 | 110.6% | 23.5% | 999 | 41.2% | 21.9 d | 10 (−$43) | rank 538, trailing 341, max loss 109 |

- **The regime fix works as designed.** The bought-then-force-sold inverse-ETF
  churn is gone (10 round trips to 3–4, now held through a bear regime). The
  regime-adaptive stops hold winners longer (63 vs 50 days). Alone, it
  improves every return figure.
- **The rank exit is what costs.** It halves the holding period and sells
  winners on rank noise. It wins more often (41% vs 36%) but on smaller moves,
  and it gives up more than the regime fix gains. On these bars, momentum's
  edge is in letting winners run past the point where they stop ranking top 10.

### What this does and does not license
- **The regime-only variant is not a candidate.** It was a pre-declared
  diagnostic. Its DSR at 10 trials (counting it) is **0.48**: still a FAIL,
  and its in-sample figure is now selection-contaminated, because it was
  picked after seeing these results. Adopting it means a new trial entry, and
  only a forward window can judge it.
- **The bar.** At 9 trials, the Sharpe that a pure-noise search of this size
  wins (SR*) is **0.64** annualised. That is above every incumbent except
  thematic. Holding v2's length, skew (−0.3) and kurtosis (5.1), and the other
  sleeves' spread, fixed:

  | Annualised Sharpe | 0.6 | 0.8 | 1.0 | 1.1 | 1.2 | 1.3 |
  |---|---|---|---|---|---|---|
  | DSR | 0.46 | 0.70 | 0.87 | 0.93 | 0.96 | 0.98 |

  A momentum-like sleeve needs a Sharpe of about **1.15–1.2** over this
  history to clear 0.95. The best configuration seen here is 0.66. Fixing
  defects in the incumbents will not get any of them there; that takes a
  different return source, or a combination that diversifies.
- **Adoption in paper is the operator's call** (decision rule above). The
  defensible version is the regime fix alone, as a correctness change, not an
  edge claim. The 2022-style inverse-ETF churn is a real bug in the live
  sleeve today.
- **`momentum_v2_forward` stays registered and unspent.** Spending a forward
  window on a candidate that already fails in-sample confirms nothing. It is
  spent only if v2 is adopted into paper.
