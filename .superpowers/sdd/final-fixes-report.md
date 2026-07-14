# Whole-Branch Final Fixes Report

Date: 2026-07-14
Base reviewed: `a266262`
Implementation commit: `e1af868` (`fix: harden factor snapshot contracts`)

## Scope delivered

- Added causal row-wise `cross_sectional_zscore` normalization. It preserves
  missing cells, requires at least two observed values by default, emits zeros
  for observed values on zero-dispersion rows, and rejects invalid coverage.
- Made `FactorEngine` dispatch `none` and `cross_sectional_zscore` policies,
  reject unknown policies, and keep factor `compute()` formulas raw.
- Made `FactorPanel` defensively own copied frames, removed its public frame
  mapping, and made every `field()` lookup return an isolated deep frame copy.
- Added immutable `CalculationProvenance` to every `FactorSnapshotIndex` with
  the panel cutoff, deterministic universe identity, source-code checksum, and
  complete panel-input checksum. Its serialization API returns an immutable
  mapping, and the provenance identity is deterministic.
- Exported provenance on every `ShadowCandidateRecord`, persisted it on every
  `ResearchCandidate`, and added the matching JSON column to the existing
  unapplied migration. Candidate keys now include snapshot identity, preserving
  identical-rerun idempotency while separating changed code/input snapshots.
- Rejected distinct opaque `source_revision` values sharing one availability
  timestamp instead of ordering them lexicographically.
- Completed `FactorSpec` validation for positive prediction horizons and every
  required canonical text, tuple, policy, source, and license field.
- Preserved the Phase 1-2 boundary: default-off and failure-isolated behavior is
  unchanged; no risk, execution, broker, output artifact, database, reset, or
  Phase 3+ behavior was added or invoked.

## TDD evidence

Initial RED command:

```text
python -m pytest tests/research/test_operations.py tests/research/test_contracts.py tests/research/test_panel.py tests/research/test_engine.py -q
```

Expected collection failures:

```text
ImportError: cannot import name 'cross_sectional_zscore'
ImportError: cannot import name 'CalculationProvenance'
2 errors in 0.30s
```

Recorder/model RED command:

```text
python -m pytest tests/research/test_shadow.py tests/shared/test_models.py -q
```

Expected behavioral failures: missing record provenance, unchanged candidate
keys across changed input snapshots, one row instead of two for distinct
snapshots, and missing model provenance column (`4 failed, 24 passed`).

GREEN evidence:

```text
python -m pytest tests/research/ -q
98 passed in 0.91s

python -m pytest tests/backtest/ tests/scripts/test_run_paper_gate.py \
  tests/scripts/test_run_paper_reset.py \
  tests/scripts/test_run_paper_research_shadow.py \
  tests/scripts/test_paper_state.py tests/shared/test_models.py \
  tests/shared/test_config.py tests/services/ml_model/ -q
281 passed in 10.32s
```

## Release verification

```text
python -m pytest
724 passed in 11.66s

git diff --check
exit 0, no output

alembic upgrade 9b3d1c7e4a20 --sql
exit 0; generated offline PostgreSQL DDL includes provenance JSON NOT NULL

/opt/homebrew/bin/uv build --offline --wheel \
  --out-dir /tmp/algo-poc-wheel-final-fixes
Successfully built /tmp/algo-poc-wheel-final-fixes/algo_poc-0.1.0-py3-none-any.whl
```

Wheel inspection confirmed `research/`, `shared/`, `services/`, and `backtest/`
contents. No network or escalation was requested.

## Files changed

- `research/factors/contracts.py`
- `research/factors/operations.py`
- `research/factors/panel.py`
- `research/factors/engine.py`
- `research/factors/__init__.py`
- `research/shadow.py`
- `shared/models/research.py`
- `migrations/versions/9b3d1c7e4a20_add_research_candidates.py`
- `tests/research/test_contracts.py`
- `tests/research/test_operations.py`
- `tests/research/test_panel.py`
- `tests/research/test_engine.py`
- `tests/research/test_shadow.py`
- `tests/shared/test_models.py`
- `tests/scripts/test_run_paper_reset.py`

## Concerns and decisions

- Cross-sectional z-scores use population dispersion (`ddof=0`). This makes the
  cross-section itself the normalization population and yields deterministic
  `-1/+1` values for two distinct observations.
- An implicit universe (no `universe:member` field) is identified by the sorted
  panel columns and cutoff. Explicit membership uses the final membership row
  at the cutoff.
- The code revision is a SHA-256 checksum of the engine, panel contract/builder,
  operations module, selected factor implementations, and full factor specs;
  it does not depend on a mutable working-tree Git label.
- The migration was rendered only in Alembic offline SQL mode. It was not run
  against any database.

## Point-in-time universe follow-up

Whole-branch re-review identified that the initial catalog could not truthfully
apply cross-sectional normalization because the opt-in paper and cached
backtest paths do not supply authoritative dated S&P 500 or Russell 1000
membership. Follow-up implementation commit:
`6da652471b6ec9ea1cf76a7b22aa8e9677ecb292`.

The correction:

- Changes all four pre-release catalog normalization policies to `none`, keeping
  their `1.0.0` versions because no released/persisted contract exists yet.
- Retains the reusable z-score operation but requires an aligned
  `universe:member` frame for every factor that declares
  `cross_sectional_zscore`.
- Masks every date to membership equal to one before normalization and leaves
  removed/not-yet-added tickers missing. Tests mutate extreme nonmember values
  and prove active scores do not change.
- Adds immutable `provenance_for(date)` and `snapshot_identity_for(date)` APIs.
  Engine provenance uses that date's cutoff, as-of-clipped input checksum, and
  latest explicit membership snapshot at or before the date. Raw implicit
  universes are identified by the explicitly supplied panel columns.
- Makes both in-memory and SQL recorders persist/export and key by the candidate
  date's provenance identity.
- Adds a six-sleeve integration assertion proving the default shared snapshot
  records raw catalog values without changing failure isolation.

Follow-up RED evidence:

```text
python -m pytest tests/research/test_catalog.py \
  tests/research/test_engine.py tests/research/test_shadow.py -q
7 failed, 43 passed in 0.94s
```

The failures covered the still-cross-sectional catalog metadata, missing
membership acceptance, nonmember contamination, absent dated provenance APIs,
and absence of dated-provenance construction in recorders.

Follow-up GREEN and release evidence:

```text
python -m pytest tests/research/ tests/backtest/test_multi_portfolio.py -q
123 passed in 2.28s

python -m pytest tests/backtest/ tests/scripts/test_run_paper_gate.py \
  tests/scripts/test_run_paper_reset.py \
  tests/scripts/test_run_paper_research_shadow.py \
  tests/scripts/test_paper_state.py tests/shared/test_models.py \
  tests/shared/test_config.py tests/services/ml_model/ -q
282 passed in 10.65s

python -m pytest
731 passed in 12.65s

alembic upgrade 9b3d1c7e4a20 --sql
exit 0; offline PostgreSQL SQL generation only

/opt/homebrew/bin/uv build --offline --wheel \
  --out-dir /tmp/algo-poc-wheel-final-fixes-followup
Successfully built \
  /tmp/algo-poc-wheel-final-fixes-followup/algo_poc-0.1.0-py3-none-any.whl

git diff --check
exit 0, no output
```

This follow-up supersedes the earlier implicit-universe concern above: universe
identity no longer includes the candidate cutoff itself. Explicit identities
change only when dated membership changes; implicit raw identities represent
the supplied panel ticker columns, while candidate snapshot identity still
changes by date through `data_cutoff` and the as-of input checksum.
