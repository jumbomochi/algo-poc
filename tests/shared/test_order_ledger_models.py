from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.models import (
    Base,
    CapitalAdjustment,
    CapitalSnapshot,
    ExecutionFill,
    OrderIntent,
    OrderStatus,
    Position,
    ReconciliationReport,
)


NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def make_intent(recommendation_id: str, *, status: str = "PROPOSED") -> OrderIntent:
    return OrderIntent(
        recommendation_id=recommendation_id,
        account_id="DU12345",
        mode="paper",
        portfolio="quality_value",
        con_id=12345,
        symbol="BRK B",
        exchange="SMART",
        currency="USD",
        action="BUY",
        requested_quantity=1.0,
        limit_price=500.0,
        order_type="LMT",
        reserved_notional=500.0,
        filled_quantity=0.0,
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


def make_fill(execution_id: str) -> ExecutionFill:
    return ExecutionFill(
        account_id="DU12345",
        execution_id=execution_id,
        ib_order_id=42,
        recommendation_id="rec-1",
        portfolio="quality_value",
        con_id=12345,
        symbol="BRK B",
        exchange="SMART",
        currency="USD",
        side="BUY",
        quantity=1.0,
        price=500.0,
        commission=1.0,
        executed_at=NOW,
    )


def test_order_status_has_exact_design_states():
    assert {status.value for status in OrderStatus} == {
        "PROPOSED",
        "RISK_REJECTED",
        "APPROVED",
        "SUBMISSION_FAILED",
        "SUBMITTED",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELLED",
        "EXPIRED",
    }


def test_execution_id_is_unique(session):
    session.add_all([make_fill("exec-1"), make_fill("exec-1")])
    with pytest.raises(IntegrityError):
        session.commit()


def test_recommendation_id_is_unique(session):
    session.add_all([make_intent("rec-1"), make_intent("rec-1")])
    with pytest.raises(IntegrityError):
        session.commit()


def test_invalid_order_status_is_rejected_by_database(session):
    session.add(make_intent("rec-1", status="NOT_A_STATUS"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_position_accepts_broker_contract_identity(session):
    position = Position(
        account_id="DU12345",
        ticker="BRK B",
        portfolio="quality_value",
        quantity=1.0,
        avg_entry_price=500.0,
        current_price=500.0,
        peak_price=500.0,
        highest_price_since_entry=500.0,
        opened_at=NOW,
        status="open",
        con_id=12345,
        exchange="SMART",
        currency="USD",
    )
    session.add(position)
    session.commit()
    assert (position.con_id, position.exchange, position.currency) == (
        12345,
        "SMART",
        "USD",
    )
    assert position.account_id == "DU12345"


def test_position_broker_contract_identity_is_nullable(session):
    position = Position(
        ticker="AAPL",
        portfolio="momentum",
        quantity=1.0,
        avg_entry_price=200.0,
        current_price=200.0,
        peak_price=200.0,
        highest_price_since_entry=200.0,
        opened_at=NOW,
        status="open",
    )
    session.add(position)
    session.commit()
    assert (position.con_id, position.exchange, position.currency) == (None, None, None)


def test_ledger_tables_are_registered_on_shared_metadata():
    assert {
        "order_intents",
        "execution_fills",
        "capital_snapshots",
        "capital_adjustments",
        "reconciliation_reports",
    } <= set(Base.metadata.tables)


@pytest.mark.parametrize(
    "model",
    [
        OrderIntent,
        ExecutionFill,
        CapitalSnapshot,
        CapitalAdjustment,
        ReconciliationReport,
    ],
)
def test_ledger_models_share_base(model):
    assert issubclass(model, Base)
