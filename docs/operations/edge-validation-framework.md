# Edge Validation Framework

The direction doc's D10 finding is that a matching backtest proves
implementation fidelity, never edge. This document is the binding: what gate S
and gate P require beyond fidelity, where each number comes from, and how to
keep the inputs honest.

Scope note: most of this framework already existed before KAN-38. Deflated
Sharpe, probabilistic Sharpe, Benjamini–Hochberg FDR, and purged/embargoed
nested walk-forward live in `research/evaluation/` and are tested. KAN-38 added
the two things that were missing — an explicit trial count and a pre-registered
holdout — and wrote down the binding below. KAN-39 added the last missing
piece, parameter stability.

## What gate S and gate P require

| Check | Where it comes from | Passing bar |
|---|---|---|
| Deflated Sharpe against the **declared** trial count | `research/evaluation/multiple_testing.py` `control(..., n_trials=)`, count from `research/trial_registry.json` | `passes_dsr` at the 0.95 default threshold |
| Multiple-testing correction across the candidates in the run | `benjamini_hochberg` at `fdr_q` (default 0.10) | `passes_fdr` |
| Performance on the **pre-registered** holdout | `research/evaluation/holdout.py` | Registered before the look; evaluated once |
| Parameter stability | `research/evaluation/stability.py` `parameter_stability`, fed by `scripts/run_stability_sweep.py` | `is_plateau` true for every parameter swept |

A candidate that clears the walk-forward but not the deflation has not shown
edge; it has shown that a search of that size finds numbers like that by
chance.

## The trial registry

`research/trial_registry.json` records how large the search really is.

The problem it solves: `control()` used to derive the López de Prado SR\*
benchmark from the Sharpes of whatever candidates were in the current run.
Scoring four factors today deflates against four trials — but those four
factors are one slice of a search that also tried eight sleeves before six
survived. Deflating against four understates the selection bias, and no single
run can know better, because the history is not in the run.

So the count is declared, in a committed file, and read by
`declared_trial_count()`. `EvaluationConfig.n_trials` defaults to `None`,
which means "use the registry"; `scripts/run_factor_evaluation.py --n-trials`
overrides it upward when you want to model a larger search than the registry
records. The resolved count is written into every run card's `config` block,
so any historical run card says what it was deflated against.

**Adding an entry.** Whenever a search happens — a new strategy, a new factor,
a parameterization sweep, a threshold that got tuned — add an entry in the same
commit as the search:

```json
{
  "searched_at": "2026-09-01",
  "what": "Regime-filter thread: three volatility-regime cutoffs swept",
  "n_trials": 3,
  "source": "research/factors/catalog.py"
}
```

The total is the sum of the entries, which is deliberately conservative: a
candidate that survives today was selected against the whole history of the
search, not just the batch it happened to be scored in. Declaring a count below
the number of candidates in a run is rejected outright — under-declaring is the
exact failure the argument exists to prevent.

### Three limits to state when you cite a deflated Sharpe

1. **The spread is estimated from survivors.** `n_trials` supplies the count,
   but the standard deviation of trial Sharpes still comes from the candidates
   in the run — which are what *survived*. The dropped low-Sharpe tail is
   counted in `m` and absent from the sample, so the estimated spread is
   truncated, `SR*` is too small, and the correction still **under**-deflates.
   The direction of the residual error is at least the safe one to know about.
   Carrying per-entry SR dispersion in the registry would fix this properly;
   it is not built.
2. **Two candidates is a one-degree-of-freedom variance.** With a small
   candidate set, `SR*` is dominated by sampling noise and a large `n_trials`
   merely scales that noise. Declaring a count against fewer than two
   candidates is refused outright rather than silently returning zero
   deflation — the run card would otherwise record a correction that never
   happened.
3. **The sum is unscoped.** Every entry counts toward every run, so a factor
   evaluation is deflated against sleeve and (later) sentiment searches it
   could not have drawn from. That is over-deflation, i.e. false negatives at
   the gate — a real cost, not free safety margin. Once the registry grows
   past a handful of entries, scope them (`applies_to`) rather than letting
   the total drift upward forever.

## The pre-registered holdout

`research/holdout_registry.json` plus `research/evaluation/holdout.py`.

The nested walk-forward in `folds.py` is cross-validation: every date is
eventually scored, and nothing stops a researcher from re-running it until it
looks good. A holdout is the other instrument — write the boundary down before
you look, spend it once.

Three properties, each enforced in code:

1. **Registered by date, not index.** Bars arrive daily. An index-registered
   boundary would slide through the data as the panel grows, which is the one
   thing a pre-registration must never do. `resolve()` maps the registered date
   onto whatever date index it is handed.
2. **The same purge as the walk-forward.** `gap = horizon + embargo`, asserted
   against `nested_walk_forward` on a shared fixture, so the two cannot drift.
   Rows in `split.purge` belong to neither training nor holdout.
3. **Single use, recorded on disk.** `evaluate()` appends to the file's
   `evaluations` list and rewrites it, so a second call raises
   `HoldoutAlreadyEvaluated` whether or not the process restarted.
   Re-registering a burned split is refused for the same reason — that is
   moving the goalposts after the fact.

**Registering a split:**

```python
from research.evaluation.holdout import HoldoutProtocol

protocol = HoldoutProtocol.load()          # research/holdout_registry.json
protocol.register(
    split_id="sentiment_2026_11",
    holdout_start="2026-09-01",
    horizon=21,
    embargo=21,
    note="Sentiment sleeve gate eval; boundary set before any scoring run.",
)
```

**Spending it:**

```python
split = protocol.evaluate("sentiment_2026_11", dates, label="gate P submission")
train_rows = frame.iloc[slice(*split.train)]
holdout_rows = frame.iloc[slice(*split.holdout)]
```

Commit the resulting registry change. A holdout whose use is not in git is a
holdout on the honour system — and note that `registered_at` / `evaluated_at`
are caller-supplied and therefore backdatable. The evidence of record is the
**git commit date of `research/holdout_registry.json`**, not the field. An
anti-p-hacking device whose timestamp is an input proves nothing on its own.

The single-use check re-reads the file before recording a burn, so a burn
written by another process is honoured rather than lost, and the write is
atomic (temp file + rename) so a crash cannot destroy the registry. Neither is
a lock: two processes evaluating the *same* split within the same instant is
not defended against.

### Currently registered

- `incumbent_sleeves_2026` — holdout starts **2026-06-01**, horizon 21,
  embargo 21. The six-sleeve set was fixed on 2026-05-26 when
  `mean_reversion` and `short_term_mr` were dropped (see
  `../strategies/mean-reversion-failure-analysis.md`), so no bar from June
  onward was part of the search that produced it. Reserved for the formal
  incumbent evaluation (KAN-40). The window is short today and grows with
  every trading day — report its length alongside the result, because a
  holdout too small to be significant is evidence of nothing.

  Two limits, both of which belong in any writeup that cites it:

  - **It was registered on 2026-08-16, after the window opened.** That makes
    it out-of-*search*-sample, not out-of-sight-sample: the paper book ran
    over June–August and was watched daily. It is the cleanest split
    available, not a clean one.
  - **It is valid for the six sleeves only.** The native factor catalog was
    specified 2026-08-02, *inside* the window, so spending this split on a
    factor evaluation would be contaminated. Register a separate split with a
    later boundary for that. Nothing in the code enforces this — the scope
    lives in the registration's note, and `evaluate()` will happily hand you
    the split for any purpose.

## Parameter stability

`research/evaluation/stability.py` plus `scripts/run_stability_sweep.py`.

Deflation and the holdout both judge a single parameterization. Neither can see
that the parameterization is the highest bump in a noisy surface. If the
momentum sleeve earns a Sharpe of 2.0 at a 126-day lookback and 0.1 at 120 and
132, the 126-day result is a fitting artifact — and testing that one point out
of sample cannot reveal it, because that point is what was fitted.

The check is split in two, because `research/` may not import `backtest` or
`scripts` (`tests/research/test_architecture.py`):

- `scripts/run_stability_sweep.py` replays one real sleeve once per grid point,
  changing only the swept parameter, and writes a
  `{parameter value → metric}` mapping file.
- `parameter_stability()` reads that mapping and returns a `StabilityReport`.
  It runs no backtest and opens no file — the verdict is provable arithmetic.

The plateau criterion, pinned in code so it cannot be renegotiated per
candidate:

1. The neighborhood mean must be no more than `plateau_tolerance` (default
   30%) below the center metric, as a fraction of the center's magnitude. The
   boundary is inclusive.
2. No neighbor may lose money while the center makes it — a parameter value one
   step away that goes negative fails regardless of the mean.

```
python scripts/run_stability_sweep.py \
    --sleeve momentum --parameter lookback_days \
    --grid 100,113,126,139,152 --center 126 \
    --bars-from-json data/cache/bars.json \
    --universe-snapshots data/universe/sp500_snapshots.json \
    --out output/stability/momentum-lookback_days.json
```

The grid must contain the shipped value and at least two neighbors; the driver
refuses before spending a single backtest otherwise. Wall-clock cost is one
full sleeve replay per grid point, so fetch the bars once
(`run_backtest.py --bars-from-json` uses the same cache format) rather than
hitting IB per run.

Pass `--universe-snapshots`. Without it the sleeve ranks a present-day ticker
list and every point on the surface is survivorship-inflated — the driver
warns, and stamps `point_in_time_universe: false` on the artifact, but it will
still run. A plateau measured on survivors is not evidence.

Two things this deliberately does **not** do:

- **It does not re-optimize.** Picking the parameter value with the best
  stability report would be the same overfitting one level up.
- **It does not claim profitability.** A sleeve that loses money at every point
  on the grid is perfectly stable, and `is_plateau` will say so. Stability is a
  necessary condition for the gate, never a sufficient one.

Sleeves whose signal functions need more than bars (`thematic_momentum`,
`quality_value`, `earnings_drift`, `tail_risk_hedge` need the regime series,
the fundamentals cache or the earnings cache) are not wired into the driver
yet. That is deliberate: a sweep run against a missing cache would silently
measure a sleeve that never trades.

## Operator checklist for a promotion

1. Confirm the registry entry for this candidate's search exists and is
   committed.
2. Run the evaluation; check `deflated_sharpe`, `passes_dsr`, `passes_fdr` and
   the run card's `config.n_trials`.
3. Register the holdout split — before, not after.
4. Spend the holdout once; commit the burn.
5. Sweep each parameter the candidate ships and confirm every verdict is
   `PLATEAU` — see "Parameter stability" below.
6. Record all of it against the gate in the promotion pipeline
   (`../designs/project-direction.md` § Strategy Promotion Pipeline).
