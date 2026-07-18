from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from shared.models import Base, OrderIntent, OrderStatus
from shared.order_ledger import (
    ConflictingOrderIntent,
    InvalidOrderTransition,
    OrderLedger,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def make_proposal(
    recommendation_id: str,
    *,
    quantity: float = 5,
    price: float | None = 100,
    action: str = "BUY",
):
    return SimpleNamespace(
        recommendation_id=recommendation_id,
        account_id="DU12345",
        mode="paper",
        portfolio="momentum",
        con_id=265598,
        symbol="AAPL",
        exchange="SMART",
        currency="USD",
        action=action,
        quantity=quantity,
        limit_price=price,
        order_type="LMT",
    )


def test_terminal_intent_cannot_transition(session):
    ledger = OrderLedger(session)
    ledger.create_intent(make_proposal("rec-1"))
    ledger.transition("rec-1", OrderStatus.RISK_REJECTED, reason="sector")
    with pytest.raises(InvalidOrderTransition):
        ledger.transition("rec-1", OrderStatus.APPROVED)


def test_active_buy_reservation_uses_unfilled_notional(session):
    ledger = OrderLedger(session)
    ledger.create_intent(make_proposal("rec-1", quantity=10, price=100))
    ledger.transition("rec-1", OrderStatus.APPROVED)
    intent = ledger.get("rec-1")
    intent.filled_quantity = 4
    session.flush()
    assert ledger.active_reservations("momentum") == pytest.approx(600.0)


def test_create_intent_is_idempotent(session):
    ledger = OrderLedger(session)
    first = ledger.create_intent(make_proposal("rec-1"))
    second = ledger.create_intent(make_proposal("rec-1"))
    assert first.id == second.id


def test_create_intent_rejects_conflicting_recommendation_reuse(session):
    ledger = OrderLedger(session)
    ledger.create_intent(make_proposal("rec-1", quantity=5))

    with pytest.raises(ConflictingOrderIntent):
        ledger.create_intent(make_proposal("rec-1", quantity=6))


def test_create_intent_recovers_matching_concurrent_insert(session, monkeypatch):
    first = OrderLedger(session).create_intent(make_proposal("rec-1"))
    ledger = OrderLedger(session)
    original_locked = ledger._locked
    calls = 0

    def miss_once(recommendation_id, *, required):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return original_locked(recommendation_id, required=required)

    monkeypatch.setattr(ledger, "_locked", miss_once)
    replay = ledger.create_intent(make_proposal("rec-1"))

    assert replay.id == first.id
    assert session.in_transaction()


def test_create_intent_is_rolled_back_by_caller(session):
    ledger = OrderLedger(session)
    ledger.create_intent(make_proposal("rec-1"))

    session.rollback()

    assert session.scalar(select(OrderIntent).where(
        OrderIntent.recommendation_id == "rec-1"
    )) is None


def test_submission_pending_load_and_publication_lifecycle(session):
    ledger = OrderLedger(session)
    intent = ledger.create_intent(make_proposal("rec-1"))
    ledger.mark_published("rec-1")
    ledger.transition("rec-1", OrderStatus.APPROVED)
    ledger.record_submission("rec-1", "42")

    assert intent.published_at is not None
    assert intent.ib_order_id == "42"
    assert intent.status == OrderStatus.SUBMITTED.value
    assert ledger.load_pending_orders() == [intent]


def test_repository_flushes_without_committing(session, monkeypatch):
    ledger = OrderLedger(session)
    monkeypatch.setattr(session, "commit", lambda: pytest.fail("unexpected commit"))

    ledger.create_intent(make_proposal("rec-1"))
    ledger.transition("rec-1", OrderStatus.APPROVED)
    ledger.mark_published("rec-1")
    ledger.record_submission("rec-1", "42")


def test_transition_sets_lifecycle_timestamps(session):
    ledger = OrderLedger(session)
    intent = ledger.create_intent(make_proposal("rec-1"))
    ledger.transition("rec-1", OrderStatus.APPROVED)
    assert intent.approved_at is not None

    ledger.record_submission("rec-1", "42")
    assert intent.submitted_at is not None

    ledger.transition("rec-1", OrderStatus.FILLED)
    assert intent.terminal_at is not None
    assert intent.updated_at >= intent.created_at


def test_mark_published_accepts_explicit_timestamp(session):
    ledger = OrderLedger(session)
    intent = ledger.create_intent(make_proposal("rec-1"))
    published_at = datetime(2026, 7, 19, tzinfo=timezone.utc)

    ledger.mark_published("rec-1", published_at=published_at)

    assert intent.published_at == published_at
