# Task 5 Report: Persist IB order and execution identity

## Outcome

- Enriched `FillMessage` and IB fill callbacks with execution, account, order,
  cumulative quantity, portfolio, and broker contract identity while retaining
  compatibility with legacy fill payloads that omit the new fields.
- Injected a production session-backed `OrderLedger` into the execution runner.
  The runner owns explicit commit/rollback boundaries; ledger repository methods
  remain transaction-neutral.
- Persisted `SUBMITTED` and the IB order ID before returning to Redis
  acknowledgment paths, and persisted `SUBMISSION_FAILED` on broker skip/failure.
- Restored submitted/partially-filled attribution and broker callbacks on
  startup. Fill recommendation/portfolio attribution is reloaded from the
  durable intent.
- Added broker status handling for `Cancelled`, `ApiCancelled`, `Inactive`, and
  completed-history-confirmed `Expired` orders.
- Set IB `orderRef` to the stable recommendation ID and recover matching broker
  orders before any resubmission, closing the IB-accepted/DB-not-persisted crash
  window.
- Ended read-only SQLAlchemy transactions before every broker/Redis await so IB
  callbacks do not interleave on an active shared session transaction.

## TDD evidence

Initial focused RED:

```text
pytest tests/shared/test_schemas.py tests/services/execution/test_runner.py tests/services/execution/test_partial_fills.py -v
4 failed, 36 passed, 10 errors
```

The failures/errors were at the intended missing interfaces: fill schema fields,
ledger injection/lifecycle handlers, enriched IB payload, and orderRef recovery.

Additional focused RED cycles found during self-review:

```text
restart completed-history reconciliation: 1 failed (missing restore_order_by_ref)
submission await/session boundary: 1 failed (transaction remained open)
IB rejection reason extraction: 1 failed (missing Trade.log extraction)
review findings: 3 failed (completed orderRef lookup, fail-closed restore,
terminal-intent late-fill attribution)
```

Focused GREEN:

```text
pytest tests/shared/test_schemas.py tests/services/execution/ -v
77 passed in 0.38s
```

## Verification

```text
pytest
662 passed in 11.95s

git diff --check
clean
```

No IB, Redis, or PostgreSQL connection was made. No order was placed, modified,
or cancelled; all broker/service behavior was exercised with mocks and SQLite
in-memory sessions.

## Commit

`feat: persist IB order and execution identity`

## Concerns

- Broker integration behavior is unit-tested against the ib_insync-facing
  interface but intentionally not exercised against a real paper/live Gateway
  under the repository safety constraints.
- Legacy tests can still construct a runner without a ledger for isolated unit
  compatibility; the production entry point always injects a real
  session-backed ledger.

## Review

Independent review identified one Critical and three Important findings. All
were addressed before commit: completed orders are included in orderRef crash
recovery, missing active broker orders fail startup closed, late fills query
terminal durable intents by account/order ID, and the extended executor methods
are declared in `IBExecutorProtocol`.

## Follow-up controller review

Controller review of `473eaad` identified three lifecycle races. Focused RED
captured five failures: durable broker failure still raised after committing,
terminal replay called the broker, Inactive ignored broker filled quantity,
completed Filled recovery stayed SUBMITTED, and the status payload omitted
filled quantity. A sixth RED verified that ordinary live Filled status must wait
for the fill projector.

The follow-up fixes make submission failure ack-safe, short-circuit every
terminal replay, include broker filled quantity in status callbacks, reconcile
recovered orders only after the submission commit, and transition completed-
history-confirmed Filled orders while leaving live Filled status for Task 6.

```text
pytest tests/shared/test_schemas.py tests/services/execution/ -q
82 passed in 0.61s

pytest
667 passed in 13.00s
```

## Completed Inactive follow-up

Final review found that completed-history `Inactive` orders discarded broker
reason context and could stay active when IB supplied no reason. Three RED tests
captured Trade.log reason loss, missing fallback reason, and the durable order
remaining SUBMITTED. Restore now uses `_status_reason(trade)` and supplies
`IB completed order is Inactive` when IB history has no context; the runner also
enforces that fallback for confirmed completed status payloads.

```text
pytest tests/shared/test_schemas.py tests/services/execution/ -q
85 passed in 0.39s

pytest
670 passed in 11.78s
```
