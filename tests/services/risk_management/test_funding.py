from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.risk_management.funding import (
    check_settled_usd_funding,
    estimate_commission_usd,
    size_to_sleeve_cash,
    sleeve_buy_cost_usd,
)
from shared.models import Base, ExecutionFill, OrderStatus
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

        assert ledger.active_buy_reservations_for_account(
            "DUONE", commission_per_share=0, minimum_commission=0
        ) == pytest.approx(2_000)


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
            "DUONE",
            exclude_recommendation_id="current",
            commission_per_share=0,
            minimum_commission=0,
        ) == pytest.approx(1_200)


def test_active_buy_reservations_include_conservative_commission_per_order():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ledger = OrderLedger(session)
        for recommendation_id in ("one", "two"):
            ledger.create_intent(
                _proposal(recommendation_id, quantity=10, price=100)
            )
            ledger.transition(recommendation_id, OrderStatus.APPROVED)

        assert ledger.active_buy_reservations_for_account(
            "DUONE",
            commission_per_share=0.005,
            minimum_commission=1,
        ) == pytest.approx(2_002)


def test_active_buy_reservations_require_commission_inputs():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ledger = OrderLedger(session)
        ledger.create_intent(_proposal("one", quantity=10, price=100))
        ledger.transition("one", OrderStatus.APPROVED)

        with pytest.raises(TypeError):
            ledger.active_buy_reservations_for_account("DUONE")


def _execution_fill(
    *,
    executed_at: datetime,
    account_id: str = "DUONE",
    quantity: float = 9,
    price: float = 100,
    commission: float = 1,
    commission_currency: str | None = "USD",
    commission_trading: float | None = None,
) -> ExecutionFill:
    return ExecutionFill(
        account_id=account_id,
        execution_id=f"exec-{account_id}-{executed_at.timestamp()}",
        ib_order_id="42",
        recommendation_id="filled-order",
        portfolio="momentum",
        con_id=1,
        symbol="AAPL",
        exchange="SMART",
        currency="USD",
        side="BUY",
        quantity=quantity,
        price=price,
        commission=commission,
        commission_currency=commission_currency,
        commission_trading=commission_trading,
        cumulative_quantity=quantity,
        executed_at=executed_at,
    )


def test_buy_fill_spend_after_snapshot_remains_committed():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        snapshot_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        session.add(_execution_fill(executed_at=datetime.now(timezone.utc)))
        session.flush()

        assert OrderLedger(session).buy_fill_spend_for_account_since(
            "DUONE", captured_after=snapshot_at
        ) == pytest.approx(901)


def test_newer_snapshot_supersedes_fill_spend_and_other_accounts_are_isolated():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        now = datetime.now(timezone.utc)
        session.add_all(
            [
                _execution_fill(executed_at=now - timedelta(minutes=2)),
                _execution_fill(
                    executed_at=now,
                    account_id="DUTWO",
                    quantity=50,
                    price=1_000,
                ),
            ]
        )
        session.flush()

        assert OrderLedger(session).buy_fill_spend_for_account_since(
            "DUONE", captured_after=now - timedelta(minutes=1)
        ) == 0


def test_non_usd_commission_without_trading_value_fails_closed():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        snapshot_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        session.add(
            _execution_fill(
                executed_at=datetime.now(timezone.utc),
                commission_currency="SGD",
                commission_trading=None,
            )
        )
        session.flush()

        with pytest.raises(ValueError, match="commission"):
            OrderLedger(session).buy_fill_spend_for_account_since(
                "DUONE", captured_after=snapshot_at
            )


# --------------------------------------------------------------- KAN-111


def _size(quantity, price, cash, committed=0.0, *, whole_shares=True, buffer_bps=25.0):
    return size_to_sleeve_cash(
        quantity=quantity,
        price=price,
        sleeve_cash_usd=cash,
        committed_usd=committed,
        per_share=0.005,
        minimum=1.0,
        buffer_bps=buffer_bps,
        whole_shares=whole_shares,
    )


def _cost(quantity, price, buffer_bps=25.0):
    return sleeve_buy_cost_usd(
        quantity, price, per_share=0.005, minimum=1.0, buffer_bps=buffer_bps
    )


def test_sleeve_cash_buy_that_fits_is_returned_unchanged():
    decision = _size(11.1691, 167.04, 5_000.0)

    assert decision.approved and not decision.downsized
    assert decision.quantity == 11.1691
    assert decision.required_usd == pytest.approx(11.1691 * 167.04 * 1.0025 + 1.0)


def test_sleeve_cash_below_one_share_skips_with_have_and_need():
    decision = _size(11.1691, 167.04, 24.83)

    assert not decision.approved
    assert decision.quantity == 0.0
    assert decision.reason == (
        "insufficient sleeve cash: have $24.83, need $1,871.35; "
        "1 share needs $168.46"
    )


def test_sleeve_cash_commitments_come_off_the_top():
    assert _size(11.1691, 167.04, 2_000.0, committed=1_000.0).quantity == 5.0
    assert not _size(1.0, 167.04, 2_000.0, committed=1_900.0).approved


@pytest.mark.parametrize("whole_shares", [True, False])
@pytest.mark.parametrize("cash", [168.46, 500.0, 1_000.0, 1_871.0, 1_871.35])
def test_sleeve_cash_downsizes_to_the_largest_order_that_fits(whole_shares, cash):
    decision = _size(11.1691, 167.04, cash, whole_shares=whole_shares)
    step = 1.0 if whole_shares else 0.0001

    assert decision.approved and decision.downsized
    assert _cost(decision.quantity, 167.04) <= cash
    assert _cost(decision.quantity + step, 167.04) > cash
    if whole_shares:
        assert decision.quantity.is_integer()


def test_sleeve_cash_per_share_commission_counts_on_a_large_order():
    # 2,000 shares at $1: commission is per-share ($10), not the $1 floor.
    decision = _size(5_000.0, 1.0, 2_010.0, buffer_bps=0.0)

    assert decision.quantity == 2_000.0
    assert _cost(2_001.0, 1.0, buffer_bps=0.0) > 2_010.0


@pytest.mark.parametrize(
    "cash, committed, price",
    [(None, 0.0, 10.0), (float("nan"), 0.0, 10.0), (100.0, float("nan"), 10.0),
     (100.0, 0.0, 0.0), ("x", 0.0, 10.0)],
)
def test_sleeve_cash_unusable_inputs_fail_closed(cash, committed, price):
    decision = _size(1.0, price, cash, committed)

    assert not decision.approved
    assert decision.reason == "sleeve cash or order data unusable"


def test_sleeve_cash_overdrawn_sleeve_skips():
    assert not _size(1.0, 10.0, -5.0).approved
    assert not _size(1.0, 10.0, 100.0, committed=150.0).approved
