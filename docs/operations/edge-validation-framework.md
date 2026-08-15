# Edge Validation Framework

The direction doc's D10 finding is that a matching backtest proves
implementation fidelity, never edge. This document is the binding: what gate S
and gate P require beyond fidelity, where each number comes from, and how to
keep the inputs honest.

Scope note: most of this framework already existed before KAN-38. Deflated
Sharpe, probabilistic Sharpe, Benjamini–Hochberg FDR, and purged/embargoed
nested walk-forward live in `research/evaluation/` and are tested. KAN-38 added
the two things that were missing — an explicit trial count and a pre-registered
holdout — and wrote down the binding below. Parameter stability is KAN-39.

## What gate S and gate P require

| Check | Where it comes from | Passing bar |
|---|---|---|
| Deflated Sharpe against the **declared** trial count | `research/evaluation/multiple_testing.py` `control(..., n_trials=)`, count from `research/trial_registry.json` | `passes_dsr` at the 0.95 default threshold |
| Multiple-testing correction across the candidates in the run | `benjamini_hochberg` at `fdr_q` (default 0.10) | `passes_fdr` |
| Performance on the **pre-registered** holdout | `research/evaluation/holdout.py` | Registered before the look; evaluated once |
| Parameter stability | KAN-39 — not yet built | — |

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
holdout on the honour system.

### Currently registered

- `incumbent_sleeves_2026` — holdout starts **2026-06-01**, horizon 21,
  embargo 21. The six-sleeve set was fixed on 2026-05-26 when
  `mean_reversion` and `short_term_mr` were dropped (see
  `../strategies/mean-reversion-failure-analysis.md`), so no bar from June
  onward was part of the search that produced it. Reserved for the formal
  incumbent evaluation (KAN-40). The window is short today and grows with
  every trading day — report its length alongside the result, because a
  holdout too small to be significant is evidence of nothing.

## Operator checklist for a promotion

1. Confirm the registry entry for this candidate's search exists and is
   committed.
2. Run the evaluation; check `deflated_sharpe`, `passes_dsr`, `passes_fdr` and
   the run card's `config.n_trials`.
3. Register the holdout split — before, not after.
4. Spend the holdout once; commit the burn.
5. Parameter stability (KAN-39) once it exists.
6. Record all of it against the gate in the promotion pipeline
   (`../designs/project-direction.md` § Strategy Promotion Pipeline).
