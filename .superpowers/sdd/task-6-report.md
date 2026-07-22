# Task 6 Report: Project IB fills into sleeve positions

## Outcome

- Added a `portfolio_accounting` service whose `FillProjector.apply()` owns the
  durable fill transaction and returns `False` for an immutable broker replay.
- Inserted fills by `(account_id, execution_id)`, locked the durable intent,
  sleeve cash, and position, then updated commissions, cash, quantity, weighted
  average entry price, trades, and order lifecycle atomically.
- Kept unknown, mismatched, and invalid-but-identifiable executions durably
  audited while leaving cash, positions, and lifecycle unchanged before raising
  a projection error. Non-finite payloads that SQLite/PostgreSQL cannot safely
  persist are rejected before insertion.
- Reconciled delayed executions after completed-order history has already marked
  an intent `FILLED`, without attempting an illegal `FILLED -> FILLED`
  transition or double-counting `filled_quantity`.
- Prevented overfills, non-monotonic cumulative quantities, oversized sells,
  missing positions, negative buy cash, and conflicting execution-identity
  reuse.
- Added a runner using consumer group `portfolio_accounting`: startup pending
  messages are drained first, projection commits before acknowledgment,
  duplicates acknowledge successfully, deterministic malformed/rejected fills
  go to the fill DLQ, and transient unexpected failures remain pending.
- Refactored paper fill accounting behind a private transactional helper and
  removed both simulated `record_fill` calls from `scripts/run_paper.py`, so
  signals can no longer mutate the durable book before IB execution.
- Added the service package and Dockerfile.

## TDD evidence

Initial focused RED:

```text
pytest tests/services/portfolio_accounting/ -v
2 collection errors
ModuleNotFoundError: No module named 'services.portfolio_accounting'
```

Additional RED/GREEN cycles covered:

- non-finite economics reaching database/accounting state;
- rejected audit rows poisoning a later valid cumulative fill;
- unexpected database/runtime failures being DLQ'd and acknowledged;
- conflicting execution timestamps being accepted as immutable replays.

Focused GREEN:

```text
pytest tests/services/portfolio_accounting/ tests/scripts/test_paper_state.py \
  tests/backtest/test_paper_state.py tests/scripts/test_run_paper_gate.py -q
50 passed in 0.64s
```

## Verification

```text
pytest
697 passed in 11.37s

git diff --check
clean

python -m compileall -q services/portfolio_accounting \
  scripts/paper_state.py scripts/run_paper.py
clean
```

No PostgreSQL, Redis, or IB connection was made. No paper reset, database
destruction, volume removal, or live/paper broker order action was performed.

## Review

Self-review tightened immutable replay comparison to include execution time,
kept transient infrastructure failures pending instead of acknowledging them,
and reconstructed delayed completed-history fill progress without counting
rejected immutable audit rows.

## Independent-review correction (2026-07-22)

Independent review demonstrated that the reconstruction claim above was not
yet durable: structurally valid rejected audit rows could still be inferred as
applied.  Commit `72b56a1` adds an additive migration and a non-null
`execution_fills.projection_applied` marker.  The audit insert remains durable,
while accounting mutations, intent advancement, and flipping the marker to
`true` now share a savepoint.  Recovery counts only rows whose applied outcome
was committed.  Immutable timestamp comparison now normalizes both values to
UTC rather than discarding timezone offsets.

RED:

```text
/Users/huiliang/GitHub/algo-poc/.venv/bin/python -m pytest \
  tests/services/portfolio_accounting/test_projector.py -q
2 failed, 21 passed in 0.50s

test_replayed_fill_accepts_equivalent_timestamp_offset:
FillConflictError: execution identity conflicts on: executed_at

test_rejected_fill_is_not_reconstructed_as_applied_for_delayed_history:
Failed: DID NOT RAISE InvalidFillError
```

GREEN:

```text
/Users/huiliang/GitHub/algo-poc/.venv/bin/python -m pytest \
  tests/services/portfolio_accounting/ tests/scripts/test_paper_state.py \
  tests/backtest/test_paper_state.py tests/scripts/test_run_paper_gate.py \
  tests/shared/test_order_ledger_models.py -q
64 passed in 1.17s

/Users/huiliang/GitHub/algo-poc/.venv/bin/python -c \
  'import asyncio, pytest; asyncio.set_event_loop(asyncio.new_event_loop()); \
  raise SystemExit(pytest.main(["-q"]))'
699 passed in 12.22s

python -m alembic heads
c3a947f26510 (head)

python -m compileall -q services/portfolio_accounting \
  shared/models/order_ledger.py \
  migrations/versions/c3a947f26510_track_fill_projection_outcome.py
clean

git diff --check
clean
```

Files changed in the correction:

- `shared/models/order_ledger.py`
- `services/portfolio_accounting/projector.py`
- `tests/services/portfolio_accounting/test_projector.py`
- `migrations/versions/c3a947f26510_track_fill_projection_outcome.py`

Concern: under Python 3.14, the repository's plain full-suite command has one
pre-existing synchronous execution test that calls `asyncio.ensure_future`
without a current event loop.  The plain run produced `1 failed, 698 passed`;
initializing the event loop before pytest produced the clean 699-test result
above.  This correction does not touch that execution callback or its test.

## Migration safety correction (2026-07-22)

Re-review found that defaulting pre-existing `execution_fills` rows to
`projection_applied=false` would silently classify legacy rows whose actual
accounting outcome is unknowable.  Commit `09328cb` makes an empty
`execution_fills` table an explicit upgrade precondition.  The migration checks
before any schema mutation and fails with a manual-reconciliation instruction
when rows exist.  Because this feature branch has not been deployed, an empty
table is the only safe automatic upgrade path.  Downgrade continues to remove
the added column.

RED:

```text
/Users/huiliang/GitHub/algo-poc/.venv/bin/python -m pytest \
  tests/migrations/test_fill_projection_outcome_migration.py -q
1 failed, 2 passed in 0.19s

test_upgrade_refuses_to_classify_preexisting_execution_fills:
Failed: DID NOT RAISE RuntimeError
```

GREEN:

```text
/Users/huiliang/GitHub/algo-poc/.venv/bin/python -m pytest \
  tests/migrations/test_fill_projection_outcome_migration.py \
  tests/services/portfolio_accounting/test_projector.py \
  tests/shared/test_order_ledger_models.py -q
38 passed in 0.41s

/Users/huiliang/GitHub/algo-poc/.venv/bin/python -c \
  'import asyncio, pytest; asyncio.set_event_loop(asyncio.new_event_loop()); \
  raise SystemExit(pytest.main(["-q"]))'
702 passed in 12.10s

python -m alembic heads
c3a947f26510 (head)

python -m compileall -q \
  migrations/versions/c3a947f26510_track_fill_projection_outcome.py \
  tests/migrations/test_fill_projection_outcome_migration.py
clean

git diff --check
clean
```

Files changed in the migration safety correction:

- `migrations/versions/c3a947f26510_track_fill_projection_outcome.py`
- `tests/migrations/test_fill_projection_outcome_migration.py`

Concern: if an operator has independently applied the preceding durable-ledger
migration and accumulated fill rows before applying this revision, automatic
upgrade intentionally stops.  Those outcomes require explicit human
reconciliation; the migration does not delete, overwrite, or classify them.

## Migration runtime correction (2026-07-22)

Final re-review exercised the migration through Alembic rather than only the
operation mock.  Commit `9ce1025` adds real SQLite upgrade/downgrade coverage,
retains the safe `false` server default instead of issuing SQLite's unsupported
`ALTER COLUMN ... DROP DEFAULT`, and explicitly rejects offline SQL generation
because offline mode cannot query the table to enforce the empty-table safety
precondition.  Online PostgreSQL and SQLite upgrades still query for legacy
rows before changing the schema.

RED:

```text
/Users/huiliang/GitHub/algo-poc/.venv/bin/python -m pytest \
  tests/migrations/test_fill_projection_outcome_migration.py -q
3 failed, 3 passed in 0.56s

SQLite memory and file upgrades:
OperationalError: near "DEFAULT": syntax error
[SQL: ALTER TABLE execution_fills ALTER COLUMN projection_applied DROP DEFAULT]

Offline PostgreSQL generation:
AttributeError: 'MockConnection' object has no attribute 'scalar'
```

GREEN:

```text
/Users/huiliang/GitHub/algo-poc/.venv/bin/python -m pytest \
  tests/migrations/test_fill_projection_outcome_migration.py -q
6 passed in 0.32s

/Users/huiliang/GitHub/algo-poc/.venv/bin/python -m pytest \
  tests/migrations/test_fill_projection_outcome_migration.py \
  tests/services/portfolio_accounting/test_projector.py \
  tests/shared/test_order_ledger_models.py -q
41 passed in 0.46s

/Users/huiliang/GitHub/algo-poc/.venv/bin/python -c \
  'import asyncio, pytest; asyncio.set_event_loop(asyncio.new_event_loop()); \
  raise SystemExit(pytest.main(["-q"]))'
705 passed in 12.34s

python -m alembic heads
c3a947f26510 (head)

python -m compileall -q \
  migrations/versions/c3a947f26510_track_fill_projection_outcome.py \
  tests/migrations/test_fill_projection_outcome_migration.py
clean

git diff --check
clean
```

Files changed in the runtime correction:

- `migrations/versions/c3a947f26510_track_fill_projection_outcome.py`
- `tests/migrations/test_fill_projection_outcome_migration.py`

Concern: offline SQL generation is intentionally unsupported for this revision
and raises a clear `RuntimeError`; operators must run the migration online so
Alembic can verify that `execution_fills` is empty before adding the marker.
