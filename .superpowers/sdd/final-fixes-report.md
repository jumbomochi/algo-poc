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
