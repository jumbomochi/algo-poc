from __future__ import annotations

import math
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.risk_management.funding import (
    check_settled_usd_funding,
    estimate_commission_usd,
)
from shared.models import Base, OrderStatus
from shared.order_ledger import OrderLedger


def test_buy_is_rejected_when_reservations_and_buffer_exceed_cash():
    decision = check_settled_usd_funding(
        order_notional_usd=900,
        settled_cash_usd=1_000,
        active_reservations_usd=50,
        estimated_commission_usd=1,
        minimum_reserve_usd=100,
    )

    assert decision.approved is False
    assert decision.required_usd == pytest.approx(1_051)
    assert decision.remaining_usd == pytest.approx(-51)
    assert "settled USD cash" in decision.reason


def test_cash_gate_ignores_margin_and_scales_nothing():
    decision = check_settled_usd_funding(
        order_notional_usd=800,
        settled_cash_usd=1_000,
        active_reservations_usd=0,
        estimated_commission_usd=1,
        minimum_reserve_usd=100,
    )

    assert decision.approved is True
    assert decision.required_usd == pytest.approx(901)
    assert decision.remaining_usd == pytest.approx(99)


def test_commission_estimate_uses_configured_minimum():
    assert estimate_commission_usd(10, per_share=0.005, minimum=1) == pytest.approx(1)
    assert estimate_commission_usd(1_000, per_share=0.005, minimum=1) == pytest.approx(5)


@pytest.mark.parametrize("invalid_cash", [None, math.nan, math.inf, -math.inf])
def test_invalid_settled_cash_fails_closed(invalid_cash):
    decision = check_settled_usd_funding(
        order_notional_usd=100,
        settled_cash_usd=invalid_cash,
        active_reservations_usd=0,
        estimated_commission_usd=1,
        minimum_reserve_usd=0,
    )

    assert decision.approved is False
    assert "invalid settled USD cash" in decision.reason


@pytest.mark.parametrize(
    "field",
    [
        "order_notional_usd",
        "active_reservations_usd",
        "estimated_commission_usd",
        "minimum_reserve_usd",
    ],
)
def test_invalid_funding_requirement_fails_closed(field):
    values = {
        "order_notional_usd": 100,
        "settled_cash_usd": 1_000,
        "active_reservations_usd": 0,
        "estimated_commission_usd": 1,
        "minimum_reserve_usd": 0,
    }
    values[field] = math.nan

    decision = check_settled_usd_funding(**values)

    assert decision.approved is False
    assert "invalid USD funding data" in decision.reason


def _proposal(
    recommendation_id: str,
    *,
    account_id: str = "DUONE",
    portfolio: str = "momentum",
    quantity: float = 10,
    price: float = 100,
):
    return SimpleNamespace(
        recommendation_id=recommendation_id,
        account_id=account_id,
        mode="paper",
        portfolio=portfolio,
        con_id=1,
        symbol="AAPL",
        exchange="SMART",
        currency="USD",
        action="BUY",
        quantity=quantity,
        limit_price=price,
        order_type="LMT",
    )


def test_account_buy_reservations_include_every_sleeve_and_published_proposals():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ledger = OrderLedger(session)
        ledger.create_intent(_proposal("momentum", portfolio="momentum"))
        ledger.transition("momentum", OrderStatus.APPROVED)
        proposed = ledger.create_intent(
            _proposal("quality", portfolio="quality_value", quantity=4, price=250)
        )
        ledger.mark_published(proposed.recommendation_id)
        ledger.create_intent(
            _proposal("other-account", account_id="DUTWO", quantity=50, price=1_000)
        )
        ledger.transition("other-account", OrderStatus.APPROVED)

        assert ledger.active_buy_reservations_for_account("DUONE") == pytest.approx(
            2_000
        )


def test_account_buy_reservations_use_remaining_quantity_and_can_exclude_current():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ledger = OrderLedger(session)
        current = ledger.create_intent(_proposal("current", quantity=10, price=100))
        ledger.mark_published(current.recommendation_id)
        ledger.create_intent(_proposal("partial", quantity=10, price=200))
        ledger.transition("partial", OrderStatus.APPROVED)
        ledger.transition("partial", OrderStatus.SUBMITTED)
        ledger.transition("partial", OrderStatus.PARTIALLY_FILLED)
        ledger.get("partial").filled_quantity = 4
        session.flush()

        assert ledger.active_buy_reservations_for_account(
            "DUONE", exclude_recommendation_id="current"
        ) == pytest.approx(1_200)
