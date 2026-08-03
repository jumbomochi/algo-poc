# Native Factor Research and Automated Research Sleeve

**Status:** Approved design
**Date:** 2026-07-14
**Scope:** Research, validation, candidate generation, and bounded promotion into the existing trading pipeline

## 1. Purpose

Build a native factor-research subsystem inspired by Vibe-Trading's Alpha Zoo architecture without copying Vibe-Trading or introducing it as a runtime dependency. The subsystem will evaluate registered factors across all six active sleeves, discover new liquid US-equity candidates, and automatically promote sufficiently validated research strategies into a small live research sleeve.

The existing trading surfaces remain authoritative and unchanged in role:

- `risk_management` remains the only service that approves or rejects orders.
- `execution` remains the only service that writes orders to Interactive Brokers.
- Existing reconciliation, kill-switch, account-type, fill, and notification behavior applies to research positions.
- Research cannot change its capital ceiling, bypass risk, or call IB directly.

The initial objective is not to replace established sleeve signals. Factors first operate in shadow mode across all sleeves. Validated research strategies may later generate new stock candidates through a separately funded research sleeve.

## 2. Design principles

1. **Native contracts, selective formulas.** `algo-poc` owns the factor API, temporal rules, validation, persistence, and tests. External repositories may inform formulas and architecture but are not runtime or correctness dependencies.
2. **Point-in-time by construction.** A factor value at date `t` may use only market observations and fundamental revisions available by `t`.
3. **Research is evidence, not authority.** A composite validation score may authorize a versioned strategy, but hard vetoes and existing risk controls always take precedence.
4. **Incremental contribution.** Research is evaluated against the opportunity cost of taking capital from existing sleeves, not by adding leverage.
5. **No duplicate conviction.** Correlated signals selecting the same stock do not count as independent evidence.
6. **Fail closed for research, fail isolated for production.** Missing or stale research data prevents research orders but never interrupts established sleeves.
7. **Reproducible promotion.** The exact strategy, factor, data, universe, and configuration hashes tested are the ones promoted.

## 3. System boundary

Add a self-contained `research/` package. It must not import execution implementations, instantiate IB clients, publish approved orders, or modify risk decisions.

```text
OHLCV + point-in-time fundamentals
                 |
                 v
       Factor Panel Builder
                 |
                 v
       Factor Registry/Engine
                 |
       versioned factor values
                 v
       Shadow Candidate Recorder
                 |
                 v
     Offline Evaluation and Promotion
                 |
        authorized strategies
                 v
       Research Candidate Generator
                 |
       existing RecommendationMessage
                 v
       risk_management -> execution
```

Research is a parallel recommendation producer alongside the existing signal and ML paths. It reuses `RecommendationMessage` with `portfolio="research"`; no new execution message or broker interface is introduced.

## 4. Components

### 4.1 Factor contracts

`research/factors/contracts.py` defines an explicit `FactorSpec` containing:

- Stable factor ID and semantic version
- Factor family and economic rationale
- Required market and fundamental fields
- Lookback and expected prediction horizon
- Expected direction
- Supported sleeves and universes
- Missing-data policy
- Normalization policy
- Source and license attribution

Factor implementations accept a point-in-time panel and return ticker-by-date scores. Only reviewed, registered Python implementations are executable. Generated arbitrary Python is not supported.

### 4.2 Panel builder

`research/factors/panel.py` aligns:

- Daily OHLCV
- Dated universe membership
- Fundamentals using `effective_at`, `ingested_at`, and `source_revision`
- Regime labels known at the calculation cutoff

Historical panels must include removed and delisted constituents where data is available. Current constituents must not be projected backward. Later filing revisions must not rewrite earlier factor values.

### 4.3 Factor registry and engine

`research/factors/registry.py` provides explicit registration and discovery. `research/factors/engine.py` provides reusable, causally safe transforms such as:

- Cross-sectional rank and z-score
- Winsorization
- Rolling mean, volatility, rank, correlation, and change
- Industry/sector neutralization
- Missing-value and minimum-coverage checks

Every calculation records the factor version, data cutoff, universe snapshot, code revision, and input artifact checksum.

### 4.4 Shadow observation

Shadow scoring observes every raw sleeve entry candidate before risk evaluation, including candidates later rejected by risk. It does not change the ticker, action, quantity, limit price, or risk result.

Shadow records contain:

- Sleeve and candidate timestamp
- Ticker and intended horizon
- Factor values, freshness, and versions
- Existing sleeve evidence
- Subsequent risk decision
- Subsequent trade outcome when applicable

Failures are observational. A missing factor snapshot is recorded, and established strategy behavior continues unchanged.

### 4.5 Research strategy specifications

Automated research operates on declarative, versioned strategy specifications. It may:

- Evaluate registered individual factors
- Combine at most three registered factors
- Tune weights and entry thresholds inside training folds
- Select compatible factor families per sleeve
- Generate new candidates from the permitted universe

It may not generate executable strategy code, alter factor formulas during validation, expand its own universe or asset classes, or reuse test-fold outcomes for selection.

### 4.6 Research candidate generator

Only an authorized strategy version may emit live candidates. Each recommendation includes:

- `portfolio="research"`
- Deterministic, idempotent recommendation ID
- Candidate conviction score
- Authorized strategy and factor versions
- Factor attribution in `top_features`
- Research-sleeve quantity and limit price

The existing risk service may reduce or reject every recommendation. The existing execution service remains the only IB order writer.

## 5. Validation levels

Validation is separated into strategy authorization and stock selection.

### 5.1 Research Validation Score

Each research strategy receives a score from 0 to 100:

| Component | Weight |
|---|---:|
| Walk-forward predictive validity | 25 |
| Stability across folds and regimes | 20 |
| Paper/model concordance | 20 |
| Risk-adjusted utility after costs | 15 |
| Diversification from existing sleeves | 10 |
| Data quality, liquidity, and execution feasibility | 10 |

Automated live eligibility requires:

- Validation score of at least 75
- At least three months of paper shadow operation
- A sufficient candidate sample for the strategy horizon
- Positive marginal contribution after funding the 2% research sleeve
- No unresolved divergence breach
- No hard veto

Hard vetoes include look-ahead evidence, failed point-in-time reconstruction, inadequate coverage, unresolved data revisions, invalid multiple-testing controls, non-reproducible output, and infeasible liquidity.

### 5.2 Candidate Conviction Score

An authorized strategy scores each `(ticker, strategy, timestamp, horizon)` context from 0 to 100. There is no permanent universal score for a stock.

Initial components are:

| Component | Weight |
|---|---:|
| Calibrated expected excess return | 35 |
| Confidence and uncertainty | 20 |
| Regime compatibility | 15 |
| Signal freshness | 10 |
| Liquidity and expected execution cost | 10 |
| Portfolio diversification benefit | 10 |

Initial entry eligibility requires an adjusted score of at least 70. The portfolio aggregator adjusts raw conviction for existing holdings, correlated evidence, ticker exposure, sector concentration, and available research capital.

Weights and thresholds are configuration values included in the validated strategy hash. They may be changed only by producing a new strategy version and restarting validation.

## 6. Overlap and portfolio aggregation

Research evaluation reports three cohorts:

- `research_only`: stocks not selected by established sleeves
- `overlap`: stocks already selected or held
- `baseline_only`: established selections without research support

For overlap stocks, research receives credit only for marginal improvement relative to the established evidence. Signals are grouped by factor family, input fields, lookback, model version, and horizon. Highly correlated evidence is discounted so duplicate momentum formulas cannot manufacture confidence.

Same-direction, independent evidence may increase the desired aggregate exposure within existing caps. Same-direction duplicate evidence adds little or no exposure. Opposite-direction evidence reduces aggregate conviction but cannot unexpectedly force another sleeve to exit outside that sleeve's exit policy.

Different horizons retain separate virtual ownership and lots. The broker sees the net position, while the database retains sleeve-specific quantities, cost basis, P&L, and exit rules.

## 7. Sleeve-specific objectives

Promotion is not based on a single portfolio-wide objective.

| Sleeve | Primary objective | Guardrails |
|---|---|---|
| `momentum` | Walk-forward Sharpe and return consistency | Drawdown, whipsaws, turnover |
| `thematic_momentum` | Risk-adjusted excess return | Concentration, crash loss |
| `sector_rotation` | Information ratio versus baseline | Turnover, sector concentration |
| `earnings_drift` | Post-event excess return and expectancy | Gap risk, sample size |
| `quality_value` | Cross-sectional spread and long-term excess return | Value traps, sector bias |
| `tail_risk_hedge` | Crisis protection and drawdown reduction | Ordinary-period protection cost |

The validation score normalizes each strategy's sleeve-specific objective and guardrails into a common authorization scale without pretending that tail protection and momentum have the same utility function.

## 8. Bounded live research sleeve

The initial live research allocation is 2% of total portfolio capital. It is funded by reducing every established sleeve proportionally, preserving total planned capital at 100%. Research cannot increase this allocation. Any increase requires a separate human-approved configuration change.

Initial hard limits are:

- Long-only US equities
- Point-in-time S&P 500 or Russell 1000 universe
- Market-cap, price, liquidity, and data-quality gates
- No leverage, shorting, options, or inverse exposure
- Maximum 0.4% of total portfolio capital per research position
- Maximum five fully sized concurrent positions
- Existing portfolio ticker, sector, drawdown, and aggregate exposure limits remain authoritative
- Existing limit-order behavior is used for entries
- Separate research sleeve P&L and virtual-lot attribution

## 9. Promotion and demotion lifecycle

```text
proposed
   -> historical_shadow
   -> walk_forward_validated
   -> paper_shadow
   -> live_eligible
   -> active_research
   -> monitoring
       -> active_research (healthy)
       -> suspended (degraded)
       -> retired (persistent failure)
```

Promotion decisions are evaluated weekly. Full walk-forward revalidation runs monthly. Factor values and tradable candidates are calculated daily after finalized bars.

Automatic suspension prevents new entries when:

- Validation score is below 60 on two consecutive weekly evaluations
- Live-versus-model divergence reaches breach status
- Required factor or universe data is stale or incomplete
- Rolling metrics violate the registered strategy guardrails
- The research sleeve falls 10% from its own equity peak

Suspension does not automatically liquidate positions. Open positions follow their registered exit rules unless the existing portfolio kill switch or risk policy requires liquidation.

All promotions, suspensions, retirements, orders, factor versions, scores, and human capital-ceiling changes are audited.

## 10. Daily data flow

1. Data ingestion stores finalized daily bars and point-in-time fundamentals.
2. The factor engine calculates scores using an immutable cutoff.
3. Authorized strategies generate candidates.
4. The portfolio aggregator resolves overlap with established holdings.
5. Ineligible and rejected candidates are recorded for counterfactual analysis.
6. Eligible candidates publish the existing recommendation contract with `portfolio="research"`.
7. Existing risk management enforces research and portfolio limits.
8. Existing execution processes approved orders.
9. Fills retain research strategy and virtual-lot attribution.

Cadence:

- Factor values: daily
- Candidate generation: daily
- Shadow and paper metrics: daily
- Validation score and promotion state: weekly
- Full walk-forward revalidation: monthly
- Dated universe refresh: monthly

Live research uses the configured canonical data source. It must not silently switch to an external fallback when production inputs are missing.

## 11. Persistence and artifacts

PostgreSQL stores operational state:

- Factor definitions and versions
- Research strategy specifications
- Validation summaries
- Promotion state and audit events
- Daily candidate scores and dispositions
- Research sleeve attribution

Parquet stores large historical artifacts:

- Factor panels
- Walk-forward predictions
- Quantile-return series
- Counterfactual portfolio replays
- Fold-level validation results

Every validation run produces a run card recording:

- Git revision
- Strategy and factor hashes
- Data cutoff and artifact checksums
- Dated universe snapshot
- Fundamental revision policy
- Transaction-cost assumptions
- Fold definitions and random seeds
- Multiple-testing adjustment
- Baseline and research portfolio metrics

## 12. Validation methodology

Use nested walk-forward evaluation. Inner folds select factors, weights, and thresholds. An untouched outer fold measures the selected specification exactly once.

Required methods include:

- Purged and embargoed splits for overlapping horizons
- Point-in-time constituents and fundamentals
- Realistic commissions and slippage
- Delisted constituents when available
- False-discovery control across tested factors
- Deflated Sharpe or an equivalent selection-bias adjustment
- Fixed seeds and deterministic artifacts

The promotion comparison is:

```text
98% proportionally reduced established sleeves + 2% research sleeve
versus
100% original six-sleeve baseline
```

This is the only portfolio-level comparison used for claiming incremental value. Research cannot claim success by increasing gross capital or exposure.

## 13. Testing requirements

Every factor requires:

- Formula unit tests with small known examples
- Output shape, range, and missing-data tests
- Cross-sectional invariance tests where applicable
- Future-mutation sentinel: changing data after `t` cannot alter values at or before `t`
- Fundamental-revision sentinel
- Dated universe-membership tests
- Deterministic rerun tests
- Source and license metadata tests

System tests require:

- Research recommendations pass through the real risk contract
- Research exposure cannot exceed 2%
- Per-position and concurrent-position caps hold
- Existing ticker and sector caps include research exposure
- Duplicate recommendations are idempotent
- Overlap preserves virtual ownership and correct net exposure
- Research rejection cannot affect an established sleeve
- Missing research data leaves established sleeves unchanged
- Suspension prevents new entries
- Kill switch and reconciliation treat research positions normally
- Architectural tests prevent `research/` from importing IB execution modules

Forward validation requires at least three months, a strategy-appropriate candidate sample, expected-versus-realized slippage analysis, paper-versus-model divergence monitoring, and an exact strategy-hash match at promotion.

## 14. Failure behavior

Research fails closed for itself and remains isolated from production:

- Missing factor data: no research order
- Stale universe membership: no research order
- Incomplete overlap calculation: no research order
- Unknown strategy version: no research order
- Duplicate recommendation: idempotent rejection
- Research process unavailable: established sleeves continue
- Risk or execution unavailable: existing behavior applies

No research exception may activate the portfolio kill switch unless it has already produced a normal portfolio-level condition that independently satisfies existing kill-switch policy.

## 15. Delivery phases

1. Native factor contracts, panel builder, registry, and causality tests
2. Shadow scoring across all six sleeves and research persistence
3. Nested walk-forward evaluator, run cards, and overlap attribution
4. Declarative strategy search and Research Validation Score
5. Automated paper research sleeve and three-month forward gate
6. Bounded 2% live research sleeve through existing risk and execution services
7. Ongoing decay, divergence, suspension, and retirement automation

Each phase must preserve the isolation boundary and be independently deployable. Live promotion cannot be enabled until all prior validation phases and forward evidence gates pass.

## Implementation status

- Phases 1–2: native factor foundation and six-sleeve shadow scoring implemented.
- Research remains disabled by default and observational only.
- Paper/live research candidate generation is not enabled.
- Phases 3–7, including validation, promotion, capital allocation, live recommendations, and lifecycle automation, remain unimplemented.

Phase 1–2 acceptance is codified by the following automated evidence:

| Boundary or behavior | Evidence |
|---|---|
| Four reviewed, versioned price factors and causal/future-mutation behavior | `tests/research/test_catalog.py`, `tests/research/test_engine.py`, `tests/research/test_panel.py` |
| Rejected raw candidates retained and observer failures isolated from established backtests | `tests/backtest/test_research_shadow.py` |
| The six canonical sleeves (`momentum`, `sector_rotation`, `thematic_momentum`, `quality_value`, `earnings_drift`, `tail_risk_hedge`) use opt-in, separately recorded shadow artifacts over one immutable snapshot; serialization preserves every sleeve's shadow field; disabled/setup-failure paths are no-ops | `tests/backtest/test_multi_portfolio.py`, `tests/backtest/test_save_results.py` |
| Paper shadow persistence is opt-in, idempotent, independently sessioned, and failure-isolated | `tests/research/test_shadow.py`, `tests/scripts/test_run_paper_research_shadow.py` |
| In-memory reset-helper regression clears reset-owned paper state while preserving a seeded research audit row | `tests/scripts/test_run_paper_reset.py` |
| Research defaults off | `tests/shared/test_config.py` |
| Research has no prohibited static import of broker, execution/risk service, Redis publishing, runtime-script, or recommendation-contract surfaces; imports of `importlib`, `runpy`, and `builtins.__import__`, plus direct `__import__` calls, are prohibited | `tests/research/test_architecture.py` |

Repository-wide tests and wheel-content inspection remain release gates for this phase; command evidence and the verified commit are recorded in the Task 8 delivery report.
