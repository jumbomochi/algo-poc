# D22: what SR*'s spread is estimated from (2026-10-22)

**Status: PROPOSED.** Merging this PR adopts it. It applies to candidates
registered in `research/trial_registry.json` after the merge. It re-grades
nothing.

## The question
D10 passes a sleeve when its deflated Sharpe (DSR) is at least 0.95. The DSR
compares the sleeve's Sharpe with SR*, the Sharpe that a search of this size
would find by luck. SR* is the spread of trial Sharpes times a factor that
grows with the trial count.

Until now the spread came from whatever sharpes were in the run: the six
incumbents plus the candidate. That has two problems, and they push in
opposite directions.

1. **Insurance in the spread (raises SR*).** `tail_risk_hedge` is built to lose
   a little in normal years and pay off in crashes. Its −0.60 Sharpe is
   intended, not a lucky or unlucky draw from the search. On its own it
   accounts for most of today's SR*. The momentum-v2 and cross-asset records
   both reported a DSR "without the insurance sleeve" beside the verdict, and
   left the rule to the operator.
2. **Survivors only (lowers SR*).** This is the framework's limit 1. The count
   includes `mean_reversion` and `short_term_mr`, which were dropped, but the
   spread does not include their Sharpes. Every failed trial since then
   (momentum v2 and its two diagnostics, both cross-asset trials) is also
   counted and left out of the spread. The spread is truncated, so SR* is
   too small.

Fixing only the first would cut the bar by more than half. That is the lenient
rule, and it is not what this record adopts: it removes the one sample point
that hurts while keeping the bias that helps.

## Decision
SR*'s spread is estimated from **every trial on record whose objective was a
return**:

- **Trials in the run** count at their in-run Sharpe.
- **Every other registered return-objective trial** counts at the annualised
  Sharpe it scored at its own verdict of record.
- **Trials declared `insurance`** are left out of the spread. They stay in the
  count.

Nothing else changes:

- The count is the registry's sleeve-scope total, as before.
- The thresholds stay at DSR ≥ 0.95 and BH-FDR at q = 0.10.
- The holdout and stability rules are unchanged.

## How it is implemented
- **`research/trial_registry.json`.** Each sleeve-level entry carries
  `trials`: a name, an annualised `sharpe`, and an `objective` (`return` or
  `insurance`).
  - A new candidate is listed with `sharpe: null` **before it is scored**, so
    its objective is declared before anyone sees its Sharpe.
  - Its verdict's Sharpe is filled in when the verdict is recorded.
- **`research.evaluation.trial_registry.spread_sharpes(in_run, periods_per_year=252)`**
  builds the sample. It refuses:
  - a name in the run that is not registered;
  - a registered return trial outside the run that has no Sharpe.
- **`control(..., spread_srs=...)`** uses that sample in place of the run's
  own. Leaving it out keeps the old rule, which every verdict before D22 used.

## What it does to the bar
Illustration only, not a verdict. It assumes:

- the next candidate, at n = 14 (13 registered trials plus it), with Sharpe 0.64;
- 2,510 daily observations, skew −0.3 and kurtosis 5.1 (momentum v2's shape);
- incumbents at their verdicts of record (KAN-55), and other trials at the
  Sharpes their records give.

| Rule | Spread sample | SR* (annualised) | Sharpe needed for DSR 0.95 |
|---|---|---|---|
| Today's rule (in-run survivors, hedge in) | 7 | 0.79 | ~1.32 |
| Lenient: hedge out, survivors only | 6 | 0.23 | ~0.76 |
| **D22: hedge out, every return trial in** | 13 | **0.65** | **~1.18** |
| Every trial in, hedge too | 14 | 0.78 | ~1.31 |

`test_committed_registry_reproduces_the_d22_figure` pins the 0.649.

**Read it as a correction, not a relief.**

- The bar moves from about 1.32 to about 1.18 at today's search size, which is
  roughly where it stood at nine trials.
- The dropped mean-reversion sleeves (−0.13 and −0.60) put back almost all of
  the spread that removing the hedge takes out.
- A new sleeve still needs about twice the incumbents' Sharpe.
- Every further failed trial now widens the spread as well as raising the
  count.

## What this does not do
- **It does not re-grade anything.** The six incumbents (KAN-55), momentum v2
  and cross-asset trend keep their verdicts of record, all FAIL.
- **The cross-asset "lenient" report is not this rule.** Its 0.92 used the
  lenient row. The record said that, if a rule were adopted, the primary 0.52
  stands. It does.
- **It does not move a passing candidate into paper.** That is still gate S,
  then gate P, and an operator decision.

## Limits
- **Mixed bases.** The recorded Sharpes come from different engines and bars:
  - `mean_reversion` and `short_term_mr` come from the 2026-05 engine, before
    the next-open fill model;
  - the incumbents come from the D18 artifact;
  - the later trials come from the KAN-120 bars.

  The spread is a cross-trial dispersion, so this matters less than it would
  for a level comparison. A trial re-scored in a later run uses its in-run
  figure.
- **Objectives can be gamed.** The objective is declared before scoring, but
  nothing stops a later entry from calling a weak return sleeve "insurance".
  Labelling a new trial `insurance` needs the same written justification as
  the hedge: what crash it pays in, and how its kill criteria judge it.
- **Two-point spreads are still noisy** (limit 2). With 13 points this is less
  of a problem than it was with 6 or 7.
