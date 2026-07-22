from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Mapping

from shared.broker_state import BrokerOpenOrder, BrokerPosition
from shared.models import ExecutionFill, OrderIntent


@dataclass(frozen=True)
class RepairAction:
    action: str
    portfolio: str
    con_id: int
    quantity: float


@dataclass(frozen=True)
class UnresolvedRepair:
    reason: str
    con_id: int | None = None
    ib_order_id: str | None = None


@dataclass(frozen=True)
class RepairPlan:
    account_id: str
    created_at: datetime
    actions: list[RepairAction] = field(default_factory=list)
    unresolved: list[UnresolvedRepair | dict[str, Any]] = field(
        default_factory=list
    )

    def to_dict(self) -> dict[str, Any]:
        def encode(value: Any) -> Any:
            if hasattr(value, "__dataclass_fields__"):
                return asdict(value)
            return value

        return {
            "account_id": self.account_id,
            "created_at": self.created_at.isoformat(),
            "actions": [encode(action) for action in self.actions],
            "unresolved": [encode(item) for item in self.unresolved],
        }


@dataclass
class ReconciliationResult:
    """Fail-closed comparison of broker state and the durable database ledger."""

    matched: list[dict[str, Any]]
    discrepancies: list[dict[str, Any]]
    severity: Literal["ok", "major"]
    account_id: str = ""

    @property
    def entries_allowed(self) -> bool:
        return self.severity == "ok" and not self.discrepancies

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "severity": self.severity,
            "entries_allowed": self.entries_allowed,
            "matched": self.matched,
            "discrepancies": self.discrepancies,
        }


class PositionReconciler:
    """Compare broker and ledger state by durable account/contract keys.

    Tolerance exists only for floating-point representation. Every mismatch
    outside it blocks new entries and is report-only; reconciliation never
    mutates or auto-corrects portfolio state.
    """

    def __init__(
        self, *, quantity_tolerance: float = 1e-6, account_id: str = ""
    ) -> None:
        if quantity_tolerance < 0:
            raise ValueError("quantity_tolerance must be non-negative")
        self.quantity_tolerance = quantity_tolerance
        self.account_id = account_id

    def reconcile(
        self,
        broker_positions: Mapping[int, BrokerPosition | float | int],
        db_positions: Mapping[int, Any],
        broker_orders: Mapping[str, BrokerOpenOrder | Any] | None = None,
        db_orders: Mapping[str, OrderIntent | Any] | None = None,
        execution_fills: Iterable[ExecutionFill | Any] = (),
        active_intents: Iterable[OrderIntent | Any] | None = None,
    ) -> ReconciliationResult:
        matched: list[dict[str, Any]] = []
        discrepancies: list[dict[str, Any]] = []
        broker_orders = broker_orders or {}
        db_orders = db_orders or {}

        for con_id in sorted(set(broker_positions) | set(db_positions)):
            broker_value = broker_positions.get(con_id)
            db_value = db_positions.get(con_id)
            if isinstance(broker_value, BrokerPosition):
                if self.account_id and broker_value.account_id != self.account_id:
                    discrepancies.append({
                        "type": "account_mismatch",
                        "con_id": con_id,
                        "expected_account_id": self.account_id,
                        "actual_account_id": broker_value.account_id,
                        "auto_correct": False,
                    })
                    continue
                broker_qty = broker_value.quantity
                symbol = broker_value.symbol
            else:
                broker_qty = broker_value
                symbol = getattr(db_value, "ticker", None)
            db_qty = self._quantity(db_value)
            portfolio = getattr(db_value, "portfolio", None)

            if broker_value is None:
                discrepancies.append({
                    "type": "missing_in_ib", "con_id": con_id,
                    "symbol": symbol, "ib_quantity": None,
                    "db_quantity": db_qty, "portfolio": portfolio,
                    "auto_correct": False,
                })
            elif db_value is None:
                discrepancies.append({
                    "type": "missing_in_db", "con_id": con_id,
                    "symbol": symbol, "ib_quantity": float(broker_qty),
                    "db_quantity": None, "auto_correct": False,
                })
            elif abs(float(broker_qty) - db_qty) > self.quantity_tolerance:
                discrepancies.append({
                    "type": "quantity_mismatch", "con_id": con_id,
                    "symbol": symbol, "ib_quantity": float(broker_qty),
                    "db_quantity": db_qty, "portfolio": portfolio,
                    "auto_correct": False,
                })
            else:
                matched.append({
                    "type": "position", "con_id": con_id,
                    "quantity": float(broker_qty),
                })

        for order_id in sorted(set(broker_orders) | set(db_orders)):
            broker_order = broker_orders.get(order_id)
            db_order = db_orders.get(order_id)
            if broker_order is None:
                discrepancies.append({
                    "type": "order_missing_at_ib", "ib_order_id": order_id,
                    "recommendation_id": getattr(db_order, "recommendation_id", None),
                    "auto_correct": False,
                })
                continue
            if db_order is None:
                discrepancies.append({
                    "type": "order_missing_in_db", "ib_order_id": order_id,
                    "con_id": getattr(broker_order, "con_id", None),
                    "auto_correct": False,
                })
                continue
            self._compare_order(order_id, broker_order, db_order, discrepancies)
            if not any(
                item.get("ib_order_id") == order_id
                for item in discrepancies
            ):
                matched.append({"type": "open_order", "ib_order_id": order_id})

        fill_totals: dict[str, float] = {}
        for fill in execution_fills:
            recommendation_id = getattr(fill, "recommendation_id", None)
            if not bool(getattr(fill, "projection_applied", True)):
                discrepancies.append({
                    "type": "unapplied_execution_fill",
                    "execution_id": getattr(fill, "execution_id", None),
                    "recommendation_id": recommendation_id,
                    "ib_order_id": str(getattr(fill, "ib_order_id", "")),
                    "auto_correct": False,
                })
            if recommendation_id:
                fill_totals[recommendation_id] = (
                    fill_totals.get(recommendation_id, 0.0)
                    + float(fill.quantity)
                )
        intents_to_check = (
            list(active_intents)
            if active_intents is not None
            else list(db_orders.values())
        )
        for intent in intents_to_check:
            recommendation_id = getattr(intent, "recommendation_id", None)
            if recommendation_id is None:
                continue
            ledger_qty = float(getattr(intent, "filled_quantity", 0.0))
            audit_qty = fill_totals.get(recommendation_id, 0.0)
            if abs(ledger_qty - audit_qty) > self.quantity_tolerance:
                discrepancies.append({
                    "type": "fill_quantity_mismatch",
                    "recommendation_id": recommendation_id,
                    "ib_order_id": str(getattr(intent, "ib_order_id", "")),
                    "intent_filled_quantity": ledger_qty,
                    "execution_fill_quantity": audit_qty,
                    "auto_correct": False,
                })

        return ReconciliationResult(
            matched=matched,
            discrepancies=discrepancies,
            severity="major" if discrepancies else "ok",
            account_id=self.account_id,
        )

    @staticmethod
    def _quantity(value: Any) -> float:
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        return float(value.quantity)

    def _compare_order(
        self,
        order_id: str,
        broker_order: BrokerOpenOrder | Any,
        db_order: OrderIntent | Any,
        discrepancies: list[dict[str, Any]],
    ) -> None:
        broker_account = getattr(broker_order, "account_id", self.account_id)
        db_account = getattr(db_order, "account_id", self.account_id)
        broker_remaining = float(getattr(
            broker_order, "remaining_quantity",
            float(broker_order.total_quantity) - float(broker_order.filled_quantity),
        ))
        db_remaining = float(db_order.requested_quantity) - float(db_order.filled_quantity)
        fields = {
            "account_id": (broker_account, db_account),
            "con_id": (int(broker_order.con_id), int(db_order.con_id)),
            "action": (str(broker_order.action).upper(), str(db_order.action).upper()),
        }
        unequal = [name for name, pair in fields.items() if pair[0] != pair[1]]
        if abs(broker_remaining - db_remaining) > self.quantity_tolerance:
            unequal.append("remaining_quantity")
        if unequal:
            discrepancies.append({
                "type": "open_order_mismatch",
                "ib_order_id": order_id,
                "fields": unequal,
                "broker_remaining_quantity": broker_remaining,
                "db_remaining_quantity": db_remaining,
                "auto_correct": False,
            })


def build_repair_plan(result: ReconciliationResult) -> RepairPlan:
    """Construct a reviewable plan without guessing sleeve attribution."""
    actions: list[RepairAction] = []
    unresolved: list[UnresolvedRepair] = []
    for discrepancy in result.discrepancies:
        kind = discrepancy["type"]
        con_id = discrepancy.get("con_id")
        portfolio = discrepancy.get("portfolio")
        if kind == "missing_in_db":
            unresolved.append(UnresolvedRepair(
                reason="sleeve_mapping_required", con_id=con_id
            ))
        elif kind in {"missing_in_ib", "quantity_mismatch"}:
            if not portfolio:
                unresolved.append(UnresolvedRepair(
                    reason="sleeve_mapping_required", con_id=con_id
                ))
            else:
                actions.append(RepairAction(
                    action="set_position_quantity",
                    portfolio=portfolio,
                    con_id=int(con_id),
                    quantity=float(discrepancy.get("ib_quantity") or 0.0),
                ))
        else:
            unresolved.append(UnresolvedRepair(
                reason="manual_order_or_fill_resolution_required",
                con_id=con_id,
                ib_order_id=discrepancy.get("ib_order_id"),
            ))
    return RepairPlan(
        account_id=result.account_id,
        created_at=datetime.now(timezone.utc),
        actions=actions,
        unresolved=unresolved,
    )
