from __future__ import annotations

from datetime import datetime, timezone

from services.execution.reconciliation import PositionReconciler, build_repair_plan
from shared.broker_state import BrokerOpenOrder, BrokerPosition
from shared.models import ExecutionFill, OrderIntent, OrderStatus


NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)
ACCOUNT = "DUN551088"


def _position(quantity: float) -> BrokerPosition:
    return BrokerPosition(
        account_id=ACCOUNT,
        con_id=265598,
        symbol="AAPL",
        quantity=quantity,
        average_cost=100.0,
        exchange="SMART",
        currency="USD",
    )


def _intent(
    ib_order_id: str = "9",
    *,
    requested_quantity: float = 10.0,
    filled_quantity: float = 0.0,
) -> OrderIntent:
    return OrderIntent(
        recommendation_id=f"rec-{ib_order_id}",
        account_id=ACCOUNT,
        mode="paper",
        portfolio="momentum",
        con_id=265598,
        symbol="AAPL",
        exchange="SMART",
        currency="USD",
        action="BUY",
        requested_quantity=requested_quantity,
        limit_price=100.0,
        order_type="LMT",
        reserved_notional=(requested_quantity - filled_quantity) * 100.0,
        filled_quantity=filled_quantity,
        status=OrderStatus.SUBMITTED.value,
        ib_order_id=ib_order_id,
        created_at=NOW,
        updated_at=NOW,
    )


def _order(quantity: float = 10.0, *, filled_quantity: float = 0.0) -> BrokerOpenOrder:
    return BrokerOpenOrder(
        account_id=ACCOUNT,
        ib_order_id="9",
        con_id=265598,
        symbol="AAPL",
        action="BUY",
        total_quantity=quantity,
        filled_quantity=filled_quantity,
        status="Submitted",
    )


def _fill(quantity: float) -> ExecutionFill:
    return ExecutionFill(
        account_id=ACCOUNT,
        execution_id="exec-1",
        ib_order_id="9",
        recommendation_id="rec-9",
        portfolio="momentum",
        con_id=265598,
        symbol="AAPL",
        exchange="SMART",
        currency="USD",
        side="BUY",
        quantity=quantity,
        price=100.0,
        commission=1.0,
        executed_at=NOW,
        projection_applied=True,
    )


def test_matching_contract_keyed_state_allows_entries():
    result = PositionReconciler().reconcile(
        broker_positions={265598: _position(100)},
        db_positions={265598: 100.0},
        broker_orders={"9": _order()},
        db_orders={"9": _intent()},
    )

    assert result.entries_allowed is True
    assert result.severity == "ok"
    assert result.discrepancies == []


def test_any_quantity_mismatch_blocks_entries_without_auto_correction():
    result = PositionReconciler(quantity_tolerance=1e-6).reconcile(
        broker_positions={265598: 100.0},
        db_positions={265598: 100.01},
        broker_orders={},
        db_orders={},
    )

    assert result.entries_allowed is False
    assert result.severity == "major"
    assert result.discrepancies[0]["type"] == "quantity_mismatch"
    assert result.discrepancies[0]["con_id"] == 265598
    assert result.discrepancies[0]["auto_correct"] is False


def test_representation_difference_inside_tolerance_is_matched():
    result = PositionReconciler(quantity_tolerance=1e-6).reconcile(
        broker_positions={265598: 100.0},
        db_positions={265598: 100.0000005},
        broker_orders={},
        db_orders={},
    )

    assert result.entries_allowed is True


def test_open_order_missing_at_ib_blocks_entries():
    result = PositionReconciler().reconcile(
        broker_positions={},
        db_positions={},
        broker_orders={},
        db_orders={"9": _intent()},
    )

    assert result.entries_allowed is False
    assert result.discrepancies[0]["type"] == "order_missing_at_ib"


def test_ib_only_open_order_blocks_entries():
    result = PositionReconciler().reconcile(
        broker_positions={},
        db_positions={},
        broker_orders={"9": _order()},
        db_orders={},
    )

    assert result.entries_allowed is False
    assert result.discrepancies[0]["type"] == "order_missing_in_db"


def test_active_intent_filled_quantity_must_equal_execution_fill_sum():
    result = PositionReconciler().reconcile(
        broker_positions={},
        db_positions={},
        broker_orders={"9": _order(filled_quantity=3)},
        db_orders={"9": _intent(filled_quantity=3)},
        execution_fills=[_fill(2)],
    )

    assert result.entries_allowed is False
    assert any(
        item["type"] == "fill_quantity_mismatch"
        for item in result.discrepancies
    )


def test_unapplied_execution_fill_blocks_entries_even_without_active_intent():
    rejected_fill = _fill(2)
    rejected_fill.projection_applied = False
    result = PositionReconciler().reconcile(
        broker_positions={}, db_positions={}, broker_orders={}, db_orders={},
        execution_fills=[rejected_fill],
    )

    assert result.entries_allowed is False
    assert result.discrepancies[0]["type"] == "unapplied_execution_fill"


def test_position_keys_include_account_identity():
    other = BrokerPosition(
        account_id="DUOTHER",
        con_id=265598,
        symbol="AAPL",
        quantity=10,
    )
    result = PositionReconciler(account_id=ACCOUNT).reconcile(
        broker_positions={265598: other},
        db_positions={265598: 10},
        broker_orders={},
        db_orders={},
    )

    assert result.entries_allowed is False
    assert result.discrepancies[0]["type"] == "account_mismatch"


def test_ib_only_position_requires_explicit_sleeve_mapping():
    result = PositionReconciler(account_id=ACCOUNT).reconcile(
        broker_positions={265598: _position(10)},
        db_positions={},
        broker_orders={},
        db_orders={},
    )
    plan = build_repair_plan(result)

    assert plan.actions == []
    assert plan.unresolved[0].reason == "sleeve_mapping_required"
