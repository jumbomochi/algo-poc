"""Tests for the pure sweep decision (KAN-87 Task 3).

No IB, no Redis: every behaviour is driven through ``OrderLedger`` against
an in-memory sqlite session, following the pattern in
``test_broker_stop_verifier.py`` (a local ``session`` fixture, ``OrderLedger``
constructed inline in each test — there is no shared ``ledger`` fixture for
``tests/services/``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.execution.execution_sweep import (
    RECOVERY_SOURCE_SWEEP,
    SweptExecution,
    plan_sweep,
)
from shared.models import Base, ExecutionFill, OrderIntent, OrderStatus
from shared.order_ledger import ABSENT_AT_IB_REASON, OrderLedger


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _proposal(
    recommendation_id: str,
    *,
    account_id: str = "DUN551088",
    portfolio: str = "momentum",
    con_id: int = 756733,
    symbol: str = "UNH",
    quantity: float = 6.0,
    action: str = "SELL",
) -> SimpleNamespace:
    return SimpleNamespace(
        recommendation_id=recommendation_id,
        account_id=account_id,
        mode="paper",
        portfolio=portfolio,
        con_id=con_id,
        symbol=symbol,
        exchange="SMART",
        currency="USD",
        action=action,
        quantity=quantity,
        limit_price=340.0,
        order_type="LMT",
    )


def _submitted_intent(
    session: Session,
    ledger: OrderLedger,
    recommendation_id: str,
    *,
    ib_order_id: str,
    account_id: str = "DUN551088",
    portfolio: str = "momentum",
    quantity: float = 6.0,
) -> OrderIntent:
    """Drive a fresh intent to SUBMITTED via the real ledger transitions."""
    ledger.create_intent(
        _proposal(
            recommendation_id,
            account_id=account_id,
            portfolio=portfolio,
            quantity=quantity,
        )
    )
    ledger.transition(recommendation_id, OrderStatus.APPROVED)
    intent = ledger.record_submission(recommendation_id, ib_order_id)
    session.flush()
    return intent


def _record_execution_fill(
    session: Session,
    *,
    account_id: str,
    execution_id: str,
    ib_order_id: str = "148",
) -> ExecutionFill:
    fill = ExecutionFill(
        account_id=account_id,
        execution_id=execution_id,
        ib_order_id=ib_order_id,
        recommendation_id="rec-unh",
        portfolio="momentum",
        con_id=756733,
        symbol="UNH",
        exchange="SMART",
        currency="USD",
        side="SELL",
        quantity=6.0,
        price=341.22,
        commission=1.0,
        executed_at=datetime(2026, 9, 14, 13, 31, tzinfo=timezone.utc),
        projection_applied=True,
    )
    session.add(fill)
    session.flush()
    return fill


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


def test_an_unrecorded_execution_is_published(session) -> None:
    """AC1: the fill the callback missed reaches stream:fills."""
    ledger = OrderLedger(session)
    intent = _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")

    outcome = plan_sweep([_execution()], ledger)

    assert len(outcome.recovered) == 1
    fill = outcome.recovered[0]
    assert fill.execution_id == "exec-1"
    assert fill.recommendation_id == intent.recommendation_id
    assert fill.portfolio == intent.portfolio
    assert fill.recovery_source == RECOVERY_SOURCE_SWEEP
    assert outcome.already_recorded == 0


def test_an_already_recorded_execution_is_not_republished(session) -> None:
    """AC2: no double-count."""
    ledger = OrderLedger(session)
    _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")
    _record_execution_fill(session, account_id="DUN551088", execution_id="exec-1")

    outcome = plan_sweep([_execution()], ledger)

    assert outcome.recovered == ()
    assert outcome.already_recorded == 1


def test_a_double_sweep_does_not_double_count(session) -> None:
    """AC2: running the same plan twice over a recorded fill stays empty."""
    ledger = OrderLedger(session)
    _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")
    _record_execution_fill(session, account_id="DUN551088", execution_id="exec-1")

    first = plan_sweep([_execution()], ledger)
    second = plan_sweep([_execution()], ledger)

    assert first.recovered == () and second.recovered == ()
    assert second.already_recorded == 1


def test_an_untracked_order_is_reported_never_projected(session) -> None:
    """AC3: someone else's order is named, not booked."""
    ledger = OrderLedger(session)

    outcome = plan_sweep([_execution(ib_order_id="999")], ledger)

    assert outcome.recovered == ()
    assert outcome.untracked == ("999",)


def test_an_absence_expired_intent_is_corrected(session) -> None:
    """AC4: EXPIRED-on-absence becomes fillable again, and is reported."""
    ledger = OrderLedger(session)
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


def test_a_genuinely_expired_intent_is_left_alone(session) -> None:
    """The correction is evidence-gated, not a blanket un-expire."""
    ledger = OrderLedger(session)
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


def test_a_corrected_intent_is_still_publishable_on_a_later_sweep(session) -> None:
    """A publish that failed after the correction committed must retry.

    ``plan_sweep`` never writes ``execution_fills`` itself — the projector
    does, on consuming ``stream:fills``. So if Task 4's publish step fails
    after this call returns, the AC4 correction (EXPIRED -> SUBMITTED) is
    still committed but no fill is recorded. The next run's sweep must not
    treat that as "nothing left to do": ``execution_fill_exists`` is still
    False, so it re-decides the same execution, finds the intent already
    SUBMITTED (nothing left to correct -- ``restore_absent_terminalization``
    raises ``InvalidOrderTransition``), and republishes on the normal
    ``_FILLABLE_STATUSES`` branch.
    """
    ledger = OrderLedger(session)
    intent = _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")
    ledger.transition(
        intent.recommendation_id,
        OrderStatus.EXPIRED,
        reason=ABSENT_AT_IB_REASON,
    )

    first = plan_sweep([_execution()], ledger)  # corrects, publish "fails"

    assert first.corrected == (intent.recommendation_id,)
    assert len(first.recovered) == 1

    second = plan_sweep([_execution()], ledger)  # the next run

    assert second.corrected == ()  # nothing left to correct
    assert len(second.recovered) == 1  # still publishable
