# Native Factor Evaluation (Phase 3)

**Status:** Approved design
**Date:** 2026-08-02
**Scope:** Offline factor evaluation — nested walk-forward validation, multiple-testing control, overlap attribution, and reproducible run cards
**Parent design:** `docs/superpowers/specs/2026-07-14-native-factor-research-design.md` (delivery phase 3)
**Predecessor:** Phases 1–2 (factor foundation + shadow scoring) on `feature/native-factor-shadow`

## 1. Purpose

Phases 1–2 gave `algo-poc` a native, point-in-time factor foundation and a
failure-isolated shadow recorder. They answer *"what did each factor score for
each candidate?"* — but not *"does this factor actually predict forward
returns, once you account for look-ahead and the fact that we tested several
factors?"*

Phase 3 builds the **offline evaluator** that produces that evidence. It is the
statistical gate every later phase depends on: Phase 4 (strategy search +
Research Validation Score) may only build strategies from factors this phase has
shown to carry honest, multiple-testing-adjusted predictive validity, and it
"may not begin by bypassing the shadow data and causality tests created here."

The evaluator is **offline research code**. It changes no trading decision, adds
no live-ops footprint, and imports nothing from the execution, risk, IB, or
Redis surfaces — extending the isolation boundary already enforced for
`research/`.

## 2. Design principles

Inherited from the parent design, made concrete for evaluation:

1. **Measure what the system can actually trade.** The eventual research sleeve
   is long-only. Factor edge is therefore measured as a **top-quantile
   long-only excess return over the equal-weight universe**, never a
   long-short spread that credits an untradeable short leg.
2. **Point-in-time by construction.** Forward returns and factor scores at date
   `t` use only observations available by `t`. Overlapping horizons are handled
   by purge and embargo, not ignored.
3. **Honest significance.** A number that has been tuned or cherry-picked is not
   evidence. Selection happens only inside inner folds; the outer fold is
   measured exactly once; significance is deflated for the number of factors
   tested.
4. **Reproducible by identity.** Same frozen bars + same code revision → a
   byte-identical run card. Every reported number is traceable to a git
   revision, data hash, universe hash, seed, and fold definition.
5. **Evidence, not authority.** The evaluator emits metrics and survival flags.
   It authorizes nothing, generates no candidates, and touches no capital.

## 3. Scope

**In scope (this phase):**

- Evaluate the four registered catalog factors **individually**.
- Panel-primary evidence: full-universe factor scores + forward excess returns
  rebuilt deterministically from frozen backtest bar artifacts.
- Shadow candidate records layered in as the baseline-selection set for overlap
  attribution.
- Nested, purged, embargoed walk-forward evaluation.
- Multiple-testing control (Deflated Sharpe + Benjamini–Hochberg FDR).
- Overlap attribution into `research_only` / `overlap` / `baseline_only`
  cohorts with per-cohort forward returns.
- One immutable JSON run card per evaluation.
- A thin CLI entry point.

**Explicitly out of scope (later phases):**

- Research Validation Score composition, factor **combinations** (≤3 factors),
  weight/threshold search beyond the single quantile cutoff — Phase 4.
- Candidate Conviction Score, candidate generation, portfolio aggregation, and
  any capital blending — Phases 4 and 6.
- Any DB write, Redis publish, or trading-path change.
- Fundamental-factor evaluation (the initial catalog is price-only; the panel
  already carries the fundamental contract for later factor plans).

## 4. System boundary

Add a self-contained subpackage `research/evaluation/`. It may import from
`research/factors/` (contracts, panel, engine) and read frozen artifacts. It
must not import `services.execution`, `services.risk_management`, `ib_insync`,
`ibapi`, `redis`, `shared.redis_client`, `shared.schemas.messages`,
`backtest.runner`, or `scripts.run_paper`. The existing
`tests/research/test_architecture.py` boundary scanner is extended to cover
`research/evaluation/`.

```text
frozen OHLCV bars (--bars-from-json)
        │
        ▼
research.factors.panel.build_factor_panel      (Phase 1-2, reused)
        │
        ▼
research.factors.engine.FactorEngine.compute   (Phase 1-2, reused)
        │  factor scores: date × ticker, per factor version
        ▼
research.evaluation.forward_returns   ── h-day forward EXCESS return vs equal-weight universe
        │
        ▼
research.evaluation.folds             ── nested, purged, embargoed walk-forward splits
        │
        ▼
research.evaluation.portfolio         ── top-quantile long-only holdings → daily excess series + IC series
        │
        ▼
research.evaluation.metrics           ── Sharpe, Deflated Sharpe, max DD, turnover, IC mean/t-stat
        │
        ▼
research.evaluation.multiple_testing  ── Deflated Sharpe across N factors + BH-FDR on IC t-stats
        │
        ▼
research.evaluation.overlap           ── cohorts from shadow records + per-cohort forward returns
        │
        ▼
research.evaluation.runcard           ── immutable JSON run card (provenance, hashes, seed, evidence)
        ▲
scripts/run_factor_evaluation.py      ── CLI: --bars-from-json, --shadow-from-json|--shadow-from-db, --output-dir
```

## 5. Components

### 5.1 `forward_returns.py`

`forward_excess_returns(panel, horizon) -> pd.DataFrame` (date × ticker).

- For each ticker and date `t`, the `h`-trading-day forward return
  `close[t+h]/close[t] - 1`.
- Subtract the **equal-weight universe** forward return over the same window
  (mean across tickers with a valid forward return at `t`).
- Rows near the end of the panel with no `t+h` bar are `NaN` and excluded from
  scoring.
- Causal: mutating any bar after `t` cannot change the forward return anchored
  at or before `t - h` (future-mutation sentinel).

### 5.2 `folds.py`

`nested_walk_forward(dates, n_outer, n_inner, horizon, embargo) -> list[OuterFold]`
where each `OuterFold` carries an ordered list of `InnerFold`s and a single
outer-test index range.

- Time-ordered, non-shuffled partitioning.
- **Purge:** training samples whose `h`-day forward window overlaps the test
  span are removed.
- **Embargo:** an additional `embargo` (default `= horizon`) trading days
  between train and test are dropped on both sides of the boundary.
- Inner folds live entirely inside the outer-train span and are used only for
  selecting the quantile cutoff.

### 5.3 `portfolio.py`

`quantile_long_only(scores, forward_returns, quantile, rebalance) -> PortfolioSeries`.

- On each rebalance date, rank tickers by factor score; hold the top `quantile`
  fraction, equal-weighted, long-only.
- Series value = the held book's forward excess return.
- Tracks one-way turnover between rebalances for cost realism (reuse the
  repo's existing commission/slippage assumptions from the backtest config).
- Also emits the per-date **Information Coefficient**: Spearman rank
  correlation between factor score and forward excess return across the
  universe (secondary diagnostic).

### 5.4 `metrics.py`

Pure functions over a return series:

- `sharpe(series)` — annualized; reuse `backtest.metrics` primitives where the
  signatures fit.
- `deflated_sharpe(series, n_trials)` — Bailey–López de Prado Deflated Sharpe
  Ratio, adjusting the observed Sharpe for the number of trials and the
  series' skew and kurtosis; normal CDF via `math.erf` (no scipy).
- `max_drawdown(series)`, `annualized_turnover(turnover_series)`.
- `ic_summary(ic_series)` — mean IC, IC t-stat, hit rate.

### 5.5 `multiple_testing.py`

`control(per_factor_results) -> dict[factor_id, MultipleTestingVerdict]`.

- **Deflated Sharpe** computed with `n_trials = number of factors evaluated`.
- **Benjamini–Hochberg FDR** applied across the factors' IC t-stat p-values at a
  configurable `q` (default `0.10`).
- A factor `survives_multiple_testing` only if it clears **both** gates.

### 5.6 `overlap.py`

`attribute(factor_selections, shadow_records) -> OverlapReport`.

- `factor_selections`: the factor's top-quantile picks per date.
- `shadow_records`: established-sleeve buy candidates (the baseline selection
  set), loaded from a frozen shadow JSON export or, optionally, the
  `research_candidates` table (read-only).
- Buckets each `(date, ticker)` into `research_only`, `overlap`, or
  `baseline_only`, and reports cohort counts plus the factor's forward excess
  return within each cohort.
- No capital or aggregation logic — this is diagnostic evidence for later
  blending decisions, not a blending mechanism.

### 5.7 `runcard.py`

`build_run_card(...) -> dict` and `write_run_card(card, output_dir) -> Path`.

The run card is immutable JSON containing, per the parent design's "reproducible
promotion" requirement:

- git revision, code revision hash of the evaluation + factor modules;
- data cutoff, input bar artifact checksum, universe hash;
- factor ids + versions evaluated;
- random seed, horizon, fold definitions (outer/inner counts, purge, embargo),
  quantile grid, FDR `q`;
- per-factor: outer-test Sharpe, Deflated Sharpe + p-value, max DD, turnover,
  IC mean/t-stat, `survives_multiple_testing`;
- per-factor overlap cohort counts and per-cohort forward returns.

Written under `--output-dir`; never overwrites anything under `output/`.

### 5.8 `scripts/run_factor_evaluation.py`

Thin CLI wiring the pipeline:

- `--bars-from-json PATH` (required) — frozen bar artifact.
- `--shadow-from-json PATH` or `--shadow-from-db` (optional) — baseline
  selection set for overlap; overlap section is omitted with a recorded note if
  neither is supplied.
- `--horizon`, `--outer-folds`, `--inner-folds`, `--quantiles`, `--fdr-q`,
  `--seed`, `--output-dir` — all defaulted.
- Exit 0 on success; writes exactly one run card.

## 6. Data flow

1. Load frozen bars; build the point-in-time panel; compute factor scores with
   the existing engine (immutable snapshot identity + provenance).
2. Compute forward excess returns for the configured horizon.
3. Build nested purged+embargoed walk-forward folds over the panel dates.
4. For each factor: inner-fold select the quantile cutoff, score the outer-test
   span once, accumulate the outer-test excess-return and IC series.
5. Compute per-factor metrics; apply multiple-testing control across factors.
6. Attribute overlap cohorts against the baseline selection set.
7. Assemble and write the immutable run card.

## 7. Failure behavior

Offline and fail-safe for research; zero effect on production:

- Insufficient history for the horizon/folds: the affected factor is reported
  with a `skipped` status and reason; other factors still evaluate.
- Missing shadow baseline: overlap section omitted with an explicit note; the
  predictive evaluation still runs.
- A single factor raising during computation is caught, recorded as an error
  entry in the run card, and does not abort the run.
- No exception path touches the trading system; there is nothing to fail closed
  against because nothing is published or persisted to operational tables.

## 8. Testing requirements

Per-unit, sentinel-first, no new third-party runtime dependency:

- **Forward returns:** future-mutation sentinel; correct `NaN` tail; excess vs
  equal-weight universe on a small known example.
- **Folds:** purge removes overlapping-horizon train samples; embargo gap
  present on both sides; inner folds strictly inside outer-train; no test index
  ever appears in train.
- **Portfolio:** top-quantile membership and equal weighting on a known matrix;
  turnover accounting; IC sign on a monotone example.
- **Metrics:** Deflated Sharpe against a hand-computed example; max DD and
  turnover on known series.
- **Multiple testing:** BH-FDR ordering/threshold on known p-values; a factor
  passing one gate but not the other is not marked as surviving.
- **Overlap:** cohort partition is exhaustive and disjoint; per-cohort returns
  match a small fixture.
- **Determinism:** identical bars + seed → identical run card (excluding an
  explicit wall-clock stamp field, if any, which is passed in, not read).
- **Boundary:** `research/evaluation/` passes the extended architecture scanner.
- **Smoke:** CLI on the canonical frozen artifact
  (`output/backtest_multi_20260710_005841.json`) exits 0 and writes a run card
  containing all four factors; does not write under `output/`.

## 9. Phase acceptance criteria

This phase is complete only when:

- Four catalog factors are each evaluated with nested, purged, embargoed
  walk-forward over frozen bars.
- Selection occurs only in inner folds; each outer-test span is scored once.
- Factor edge is measured as top-quantile long-only excess return over the
  equal-weight universe, with IC reported alongside.
- Deflated Sharpe and BH-FDR both gate a factor's `survives_multiple_testing`
  flag.
- Overlap cohorts and per-cohort returns are reported when a baseline set is
  supplied.
- Every run writes one immutable, provenance-complete run card; identical
  inputs reproduce it byte-for-byte.
- `research/evaluation/` imports no execution, risk, IB, or Redis surface, and
  the boundary test enforces it.
- No trading-path file changes; the full repository test suite and package
  build pass.
