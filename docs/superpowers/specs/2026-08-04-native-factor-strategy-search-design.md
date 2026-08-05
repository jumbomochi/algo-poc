# Native Factor Strategy Search and Research Validation Score (Phase 4)

**Status:** Approved design
**Date:** 2026-08-04
**Scope:** Declarative strategy specifications, factor combination search, sleeve-specific
objectives, and the offline Research Validation Score
**Parent design:** `docs/superpowers/specs/2026-07-14-native-factor-research-design.md` (delivery phase 4)
**Predecessors:** Phases 1–2 (factor foundation + shadow scoring) and Phase 3 (offline
factor evaluation), both merged into `main`.

## 1. Purpose

Phase 3 answered *"does an individual factor carry honest, multiple-testing-adjusted
predictive validity?"* Phase 4 answers the next question: *"can we combine validated
factors into a versioned, sleeve-targeted strategy, and how much do we trust it?"*

Phase 4 builds the `research/strategy/` subpackage that turns Phase 3's factor evidence
into **fitted, versioned research strategies** each carrying a **Research Validation Score**.
It sits between the Phase 3 evaluator and the not-yet-built Phase 5 paper sleeve.

Phase 4 **authorizes nothing and touches no capital.** It emits fitted strategy
specifications and validation scores. Live eligibility remains a Phase 5 gate by design:
the Research Validation Score has a component — paper/model concordance — that structurally
cannot be measured until three months of paper shadow history exist. Phase 4 therefore
produces an *offline* score and always reports `live_eligible=false`.

The existing trading surfaces remain authoritative and unchanged. `risk_management` remains
the only order approver; `execution` remains the only IB order writer. Phase 4 adds no
live-ops footprint.

## 2. Design principles

Inherited from the parent design, made concrete for strategy search:

1. **Research is evidence, not authority.** Phase 4 scores strategies. It does not promote,
   allocate capital, or generate live candidates.
2. **Honest significance survives combination.** Continuous weight optimization is confined
   to inner folds. The outer fold measures each selected specification exactly once.
   Deflated-Sharpe `n_trials` counts *distinct factor-set specifications* tested, never the
   number of weight vectors explored.
3. **No silent inflation.** The Research Validation Score never renormalizes away a component
   it could not measure. The unmeasurable paper/model-concordance points are reported as
   `unavailable` and excluded from the subtotal, not redistributed.
4. **Reproducible by identity.** Same frozen bars + same code revision + same seed → a
   byte-identical run card, including fitted weights. The optimizer is deterministic and
   seeded; no wall-clock or hash-seed-dependent behavior enters a reported number.
5. **No synthetic data on blocked paths.** Sleeve objectives whose inputs are not yet on the
   evaluation panel are registered as explicitly *blocked*, never wired to invented data.
6. **Fail closed for research, fail isolated for production.** A blocked or failing sleeve is
   recorded with a status and skipped; it never aborts the run and never touches production.

## 3. Scope

**In scope (this phase):**

- Declarative, versioned strategy specifications combining ≤3 registered factors.
- A deterministic, seeded, numpy-only continuous weight optimizer plus a discrete
  entry-threshold grid, both confined to inner folds.
- Nested, purged, embargoed walk-forward search over factor-set specifications (reusing the
  Phase 3 fold machinery), scoring each outer-test span exactly once.
- Multiple-testing control across specifications (Deflated Sharpe with `n_trials = #specs`
  plus Benjamini–Hochberg FDR), reusing Phase 3's `research/evaluation/multiple_testing.py`.
- A pluggable `SleeveObjective` + `Guardrail` interface keyed by sleeve.
- Real, price-derived objectives + guardrails for **`momentum`** and **`thematic_momentum`**.
- Explicitly **blocked** objective registrations for `sector_rotation`, `quality_value`,
  `earnings_drift`, and `tail_risk_hedge`, each recording the data it needs.
- The offline **Research Validation Score** (six components, one reported `unavailable`),
  with hard vetoes.
- One immutable JSON strategy-search run card per run.
- A thin CLI entry point.

**Explicitly out of scope (later phases):**

- Paper/model concordance measurement, the three-month forward gate, candidate generation,
  and any capital blending — Phases 5–6.
- Plumbing point-in-time sector labels, earnings event dates, and regime/crisis series onto
  the evaluation panel, and the four sleeve objectives that depend on them — follow-on
  increments after this phase, tracked as their own tasks. Phase 4 defines the interfaces
  those objectives will implement.
- Any DB write, Redis publish, or trading-path change.
- Candidate Conviction Score, portfolio aggregation, and promotion/demotion lifecycle
  automation — Phases 4 (conviction, out of this slice), 6, and 7.

## 4. System boundary

Add a self-contained subpackage `research/strategy/`. It may import from `research/factors/`
(contracts, panel, engine, registry, catalog) and `research/evaluation/` (folds,
forward_returns, portfolio, metrics, multiple_testing, overlap) and read frozen artifacts.
It must not import `services.execution`, `services.risk_management`, `ib_insync`, `ibapi`,
`redis`, `shared.redis_client`, `shared.schemas.messages`, `backtest.runner`, or
`scripts.run_paper`. The existing `tests/research/test_architecture.py` boundary scanner is
extended to cover `research/strategy/`, including the same prohibitions on `importlib`,
`runpy`, `builtins.__import__`, and direct `__import__` calls.

```text
frozen OHLCV bars (--bars-from-json)
        │
        ▼
research.factors.panel.build_factor_panel            (Phase 1-2, reused)
        │
        ▼
research.factors.engine.FactorEngine.compute         (Phase 1-2, reused)
        │  NORMALIZED (cross-sectional z-scored) factor frames — see §5.1
        ▼
research.strategy.spec        ── enumerate ≤3-factor specifications per sleeve
        │
        ▼
research.strategy.search      ── nested WF; per outer fold, inner-fold optimize
        │                        weights (continuous) × threshold (grid) under the
        │                        sleeve objective; score outer-test span once
        ▼
research.strategy.objectives  ── SleeveObjective (maximized in-fold + fed to RVS)
        │                        + Guardrail hard vetoes; 2 real, 4 blocked
        ▼
research.evaluation.multiple_testing.control         (Phase 3, reused; n_trials = #specs)
        │
        ▼
research.strategy.rvs         ── six-component offline Research Validation Score
        │                        (paper/model concordance = unavailable) + hard vetoes
        ▼
research.strategy.runcard     ── immutable JSON run card (fitted weights, RVS, provenance)
        ▲
scripts/run_strategy_search.py ── CLI: --bars-from-json, --sleeve, --shadow-from-*, --output-dir
```

Strategy search reuses Phase 3's overlap attribution
(`research.evaluation.overlap`) to compute the diversification RVS component.

## 5. Components

### 5.1 Normalized-frame ranking (prerequisite fix)

The Phase 3 evaluator ranks on **raw** `factor.compute()` scores. That is correct only while
the catalog is price-only with `normalization_policy="none"`, because cross-sectional rank is
invariant to monotone normalization. A weighted blend of factors on different scales is
**not** rank-invariant, so weights would be meaningless. Phase 4 therefore ranks and combines
on the engine's **cross-sectional z-scored** frames.

This is an isolated change with its own test: the engine already produces normalized frames
per factor; the combination step is a weighted sum of those z-scored frames, then a
cross-sectional rank for top-quantile selection. The z-score masks to universe members, so
the change also removes the raw-vs-normalized assumption note currently in
`research/evaluation/evaluator.py`. A causality/invariance test asserts that combining two
z-scored factors and ranking is invariant to a positive affine rescale of either input factor
but *not* invariant to relative reweighting (proving weights actually bite).

### 5.2 Strategy specification (`research/strategy/spec.py`)

Two dataclasses, both frozen:

- `StrategySearchSpace`: `target_sleeve`, candidate factor pool (registered factor ids), the
  ≤3-factor family-compatibility rule for the sleeve, and the discrete entry-threshold grid.
  Enumerating a search space yields the set of candidate factor-set specifications
  (each an unordered ≤3-subset of the compatible pool).
- `FittedStrategy`: the concrete result — `strategy_id`, `version`, `target_sleeve`,
  `factor_set` (tuple of factor ids + versions), `objective_id`, per-outer-fold fitted
  `weights`, chosen `threshold`, and a deterministic `strategy_hash`.

Family compatibility: a specification is valid only if its factor families are compatible with
the target sleeve (e.g. `momentum` accepts `momentum`/`risk`/`liquidity` families;
combinations that duplicate a single family with correlated formulas are still allowed at
search time but discounted downstream by the diversification component and by the fact that
correlated evidence does not add independent conviction — parent design §2.5).

The strategy hash covers factor ids + versions, objective id, fitted weights, threshold, fold
definition, seed, and the input artifact checksum. Per parent §5.2, weights and thresholds are
part of the validated hash: changing them produces a new version and restarts validation.

### 5.3 Sleeve objectives and guardrails (`research/strategy/objectives.py`)

Two protocols keyed by sleeve in a registry:

- `SleeveObjective.score(portfolio_series, ic_series, context) -> float` — the scalar the
  inner-fold optimizer maximizes, and the value fed into the RVS "risk-adjusted utility"
  component. Higher is better; costs (turnover) are already netted where the sleeve objective
  demands it.
- `Guardrail.check(portfolio_series, holdings, context) -> GuardrailResult(passed, reason)` —
  a hard veto. Any failed guardrail on the outer-test evidence flags the strategy and blocks
  live eligibility regardless of score.

**Implemented on real price data now:**

| Sleeve | Objective | Guardrails |
|---|---|---|
| `momentum` | Walk-forward Sharpe with a return-consistency penalty (per-fold Sharpe dispersion) | max drawdown ceiling; one-way annual turnover / whipsaw ceiling |
| `thematic_momentum` | Risk-adjusted excess return (Sharpe of top-quantile long-only excess series) | top-name concentration ceiling; crash-loss ceiling (worst `h`-day book return) |

All inputs above are derivable from the frozen OHLCV panel and the Phase 3
portfolio/metrics primitives.

**Registered as blocked now** (return `status="blocked: needs <data>"`, no synthetic data):

| Sleeve | Blocked objective needs |
|---|---|
| `sector_rotation` | point-in-time sector map (information-ratio-vs-baseline, sector concentration) |
| `quality_value` | fundamentals + sector on the panel (cross-sectional spread, value-trap / sector-bias guards) |
| `earnings_drift` | earnings event dates on the panel (post-event drift, expectancy, sample-size guard) |
| `tail_risk_hedge` | regime/crisis series on the panel (crisis protection, ordinary-period cost) |

The search records each blocked sleeve's status and skips it. Blocked objectives are real
interface implementations that raise a typed `ObjectiveDataUnavailable` when invoked, so their
contracts are defined and testable, but they are never run against invented inputs.

### 5.4 Continuous weight optimizer (`research/strategy/search.py`, optimizer portion)

A deterministic, seeded, numpy-only optimizer over the weight simplex for a fixed factor set:

- Weights are non-negative and sum to 1 (long-only blend of already-directional z-scored
  factors; each factor's `direction` is applied before blending so higher combined score is
  always "more attractive").
- Optimization method: seeded multi-start coordinate descent (a small fixed set of simplex
  vertices + the equal-weight point as starts, then coordinate refinement) maximizing the
  **inner-fold** sleeve objective. No scipy; the normal CDF and any statistics reuse Phase 3's
  `math.erf`-based primitives.
- The entry threshold is chosen from the discrete grid jointly with the weights on the inner
  folds.
- Determinism: fixed start set + fixed iteration order + fixed tolerance → byte-identical
  fitted weights for identical inputs and seed. No `Math.random`/`Date.now` analogues; no
  dependence on Python hash seed.

The optimizer receives **only inner-fold data**. It never sees the outer-test span.

### 5.5 Search orchestration (`research/strategy/search.py`, orchestration portion)

For each evaluable sleeve:

1. Enumerate candidate factor-set specifications from the sleeve's search space.
2. Build nested purged+embargoed walk-forward folds over the panel dates (reuse
   `research.evaluation.folds.nested_walk_forward`).
3. For each specification and each outer fold: run the inner-fold optimizer to fit
   `(weights, threshold)`, then build the top-quantile long-only book on the **outer-test**
   span exactly once, accumulating the OOS excess-return, IC, and turnover series (reuse
   `research.evaluation.portfolio.quantile_long_only`).
4. Compute the sleeve objective and guardrail results on the accumulated OOS evidence.
5. After all specifications for the sleeve are scored, apply multiple-testing control across
   them with `n_trials = number of specifications evaluated`
   (`research.evaluation.multiple_testing.control`).
6. Attribute overlap cohorts against the baseline selection set (reuse
   `research.evaluation.overlap.attribute`) for the diversification component.

Continuous weight fitting within a specification does **not** inflate `n_trials`; the untouched
outer fold absorbs weight-fitting variance, and the stability component (§5.6) penalizes
specifications whose per-fold behavior is inconsistent — the residual overfitting signal.

### 5.6 Research Validation Score (`research/strategy/rvs.py`)

A composite in [0, 100] with six components. At Phase 4, five are measurable offline and one
is structurally unavailable:

| Component | Weight | Phase 4 source |
|---|---:|---|
| Walk-forward predictive validity | 25 | outer-fold Sharpe, Deflated Sharpe, IC t-stat |
| Stability across folds and regimes | 20 | inverse of per-fold objective dispersion (regime split is partial until the regime series is plumbed; recorded as such) |
| Paper/model concordance | 20 | **`unavailable`** — requires Phase 5 paper history; excluded from the subtotal |
| Risk-adjusted utility after costs | 15 | sleeve objective value net of turnover cost |
| Diversification from existing sleeves | 10 | Phase 3 overlap cohorts (`research_only` share; correlated-evidence discount) |
| Data quality, liquidity, feasibility | 10 | universe coverage, liquidity factor level, turnover feasibility |

**Reporting rule (no silent inflation).** The run card reports each component's raw
contribution and an `offline_subtotal` **out of 80** (the sum of measurable weights). It does
**not** renormalize to 100. `live_eligible` is always `false` with reason
`awaiting_paper_concordance`; live eligibility is granted only in a later phase once the
concordance component and the three-month forward gate are satisfied.

**Hard vetoes** (parent §5.1) cap the strategy as ineligible and are recorded regardless of
subtotal: look-ahead evidence, failed point-in-time reconstruction, inadequate coverage,
unresolved data revisions, failure of the Deflated-Sharpe / BH-FDR multiple-testing gate,
non-reproducible output, and infeasible liquidity. A failed guardrail (§5.3) is also a veto.

### 5.7 Run card (`research/strategy/runcard.py`)

`build_strategy_run_card(...) -> dict` and `write_strategy_run_card(card, output_dir) -> Path`.
Immutable JSON, written under `--output-dir`, refusing any path under `output/` (mirroring
`research/evaluation/runcard.py`). Per strategy it records:

- target sleeve, factor set (ids + versions), objective id;
- per-outer-fold fitted weights and chosen threshold;
- outer-test objective value, Sharpe, Deflated Sharpe + p-value, max drawdown, annual
  turnover, IC mean/t-stat, `survives_multiple_testing`;
- guardrail results;
- RVS per-component contributions, `offline_subtotal` (/80), `live_eligible=false` + reason,
  and any hard vetoes;
- overlap cohort counts and per-cohort returns (diversification evidence);
- provenance: git revision, code revision hash, data cutoff, input artifact checksum, universe
  hash, seed, fold definition, strategy hash;
- blocked sleeves with their `status` and required data.

Same frozen bars + same code revision + same seed reproduce the run card byte-for-byte
(excluding any explicitly passed-in wall-clock stamp, which is never read internally).

### 5.8 CLI (`scripts/run_strategy_search.py`)

Thin wiring:

- `--bars-from-json PATH` (required) — frozen bar artifact.
- `--sleeve NAME` (repeatable; default: all *evaluable* sleeves) — blocked sleeves named
  explicitly are still recorded with `status`, never errored.
- `--shadow-from-json PATH` or `--shadow-from-db` (optional) — baseline selection set for the
  diversification component; omitted → diversification recorded as unavailable with a note.
- `--horizon`, `--outer-folds`, `--inner-folds`, `--thresholds`, `--fdr-q`, `--seed`,
  `--output-dir` — all defaulted, consistent with `scripts/run_factor_evaluation.py`.
- Exit 0 on success; writes exactly one strategy-search run card.

## 6. Data flow

1. Load frozen bars; build the point-in-time panel; compute the engine's **normalized**
   factor frames (immutable snapshot identity + provenance).
2. For each evaluable sleeve, enumerate candidate ≤3-factor specifications.
3. Build nested purged+embargoed walk-forward folds.
4. Per specification, per outer fold: inner-fold optimize `(weights, threshold)` under the
   sleeve objective; score the outer-test span once; accumulate OOS series.
5. Compute sleeve objective + guardrails; apply multiple-testing control across the sleeve's
   specifications with `n_trials = #specs`.
6. Attribute overlap cohorts against the baseline selection set.
7. Compose the offline Research Validation Score; apply hard vetoes.
8. Assemble and write the immutable strategy-search run card.

Blocked sleeves are recorded with their status at step 2 and skipped.

## 7. Failure behavior

Offline and fail-safe for research; zero effect on production:

- Insufficient history for the horizon/folds: the affected sleeve/specification is reported
  with a `skipped` status and reason; others still evaluate.
- Blocked sleeve (missing panel data): recorded with `status="blocked: needs <data>"`;
  never run against synthetic inputs; never aborts the run.
- Missing shadow baseline: diversification component recorded as unavailable with a note;
  predictive evaluation still runs.
- A single specification raising during optimization or scoring is caught, recorded as an
  error entry in the run card, and does not abort the run.
- No exception path touches the trading system; nothing is published or persisted to
  operational tables.

## 8. Testing requirements

Per-unit, sentinel-first, no new third-party runtime dependency:

- **Normalized-frame ranking:** combining two z-scored factors and ranking is invariant to a
  positive affine rescale of an input factor but changes under relative reweighting.
- **Strategy spec:** enumeration yields exactly the ≤3-subsets of the compatible pool; family
  compatibility rejects incompatible families; `strategy_hash` is deterministic and changes
  when weights, threshold, factor set, objective, seed, or fold definition change.
- **Objectives/guardrails:** `momentum` and `thematic_momentum` objective values on known
  series match hand computation; each guardrail vetoes on a fixture that breaches it and
  passes on one that does not; blocked objectives raise `ObjectiveDataUnavailable` and never
  fabricate data.
- **Optimizer determinism:** identical inputs + seed → byte-identical fitted weights and
  threshold; no dependence on Python hash seed.
- **Outer-fold-never-seen sentinel:** mutating outer-test bars cannot change the fitted
  weights or chosen threshold for any specification.
- **Multiple testing:** `n_trials` equals the number of specifications evaluated, not the
  number of weight vectors explored; a specification passing one gate but not the other is not
  marked as surviving.
- **RVS composition:** subtotal is out of 80 with paper/model concordance reported
  `unavailable`; no renormalization to 100; `live_eligible` is always `false` with reason
  `awaiting_paper_concordance`; each hard veto flags ineligibility on its fixture.
- **Blocked-sleeve status:** naming a blocked sleeve records its status and does not error the
  run.
- **Determinism:** identical bars + seed → identical run card (excluding an explicit passed-in
  wall-clock field, if any).
- **Boundary:** `research/strategy/` passes the extended architecture scanner.
- **Smoke:** CLI on the canonical frozen artifact
  (`output/backtest_multi_20260710_005841.json`) exits 0 and writes a run card containing the
  two evaluable sleeves plus recorded blocked statuses for the other four; does not write under
  `output/`.

## 9. Phase acceptance criteria

This phase is complete only when:

- Strategy specifications combine ≤3 registered factors under sleeve family-compatibility
  rules, ranked and combined on normalized factor frames.
- Continuous weight optimization is confined to inner folds; each outer-test span is scored
  exactly once; `n_trials` for Deflated Sharpe equals the number of specifications evaluated.
- `momentum` and `thematic_momentum` objectives + guardrails run on real frozen-bar price data;
  the other four sleeves are registered as blocked with their required data recorded, and no
  synthetic data is used.
- The offline Research Validation Score reports five measurable components plus paper/model
  concordance as `unavailable`, an `offline_subtotal` out of 80 with no renormalization, and
  `live_eligible=false`; hard vetoes and failed guardrails flag ineligibility.
- Every run writes one immutable, provenance-complete strategy-search run card; identical
  inputs reproduce it byte-for-byte.
- `research/strategy/` imports no execution, risk, IB, or Redis surface, and the boundary test
  enforces it.
- No trading-path file changes; the full repository test suite and package build pass.

## 10. Suggested task decomposition

1. Normalized-frame ranking switch + invariance test (removes the evaluator assumption note).
2. Strategy spec + search-space enumeration + family compatibility + strategy hashing.
3. Objective + guardrail interfaces; `momentum` and `thematic_momentum` implementations; four
   blocked registrations with typed `ObjectiveDataUnavailable`.
4. Deterministic, seeded, numpy-only weight optimizer + threshold-grid selection.
5. Search orchestration (nested WF over specifications) + multiple-testing with `n_trials=#specs`.
6. Research Validation Score composition + hard vetoes + `unavailable`-concordance handling.
7. Run card + CLI + extended boundary scanner + determinism and smoke tests.

Each task preserves the isolation boundary and is independently testable.
