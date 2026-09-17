# KAN-87 Execution Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ask IB once per daily run what it actually executed, and project any fill the live `execDetails` callback missed, so a sell that fills after the close can never again become a phantom position.

**Architecture:** A pure planning function (`plan_sweep`) decides what to do with a list of normalized broker executions, using the existing `OrderLedger` for attribution and duplicate detection. A thin `ib_insync` adapter fetches them via `reqExecutionsAsync`. `scripts/run_paper.py` runs the sweep before reconciliation and publishes recovered fills to `stream:fills`, where the existing `FillProjector` applies them through the normal path — no new accounting code. Fills recovered this way carry `recovery_source='ib_execution_sweep'` so the 04:52 digest can count them.

**Tech Stack:** Python 3.12, `ib_insync`, SQLAlchemy 2.x, Alembic, Redis Streams, pytest (`asyncio_mode="auto"`).

**Spec:** https://huiliang.atlassian.net/browse/KAN-87 (the JIRA description IS the spec — 7 numbered ACs, 6-row test matrix, explicit out-of-scope list). Quoted inline per task; read it alongside this plan.

## Global Constraints

- All modules use `from __future__ import annotations`.
- Tests use pytest with `asyncio_mode = "auto"` (no `@pytest.mark.asyncio`).
- Alembic head at plan time is **`f2c9a6d81b74`** (`widen_trade_identifier_columns`). The new migration's `down_revision` is that value. Verify it is still the single head before writing the migration: a second head breaks `alembic upgrade head` and `run_paper.sh`'s schema guard aborts the next 04:15 run.
- **The sweep must never gate the run.** AC5: IB unreachable leaves the exit code and reconciliation unchanged.
- **The sweep must be idempotent.** AC2: `FillProjector` already keys on `(account_id, execution_id)`; do not add a second dedupe mechanism, use the existing `OrderLedger.execution_fill_exists`.
- Do NOT move the run before the close or switch sells to LMT — explicitly out of scope in the spec ("this story makes the existing design observable, it does not alter it").
- Do NOT attempt to fix KAN-85 (the repair tool). Out of scope and independent.
- Operator-only steps (applying the migration, restarting containers) are documented and handed over, never executed by the implementer — repo CLAUDE.md destructive-actions policy.

## Verified current state

Facts confirmed against the tree at `e22bb2f` before planning. Re-verify if the tree moved.

| Fact | Evidence |
|---|---|
| Nothing calls `reqExecutions` | `grep -rn "reqExecutions" services/ scripts/ shared/` → no hits; only `reqCompletedOrdersAsync` at `ib_executor.py:608,632,657,783` |
| The false terminalization | `services/execution/ib_executor.py:806-822` — sends `{"status": "Expired", "reason": "order absent from IB after session boundary", "order_absent_at_ib": True}` |
| …and how it persists | `services/execution/runner.py:1431-1436` sets `target = OrderStatus.EXPIRED`; `:1464` calls `transition(..., reason=reason)`, so `intent.reason` holds that exact string and `intent.status == "EXPIRED"` |
| `EXPIRED` is a dead end | `shared/order_ledger.py:31-54` — `ALLOWED_TRANSITIONS` has **no** `EXPIRED` key, so `.get(EXPIRED, set())` is empty and every transition out of it raises `InvalidOrderTransition` |
| The projector transitions the intent | `services/portfolio_accounting/projector.py:435-439` — `FILLED` or `PARTIALLY_FILLED` via `self._ledger.transition(...)` |
| Existing duplicate detection | `shared/order_ledger.py:192-201` `execution_fill_exists(account_id, execution_id)`; unique constraint `uq_execution_fill_account_exec` |
| Attribution by broker order id | `shared/order_ledger.py:183-190` `get_by_ib_order_id(ib_order_id, account_id=...)` |
| The live fill payload shape to mirror | `services/execution/ib_executor.py:536-559` |
| Where the run touches IB | `scripts/run_paper.py:1341-1365` `read_broker_snapshot` — connects, reads, disconnects in `finally` |
| Where reconciliation happens | `scripts/run_paper.py:1736` (snapshot) then `:1746` `prepare_daily_run`, which calls `reconcile_snapshot` at `:608`. **The sweep goes between them.** |
| Sequential same-client_id connects are the existing pattern | `scripts/run_paper.py:1788` `fetch_bars_from_ib(..., client_id=args.ib_client_id)` reconnects with the same id after `read_broker_snapshot` disconnected |
| The digest reads the ledger, not artifacts | `scripts/ops/pipeline_report_summary.py:96-155` `collect_facts` derives every fact from DB tables with a `since` bound |

---

### Task 1: Record fill provenance

Adds the column that lets the digest tell a recovered fill from a live one. Nothing reads it yet.

**Files:**
- Modify: `shared/models/order_ledger.py:135-137` (add field after `projection_applied`)
- Modify: `shared/schemas/messages.py:146-168` (add field to `FillMessage`)
- Modify: `services/portfolio_accounting/projector.py:194-213` (`_fill_values` passthrough)
- Create: `migrations/versions/<rev>_add_execution_fill_recovery_source.py`
- Test: `tests/services/portfolio_accounting/test_projector.py`

**Interfaces:**
- Produces: `ExecutionFill.recovery_source: str | None`; `FillMessage.recovery_source: str | None = None`; the constant `RECOVERY_SOURCE_SWEEP = "ib_execution_sweep"` exported from `services/execution/execution_sweep.py` in Task 3 — Task 1 uses the literal string, Task 3 introduces the constant and Task 4 imports it.

- [ ] **Step 1: Write the failing test**

In `tests/services/portfolio_accounting/test_projector.py`, alongside the existing projector tests (reuse whatever session/fixture helper the neighbouring tests use to build a `FillMessage` and an attributable `OrderIntent`):

```python
def test_a_swept_fill_records_its_provenance(session) -> None:
    """A fill recovered by the sweep is distinguishable from a live one.

    The 04:52 digest counts recovered fills; without a marker on the row
    there is nothing to count, because the projector is the only writer
    and it cannot tell where the message came from.
    """
    intent = _approved_submitted_intent(session, ib_order_id="148")
    fill = _fill_message(intent, execution_id="exec-swept-1")
    fill = fill.model_copy(update={"recovery_source": "ib_execution_sweep"})

    FillProjector(session).apply(fill)

    row = session.scalar(
        select(ExecutionFill).where(
            ExecutionFill.execution_id == "exec-swept-1"
        )
    )
    assert row.recovery_source == "ib_execution_sweep"


def test_a_live_fill_has_no_recovery_source(session) -> None:
    """The default stays None so every existing fill reads as live."""
    intent = _approved_submitted_intent(session, ib_order_id="149")
    fill = _fill_message(intent, execution_id="exec-live-1")

    FillProjector(session).apply(fill)

    row = session.scalar(
        select(ExecutionFill).where(
            ExecutionFill.execution_id == "exec-live-1"
        )
    )
    assert row.recovery_source is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/portfolio_accounting/test_projector.py -k recovery -v`
Expected: FAIL — `FillMessage` has no field `recovery_source` (pydantic ignores it on `model_copy`, then `AttributeError`/`assert None == "ib_execution_sweep"`).

- [ ] **Step 3: Add the model column**

In `shared/models/order_ledger.py`, immediately after `projection_applied` (line 135-137):

```python
    #: How this fill reached the book. ``None`` is the live ``execDetails``
    #: callback — every row written before KAN-87 and every row written by
    #: the normal path. ``"ib_execution_sweep"`` means the callback was
    #: missed and the daily sweep re-read the execution from IB.
    #:
    #: Deliberately NOT in ``_IMMUTABLE_FILL_FIELDS``: a live callback can
    #: legitimately re-report an execution the sweep already recorded (the
    #: execution service reconnects and replays), and that arrives with
    #: ``recovery_source=None`` against a stored ``"ib_execution_sweep"``.
    #: Treating that as an identity conflict would dead-letter a fill whose
    #: economics match perfectly.
    recovery_source: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
```

- [ ] **Step 4: Add the schema field**

In `shared/schemas/messages.py`, in `FillMessage` after `order_done` (line 168):

```python
    # Provenance. None = live execDetails callback. "ib_execution_sweep" =
    # recovered by the daily sweep (KAN-87). Additive optional field with a
    # default, so per the evolution rule at the top of this module it does
    # NOT require a CURRENT_SCHEMA_VERSION bump.
    recovery_source: str | None = None
```

- [ ] **Step 5: Pass it through the projector**

In `services/portfolio_accounting/projector.py`, in the dict returned by `_fill_values`, after `"executed_at": fill.timestamp,`:

```python
            "recovery_source": fill.recovery_source,
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/services/portfolio_accounting/test_projector.py -v`
Expected: PASS, including the pre-existing projector tests (the new column is nullable, so nothing else changes).

- [ ] **Step 7: Generate and edit the migration**

```bash
alembic revision -m "add execution_fill recovery_source"
```

Then edit the generated file so it reads exactly:

```python
"""add execution_fills.recovery_source

Additive only: one new nullable column, no backfill, no data change.
NULL means the live execDetails callback wrote the row — which is every
row that exists when this migration runs. "ib_execution_sweep" is written
only by the KAN-87 daily sweep.

Safe to apply on its own and safe to leave unapplied for a while: nothing
reads the column until the sweep and the digest ship together. It still
must be applied BEFORE the next 04:15 paper run on any host that has
pulled this code, because run_paper.sh compares the DB revision against
the code's head and aborts on a mismatch (the 2026-08-26 abort).
"""

from alembic import op
import sqlalchemy as sa

revision = "<generated>"
down_revision = "f2c9a6d81b74"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_fills",
        sa.Column("recovery_source", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execution_fills", "recovery_source")
```

- [ ] **Step 8: Verify the migration graph still has one head**

Run: `alembic heads`
Expected: exactly one line, the new revision. If two appear, stop — a second head breaks `run_paper.sh`'s schema guard.

- [ ] **Step 9: Commit**

```bash
git add shared/models/order_ledger.py shared/schemas/messages.py \
  services/portfolio_accounting/projector.py migrations/versions/ \
  tests/services/portfolio_accounting/test_projector.py
git commit -m "KAN-87: record whether a fill came from the sweep or the callback"
```

---

### Task 2: A narrow correction out of a false EXPIRED

AC4: *"An order terminalized with `order_absent_at_ib` whose execution IS found is corrected from EXPIRED to FILLED."*

`EXPIRED` is terminal with no outbound transitions, and that invariant is worth keeping. So rather than opening `EXPIRED → FILLED` globally in `ALLOWED_TRANSITIONS` (which would let *any* caller resurrect *any* expired order), this adds one gated method that restores the intent to `SUBMITTED` — the state it was in before the false terminalization — and lets the normal projector path do `SUBMITTED → FILLED`.

The gate is deliberately two-part: status is `EXPIRED` **and** the reason is the exact marker the false terminalization writes. A genuinely expired order has a different reason (or none) and is refused.

**Files:**
- Modify: `shared/order_ledger.py` (new method after `transition`, ~line 280)
- Test: `tests/shared/test_order_ledger.py`

**Interfaces:**
- Consumes: `OrderStatus`, `InvalidOrderTransition` (already in this module).
- Produces: `ABSENT_AT_IB_REASON: str` and `OrderLedger.restore_absent_terminalization(recommendation_id: str) -> OrderIntent`. Task 3 imports both.

- [ ] **Step 1: Write the failing tests**

In `tests/shared/test_order_ledger.py`:

```python
class TestRestoreAbsentTerminalization:
    """EXPIRED is terminal; exactly one evidence-gated way back out."""

    def test_an_absent_terminalized_intent_is_restored_to_submitted(
        self, ledger, session
    ) -> None:
        intent = _submitted_intent(session, ledger, "rec-absent")
        ledger.transition(
            "rec-absent",
            OrderStatus.EXPIRED,
            reason=ABSENT_AT_IB_REASON,
        )

        restored = ledger.restore_absent_terminalization("rec-absent")

        assert restored.status == OrderStatus.SUBMITTED.value
        assert restored.terminal_at is None

    def test_a_genuinely_expired_intent_is_refused(
        self, ledger, session
    ) -> None:
        """A day order that really expired must stay expired."""
        _submitted_intent(session, ledger, "rec-real")
        ledger.transition(
            "rec-real",
            OrderStatus.EXPIRED,
            reason="IB reported the order Expired",
        )

        with pytest.raises(InvalidOrderTransition):
            ledger.restore_absent_terminalization("rec-real")

    def test_a_non_expired_intent_is_refused(self, ledger, session) -> None:
        _submitted_intent(session, ledger, "rec-open")

        with pytest.raises(InvalidOrderTransition):
            ledger.restore_absent_terminalization("rec-open")
```

`_submitted_intent` is a helper creating an intent and driving it to `SUBMITTED` — reuse the existing helper in this test module if one exists rather than adding a second.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/shared/test_order_ledger.py -k RestoreAbsent -v`
Expected: FAIL — `ImportError` / `AttributeError: 'OrderLedger' object has no attribute 'restore_absent_terminalization'`.

- [ ] **Step 3: Add the constant**

In `shared/order_ledger.py`, near `TERMINAL_STATUSES` (after line 68):

```python
#: The exact reason ``IBExecutor.restore_order_by_ref`` writes when an order
#: is absent from both open trades and completed-order history after a
#: session boundary (``services/execution/ib_executor.py:818``). It is the
#: only evidence that an EXPIRED intent was terminalized on absence rather
#: than on IB actually reporting the order expired — and so the only thing
#: that makes the terminalization reversible. Keep the two in sync.
ABSENT_AT_IB_REASON = "order absent from IB after session boundary"
```

- [ ] **Step 4: Add the method**

In `shared/order_ledger.py`, immediately after `transition`:

```python
    def restore_absent_terminalization(
        self, recommendation_id: str
    ) -> OrderIntent:
        """Undo a terminalization that absence caused and evidence refutes.

        ``EXPIRED`` has no entry in :data:`ALLOWED_TRANSITIONS` and that is
        correct: a terminal order stays terminal. But one EXPIRED is a
        guess — the one written when an order was missing from IB after a
        session boundary — and when the broker's own execution record turns
        up, the guess is simply wrong. This restores the intent to
        ``SUBMITTED``, the state it held before the guess, so the ordinary
        fill path can terminalize it properly.

        Refuses anything else. A genuinely expired order carries a
        different reason and must stay expired.
        """
        intent = self._locked(recommendation_id, required=True)
        if intent.status != OrderStatus.EXPIRED.value:
            raise InvalidOrderTransition(
                f"cannot restore {recommendation_id} from "
                f"{intent.status}: only EXPIRED is restorable"
            )
        if intent.reason != ABSENT_AT_IB_REASON:
            raise InvalidOrderTransition(
                f"refusing to restore {recommendation_id}: it expired with "
                f"reason {intent.reason!r}, not on absence from IB"
            )
        intent.status = OrderStatus.SUBMITTED.value
        intent.reason = None
        intent.terminal_at = None
        intent.updated_at = _utcnow()
        self.session.flush()
        return intent
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/shared/test_order_ledger.py -v`
Expected: PASS, all of them.

- [ ] **Step 6: Commit**

```bash
git add shared/order_ledger.py tests/shared/test_order_ledger.py
git commit -m "KAN-87: one evidence-gated way back out of an absence-guessed EXPIRED"
```

---

### Task 3: The sweep decision, as a pure function

All five AC behaviours decided here, with no IB and no Redis, so they are cheap to test exhaustively.

**Files:**
- Create: `services/execution/execution_sweep.py`
- Test: `tests/services/execution/test_execution_sweep.py`

**Interfaces:**
- Consumes: `OrderLedger.execution_fill_exists`, `OrderLedger.get_by_ib_order_id`, `OrderLedger.restore_absent_terminalization`, `ABSENT_AT_IB_REASON` (Task 2); `FillMessage` with `recovery_source` (Task 1).
- Produces:
  - `RECOVERY_SOURCE_SWEEP: str = "ib_execution_sweep"`
  - `@dataclass(frozen=True) SweptExecution` with fields `execution_id: str`, `account_id: str`, `ib_order_id: str`, `con_id: int`, `ticker: str`, `exchange: str`, `currency: str`, `side: str`, `quantity: float`, `cumulative_quantity: float`, `price: float`, `commission: float`, `commission_currency: str | None`, `executed_at: datetime`
  - `@dataclass(frozen=True) SweepOutcome` with `recovered: tuple[FillMessage, ...]`, `already_recorded: int`, `untracked: tuple[str, ...]`, `corrected: tuple[str, ...]`
  - `plan_sweep(executions: Sequence[SweptExecution], ledger: OrderLedger) -> SweepOutcome`

- [ ] **Step 1: Write the failing tests**

Create `tests/services/execution/test_execution_sweep.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

from services.execution.execution_sweep import (
    RECOVERY_SOURCE_SWEEP,
    SweptExecution,
    plan_sweep,
)
from shared.models import OrderStatus
from shared.order_ledger import ABSENT_AT_IB_REASON


def _execution(**overrides) -> SweptExecution:
    base = dict(
        execution_id="exec-1",
        account_id="DUN551088",
        ib_order_id="148",
        con_id=756733,
        ticker="UNH",
        exchange="SMART",
        currency="USD",
        side="sell",
        quantity=6.0,
        cumulative_quantity=6.0,
        price=341.22,
        commission=1.0,
        commission_currency="USD",
        executed_at=datetime(2026, 9, 14, 13, 31, tzinfo=UTC),
    )
    base.update(overrides)
    return SweptExecution(**base)


def test_an_unrecorded_execution_is_published(ledger, session) -> None:
    """AC1: the fill the callback missed reaches stream:fills."""
    intent = _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")

    outcome = plan_sweep([_execution()], ledger)

    assert len(outcome.recovered) == 1
    fill = outcome.recovered[0]
    assert fill.execution_id == "exec-1"
    assert fill.recommendation_id == intent.recommendation_id
    assert fill.portfolio == intent.portfolio
    assert fill.recovery_source == RECOVERY_SOURCE_SWEEP
    assert outcome.already_recorded == 0


def test_an_already_recorded_execution_is_not_republished(
    ledger, session
) -> None:
    """AC2: no double-count."""
    _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")
    _record_execution_fill(session, account_id="DUN551088", execution_id="exec-1")

    outcome = plan_sweep([_execution()], ledger)

    assert outcome.recovered == ()
    assert outcome.already_recorded == 1


def test_a_double_sweep_does_not_double_count(ledger, session) -> None:
    """AC2: running the same plan twice over a recorded fill stays empty."""
    _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")
    _record_execution_fill(session, account_id="DUN551088", execution_id="exec-1")

    first = plan_sweep([_execution()], ledger)
    second = plan_sweep([_execution()], ledger)

    assert first.recovered == () and second.recovered == ()
    assert second.already_recorded == 1


def test_an_untracked_order_is_reported_never_projected(
    ledger, session
) -> None:
    """AC3: someone else's order is named, not booked."""
    outcome = plan_sweep([_execution(ib_order_id="999")], ledger)

    assert outcome.recovered == ()
    assert outcome.untracked == ("999",)


def test_an_absence_expired_intent_is_corrected(ledger, session) -> None:
    """AC4: EXPIRED-on-absence becomes fillable again, and is reported."""
    intent = _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")
    ledger.transition(
        intent.recommendation_id,
        OrderStatus.EXPIRED,
        reason=ABSENT_AT_IB_REASON,
    )

    outcome = plan_sweep([_execution()], ledger)

    assert outcome.corrected == (intent.recommendation_id,)
    assert len(outcome.recovered) == 1
    assert (
        session.get(type(intent), intent.id).status
        == OrderStatus.SUBMITTED.value
    )


def test_a_genuinely_expired_intent_is_left_alone(ledger, session) -> None:
    """The correction is evidence-gated, not a blanket un-expire."""
    intent = _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")
    ledger.transition(
        intent.recommendation_id,
        OrderStatus.EXPIRED,
        reason="IB reported the order Expired",
    )

    outcome = plan_sweep([_execution()], ledger)

    assert outcome.corrected == ()
    assert outcome.recovered == ()
    assert outcome.untracked == ("148",)
```

`_submitted_intent` and `_record_execution_fill` are local helpers; write them at the top of the file against the real models (`OrderIntent`, `ExecutionFill`) using the session fixture this test package already provides.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/services/execution/test_execution_sweep.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.execution.execution_sweep'`.

- [ ] **Step 3: Write the implementation**

Create `services/execution/execution_sweep.py`:

```python
"""Re-read from IB the executions the live callback never delivered.

Every order the daily run places is submitted ~21 minutes after the US
close and rests until the next open. The process that placed it is long
gone by then, so ``execDetails`` fires into nothing and the fill is never
booked. Position-level reconciliation notices that *something* changed but
cannot recover *what* — so the position becomes a phantom, blocks every
buy, and the repair tool writes its value off (KAN-85).

IB's own execution record survives the session boundary that the order
state does not. That asymmetry is what this module exploits. It decides
only; fetching and publishing live at the edges (Task 4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from shared.order_ledger import InvalidOrderTransition, OrderLedger
from shared.schemas.messages import FillMessage

#: Written to ``ExecutionFill.recovery_source`` for every fill this module
#: recovers, so the 04:52 digest can count them. A sweep that silently
#: recovers fills every day is hiding a worsening upstream problem.
RECOVERY_SOURCE_SWEEP = "ib_execution_sweep"


@dataclass(frozen=True)
class SweptExecution:
    """One broker execution, normalized off ``ib_insync``'s Fill."""

    execution_id: str
    account_id: str
    ib_order_id: str
    con_id: int
    ticker: str
    exchange: str
    currency: str
    side: str
    quantity: float
    cumulative_quantity: float
    price: float
    commission: float
    commission_currency: str | None
    executed_at: datetime


@dataclass(frozen=True)
class SweepOutcome:
    """What the sweep decided. Counts are reported even when zero."""

    recovered: tuple[FillMessage, ...] = ()
    already_recorded: int = 0
    untracked: tuple[str, ...] = ()
    corrected: tuple[str, ...] = ()


def plan_sweep(
    executions: Sequence[SweptExecution], ledger: OrderLedger
) -> SweepOutcome:
    """Decide what to do with each execution IB reports.

    Four outcomes, in the order they are tested:

    - already in ``execution_fills`` -> counted, nothing published (AC2);
    - no ``order_intents`` row for the broker order id -> named in
      ``untracked``, never projected (AC3);
    - the intent was terminalized EXPIRED *on absence* -> restored so the
      ordinary fill path can terminalize it properly (AC4), then published;
    - otherwise -> published (AC1).
    """
    recovered: list[FillMessage] = []
    untracked: list[str] = []
    corrected: list[str] = []
    already_recorded = 0

    for execution in executions:
        if ledger.execution_fill_exists(
            execution.account_id, execution.execution_id
        ):
            already_recorded += 1
            continue

        intent = ledger.get_by_ib_order_id(
            execution.ib_order_id, account_id=execution.account_id
        )
        if intent is None:
            untracked.append(execution.ib_order_id)
            continue

        try:
            restored = ledger.restore_absent_terminalization(
                intent.recommendation_id
            )
        except InvalidOrderTransition:
            # Either it was never terminalized (the normal case — nothing
            # to correct) or it expired for a real reason, which this must
            # not overturn. Both are decided by the guard below.
            restored = None
        else:
            corrected.append(restored.recommendation_id)
            intent = restored

        if intent.status not in _FILLABLE_STATUSES:
            # Terminal for a reason the broker record does not refute.
            # Report it as untracked rather than forcing a projection the
            # ledger would reject anyway.
            untracked.append(execution.ib_order_id)
            continue

        recovered.append(_to_fill_message(execution, intent))

    return SweepOutcome(
        recovered=tuple(recovered),
        already_recorded=already_recorded,
        untracked=tuple(untracked),
        corrected=tuple(corrected),
    )


#: The states the projector can advance to FILLED/PARTIALLY_FILLED.
#: Mirrors ``ALLOWED_TRANSITIONS`` in ``shared.order_ledger``.
_FILLABLE_STATUSES = ("SUBMITTED", "PARTIALLY_FILLED")


def _to_fill_message(
    execution: SweptExecution, intent: object
) -> FillMessage:
    """Build the same message the live callback would have published.

    Every field the projector's ``_REQUIRED_IDENTITY_FIELDS`` demands is
    present: execution_id, account_id, portfolio, con_id, exchange,
    currency. A fill missing any of them is dead-lettered (the 2026-08-01
    DLQ backlog), so this is not optional enrichment.
    """
    return FillMessage(
        ticker=execution.ticker,
        timestamp=execution.executed_at,
        side=execution.side,
        quantity=execution.quantity,
        fill_price=execution.price,
        commission=execution.commission,
        commission_currency=execution.commission_currency,
        recommendation_id=intent.recommendation_id,
        order_id=execution.ib_order_id,
        execution_id=execution.execution_id,
        account_id=execution.account_id,
        cumulative_quantity=execution.cumulative_quantity,
        portfolio=intent.portfolio,
        con_id=execution.con_id,
        exchange=execution.exchange,
        currency=execution.currency,
        # NOTE: the field is `requested_quantity` on OrderIntent — there is
        # no `intent.quantity`. `quantity` belongs to ExecutionFill.
        order_done=(
            execution.cumulative_quantity >= float(intent.requested_quantity)
        ),
        recovery_source=RECOVERY_SOURCE_SWEEP,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/services/execution/test_execution_sweep.py -v`
Expected: PASS, all seven.

- [ ] **Step 5: Commit**

```bash
git add services/execution/execution_sweep.py \
  tests/services/execution/test_execution_sweep.py
git commit -m "KAN-87: decide which broker executions the book never saw"
```

---

### Task 4: Fetch from IB and run it in the daily run

**Files:**
- Modify: `services/execution/execution_sweep.py` (append the adapter)
- Modify: `scripts/run_paper.py` (new `read_broker_executions`, wire before `prepare_daily_run` at `:1746`)
- Test: `tests/scripts/test_run_paper_execution_sweep.py` (create)

**Interfaces:**
- Consumes: `plan_sweep`, `SweptExecution`, `SweepOutcome` (Task 3).
- Produces: `executions_from_ib_fills(fills) -> list[SweptExecution]`; `read_broker_executions(*, host, port, client_id) -> list[SweptExecution]`; `run_execution_sweep(*, executions, session, redis_url) -> SweepOutcome`.

- [ ] **Step 1: Write the failing test**

Create `tests/scripts/test_run_paper_execution_sweep.py`:

```python
def test_ib_unreachable_does_not_change_the_run(monkeypatch, session) -> None:
    """AC5: the sweep is a recovery path, never a precondition."""
    def _boom(**kwargs):
        raise ConnectionError("gateway down")

    monkeypatch.setattr(run_paper, "read_broker_executions", _boom)

    outcome = run_paper.sweep_executions_best_effort(
        host="127.0.0.1", port=7497, client_id=58, session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert outcome is None  # reported, not raised


def test_a_normalized_ib_fill_becomes_a_swept_execution() -> None:
    """The adapter reads ib_insync's shape without importing it in tests."""
    fill = SimpleNamespace(
        execution=SimpleNamespace(
            execId="0000e1a7.68c1b2d4.01.01",
            acctNumber="DUN551088",
            orderId=148,
            side="SLD",
            shares=6.0,
            cumQty=6.0,
            price=341.22,
            time=datetime(2026, 9, 14, 13, 31, tzinfo=UTC),
        ),
        contract=SimpleNamespace(
            conId=756733, symbol="UNH", exchange="SMART", currency="USD"
        ),
        commissionReport=SimpleNamespace(commission=1.0, currency="USD"),
    )

    [swept] = executions_from_ib_fills([fill])

    assert swept.execution_id == "0000e1a7.68c1b2d4.01.01"
    assert swept.ib_order_id == "148"
    assert swept.side == "sell"      # SLD -> sell, BOT -> buy
    assert swept.quantity == 6.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/scripts/test_run_paper_execution_sweep.py -v`
Expected: FAIL — `ImportError` on `executions_from_ib_fills` / `AttributeError` on `run_paper.sweep_executions_best_effort`.

- [ ] **Step 3: Write the adapter**

Append to `services/execution/execution_sweep.py`:

```python
#: IB reports the side of an execution as BOT/SLD, not buy/sell.
_IB_SIDE = {"BOT": "buy", "SLD": "sell"}


def executions_from_ib_fills(fills: Sequence[object]) -> list[SweptExecution]:
    """Normalize ``ib_insync`` Fill objects, skipping anything unusable.

    Mirrors the payload the live callback builds
    (``ib_executor.py:536-559``) so a recovered fill is indistinguishable
    from the one that should have arrived.
    """
    swept: list[SweptExecution] = []
    for fill in fills:
        execution = fill.execution
        side = _IB_SIDE.get(str(execution.side).upper())
        if side is None:
            continue
        report = getattr(fill, "commissionReport", None)
        swept.append(
            SweptExecution(
                execution_id=str(execution.execId),
                account_id=str(execution.acctNumber),
                ib_order_id=str(execution.orderId),
                con_id=int(fill.contract.conId),
                ticker=str(fill.contract.symbol),
                exchange=fill.contract.exchange or "SMART",
                currency=fill.contract.currency or "USD",
                side=side,
                quantity=float(execution.shares),
                cumulative_quantity=float(execution.cumQty),
                price=float(execution.price),
                commission=float(getattr(report, "commission", 0.0) or 0.0),
                commission_currency=(
                    str(getattr(report, "currency", "") or "") or None
                ),
                executed_at=execution.time,
            )
        )
    return swept
```

- [ ] **Step 4: Add the reader and the best-effort wrapper to run_paper.py**

Add beside `read_broker_snapshot` (after line 1365). It connects with the **same** `client_id`, sequentially, after `read_broker_snapshot` has disconnected — the pattern `fetch_bars_from_ib` at `:1788` already uses. Do not invent a second client id; IB refuses a duplicate and the 2026-08-30 collision came from exactly that.

```python
async def read_broker_executions(
    *, host: str, port: int, client_id: int
) -> list[SweptExecution]:
    """Ask IB what it executed this trading day.

    ``reqExecutions`` serves the CURRENT trading day only. The fill lands
    at the next open (13:30 UTC) and this runs at 20:15 UTC the same day,
    still inside that window — which is why it recovers the misses at all.
    By the fifth day the record is gone (confirmed 2026-09-16, when UNH's
    price had to come off an IB statement by hand).
    """
    from ib_insync import IB, ExecutionFilter

    ib = IB()
    try:
        await ib.connectAsync(
            host, port, clientId=client_id, readonly=True, timeout=15
        )
        return executions_from_ib_fills(
            await ib.reqExecutionsAsync(ExecutionFilter())
        )
    finally:
        if ib.isConnected():
            ib.disconnect()


def sweep_executions_best_effort(
    *, host: str, port: int, client_id: int, session: Session, redis_url: str
) -> SweepOutcome | None:
    """Recover missed fills, or report why not. Never raises.

    AC5: a sweep failure must not change the run's exit code or block
    reconciliation. Returns ``None`` when IB could not be reached.
    """
    try:
        executions = asyncio.run(
            read_broker_executions(host=host, port=port, client_id=client_id)
        )
    except Exception as exc:
        print(
            f"WARNING: execution sweep skipped — could not read executions "
            f"from IB ({exc}). Reconciliation continues; a missed fill stays "
            f"missed until the next run."
        )
        return None

    outcome = plan_sweep(executions, OrderLedger(session))
    session.commit()
    for fill in outcome.recovered:
        publish_fill_message(fill, redis_url=redis_url)
    print(
        f"Execution sweep: {len(outcome.recovered)} recovered, "
        f"{outcome.already_recorded} already recorded, "
        f"{len(outcome.untracked)} untracked, "
        f"{len(outcome.corrected)} corrected"
    )
    return outcome
```

Add `publish_fill_message` immediately above it. Read `publish_unpublished_intents` (`scripts/run_paper.py:1119-1212`) first and mirror how it builds its Redis client and its timeouts — do not introduce a second connection style:

```python
def publish_fill_message(fill: FillMessage, *, redis_url: str) -> None:
    """Write one recovered fill to ``stream:fills``.

    Deliberately the same stream and the same message type the live
    callback publishes, so ``FillProjector`` applies it through the
    existing path — position close, realised P&L, cash, intent advanced.
    No new accounting code; this is the path that repaired UNH correctly
    by hand.
    """
    import redis

    client = redis.from_url(redis_url)
    try:
        client.xadd("stream:fills", fill.to_stream_dict())
    finally:
        client.close()
```

A publish failure must not abort the run: `sweep_executions_best_effort` already wraps the fetch, so wrap the publish loop in its own `try/except` that prints a warning naming the execution id and continues.

Such a failure is self-healing, and it is worth being precise about why. The sweep never writes `execution_fills` — the **projector** does, on consuming `stream:fills`. So a failed publish leaves no row, `execution_fill_exists` still returns `False`, and the next run's sweep re-publishes. The already-committed AC4 correction does not break that: the intent is now `SUBMITTED`, so `restore_absent_terminalization` raises `InvalidOrderTransition` (nothing to correct), `restored` is `None`, and `SUBMITTED` is in `_FILLABLE_STATUSES` — it publishes on the normal branch. Add a test for exactly this, since it is the one path that crosses a commit boundary:

```python
def test_a_corrected_intent_is_still_publishable_on_a_later_sweep(
    ledger, session
) -> None:
    """A publish that failed after the correction committed must retry."""
    intent = _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")
    ledger.transition(
        intent.recommendation_id,
        OrderStatus.EXPIRED,
        reason=ABSENT_AT_IB_REASON,
    )
    plan_sweep([_execution()], ledger)          # corrects, publish "fails"

    second = plan_sweep([_execution()], ledger)  # the next run

    assert second.corrected == ()                # nothing left to correct
    assert len(second.recovered) == 1            # still publishable
```

- [ ] **Step 5: Call it before reconciliation**

In `scripts/run_paper.py`, between the `broker_snapshot = asyncio.run(...)` block ending at line 1744 and `preparation = prepare_daily_run(` at line 1746:

```python
    # Recover fills the live callback never saw, BEFORE reconciliation reads
    # the book. A sell that filled at the open is otherwise invisible here,
    # and reconciliation can only see that a position moved, never what
    # executed — which is how PANW, LLY and UNH became phantoms.
    sweep_executions_best_effort(
        host=args.ib_host,
        port=args.ib_port,
        client_id=args.ib_client_id,
        session=session,
        redis_url=_config.redis.url,
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/scripts/test_run_paper_execution_sweep.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add services/execution/execution_sweep.py scripts/run_paper.py \
  tests/scripts/test_run_paper_execution_sweep.py
git commit -m "KAN-87: sweep IB executions once per run, before reconciliation"
```

---

### Task 5: Report the count in the daily digest

AC6: *"The count of recovered fills is reported in the daily digest, whether zero or not."*

**Files:**
- Modify: `scripts/ops/pipeline_report_summary.py:96-197` (`RunFacts`, `collect_facts`, `render_summary`)
- Test: `tests/deploy/test_pipeline_report.py`

**Interfaces:**
- Consumes: `ExecutionFill.recovery_source` (Task 1), `RECOVERY_SOURCE_SWEEP` (Task 3).
- Produces: `RunFacts.fills_recovered: int`.

- [ ] **Step 1: Write the failing test**

In `tests/deploy/test_pipeline_report.py`:

```python
def test_recovered_fills_are_counted_and_rendered(session) -> None:
    _seed_execution_fill(session, execution_id="a", recovery_source=None)
    _seed_execution_fill(
        session, execution_id="b", recovery_source="ib_execution_sweep"
    )

    facts = collect_facts(session, since=_since(), mode="paper")

    assert facts.fills == 2
    assert facts.fills_recovered == 1
    assert "1 recovered" in render_summary(facts)


def test_zero_recovered_is_still_reported(session) -> None:
    """Whether zero or not — a silent sweep hides a worsening problem."""
    _seed_execution_fill(session, execution_id="a", recovery_source=None)

    facts = collect_facts(session, since=_since(), mode="paper")

    assert facts.fills_recovered == 0
    assert "0 recovered" in render_summary(facts)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/deploy/test_pipeline_report.py -k recovered -v`
Expected: FAIL — `RunFacts` has no attribute `fills_recovered`.

- [ ] **Step 3: Add the field, the query, and the rendering**

Add `fills_recovered: int = 0` to `RunFacts`. In `collect_facts`, after the `fills` query:

```python
    # Counted the same way and over the same window as `fills`, so the two
    # are always comparable: "3 fills (1 recovered)" reads as a subset.
    fills_recovered = session.scalar(
        select(func.count())
        .select_from(ExecutionFill)
        .where(
            ExecutionFill.executed_at >= since,
            ExecutionFill.recovery_source == RECOVERY_SOURCE_SWEEP,
        )
    ) or 0
```

Pass `fills_recovered=int(fills_recovered)` into the returned `RunFacts`, and extend the fills clause in `render_summary` to read `fills {n} ({r} recovered)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/deploy/test_pipeline_report.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/ops/pipeline_report_summary.py tests/deploy/test_pipeline_report.py
git commit -m "KAN-87: report recovered fills in the daily digest, zero included"
```

---

### Task 6: Full suite, operator handover, PR

- [ ] **Step 1: Run the full suite**

Run: `pytest`
Expected: all green. Quote the actual summary line in the PR — `superpowers:verification-before-completion`, evidence before assertions.

- [ ] **Step 2: Rebase on develop**

```bash
git fetch origin develop && git rebase origin/develop
pytest
```

- [ ] **Step 3: Write the operator handover into the PR body**

The implementer does NOT run these. The PR body must state, in this order:

```
1. git pull in the tree the launchd jobs read (deploy/launchd/README.md names it)
2. alembic upgrade head          # adds execution_fills.recovery_source
3. docker compose up -d --force-recreate --no-deps portfolio-accounting
   (the projector writes the new column; --no-deps so redis is not recreated —
    it has no volume and would lose every stream)
4. Confirm: alembic current == head, before the next 04:15 run

Order matters. run_paper.sh compares the DB revision against the code's head
and aborts the run on a mismatch — a pulled-but-unapplied migration costs a
trading day (2026-08-26).
```

- [ ] **Step 4: Open the PR**

```bash
git push -u origin feat/kan-87-execution-sweep
gh pr create --base develop \
  --title "KAN-87: every daily sell fills after the close and nobody reads the execution back" \
  --body "<what/why/testing + operator handover + https://huiliang.atlassian.net/browse/KAN-87>"
```

---

## Out of scope (from the spec, do not drift into these)

- Moving the run before the close, or switching sells to LMT. Both change trading behaviour to fix a bookkeeping defect.
- KAN-85, the repair tool's inability to record a missed exit. This story reduces how often that tool is needed; it does not fix it.
- The three fills already lost. PANW and LLY remain unrecorded (−5,719.51 of book value); remediation is separate and harder, both positions being closed at quantity 0.
- Fractional intents that round to zero whole shares (17 since 2026-07-30). A sizing question, not a fill question.
