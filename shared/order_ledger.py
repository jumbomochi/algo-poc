from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.models import OrderIntent, OrderStatus


class OrderLedgerError(RuntimeError):
    """Base error for durable order ledger operations."""


class OrderIntentNotFound(OrderLedgerError):
    """Raised when a recommendation has no durable order intent."""


class InvalidOrderTransition(OrderLedgerError):
    """Raised when an order lifecycle transition is not monotonic."""


class ConflictingOrderIntent(OrderLedgerError):
    """Raised when a recommendation ID is reused for a different order."""


ALLOWED_TRANSITIONS = {
    OrderStatus.PROPOSED: {OrderStatus.RISK_REJECTED, OrderStatus.APPROVED},
    OrderStatus.APPROVED: {
        OrderStatus.SUBMISSION_FAILED,
        OrderStatus.SUBMITTED,
    },
    OrderStatus.SUBMITTED: {
        OrderStatus.SUBMISSION_FAILED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    },
}

ACTIVE_RESERVATION_STATUSES = (
    OrderStatus.APPROVED.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
)
PENDING_ORDER_STATUSES = ACTIVE_RESERVATION_STATUSES
TERMINAL_STATUSES = {
    OrderStatus.RISK_REJECTED,
    OrderStatus.SUBMISSION_FAILED,
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.EXPIRED,
}

IMMUTABLE_ECONOMIC_FIELDS = (
    "account_id",
    "mode",
    "portfolio",
    "con_id",
    "symbol",
    "exchange",
    "currency",
    "action",
    "requested_quantity",
    "limit_price",
    "order_type",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrderLedger:
    """Transaction-neutral repository for the durable order lifecycle."""

    def __init__(self, session: Session):
        self.session = session

    def create_intent(self, proposal: Any) -> OrderIntent:
        values = self._proposal_values(proposal)
        recommendation_id = proposal.recommendation_id
        existing = self._locked(recommendation_id, required=False)
        if existing is not None:
            self._ensure_same_economics(existing, values)
            return existing

        now = _utcnow()
        intent = OrderIntent(
            recommendation_id=recommendation_id,
            **values,
            reserved_notional=(
                values["requested_quantity"] * values["limit_price"]
                if values["action"].upper() == "BUY"
                and values["limit_price"] is not None
                else 0.0
            ),
            filled_quantity=0.0,
            status=OrderStatus.PROPOSED.value,
            created_at=now,
            updated_at=now,
        )
        try:
            self._ensure_sqlite_transaction()
            with self.session.begin_nested():
                self.session.add(intent)
                self.session.flush()
            return intent
        except IntegrityError:
            # A missing row cannot be locked. A concurrent creator may win the
            # unique-key race after our initial SELECT, so isolate the losing
            # INSERT in a savepoint and compare the committed winner without
            # rolling back the caller's encompassing transaction.
            existing = self._locked(recommendation_id, required=False)
            if existing is None:
                raise
            self._ensure_same_economics(existing, values)
            return existing

    def _ensure_sqlite_transaction(self) -> None:
        connection = self.session.connection()
        if connection.dialect.name != "sqlite":
            return

        driver_connection = connection.connection.driver_connection
        if not driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN")

    def get(self, recommendation_id: str) -> OrderIntent:
        return self._locked(recommendation_id, required=True)

    def get_by_ib_order_id(
        self, ib_order_id: str | int, *, account_id: str | None = None
    ) -> OrderIntent | None:
        """Load attribution by broker order ID in any lifecycle state."""
        stmt = select(OrderIntent).where(OrderIntent.ib_order_id == str(ib_order_id))
        if account_id is not None:
            stmt = stmt.where(OrderIntent.account_id == account_id)
        return self.session.scalar(stmt.with_for_update())

    def transition(
        self,
        recommendation_id: str,
        new_status: OrderStatus,
        *,
        reason: str | None = None,
    ) -> OrderIntent:
        intent = self._locked(recommendation_id, required=True)
        current_status = OrderStatus(intent.status)
        new_status = OrderStatus(new_status)
        if new_status not in ALLOWED_TRANSITIONS.get(current_status, set()):
            raise InvalidOrderTransition(
                f"cannot transition {recommendation_id} from "
                f"{current_status.value} to {new_status.value}"
            )

        now = _utcnow()
        intent.status = new_status.value
        intent.reason = reason
        intent.updated_at = now
        if new_status is OrderStatus.APPROVED:
            intent.approved_at = now
        if new_status is OrderStatus.SUBMITTED:
            intent.submitted_at = now
        if new_status in TERMINAL_STATUSES:
            intent.terminal_at = now
        self.session.flush()
        return intent

    def record_submission(
        self, recommendation_id: str, ib_order_id: str | int
    ) -> OrderIntent:
        intent = self.transition(recommendation_id, OrderStatus.SUBMITTED)
        intent.ib_order_id = str(ib_order_id)
        self.session.flush()
        return intent

    def active_reservations(
        self,
        portfolio: str,
        *,
        account_id: str | None = None,
        exclude_recommendation_id: str | None = None,
    ) -> float:
        unfilled_notional = (
            OrderIntent.requested_quantity - OrderIntent.filled_quantity
        ) * OrderIntent.limit_price
        stmt = select(func.coalesce(func.sum(unfilled_notional), 0.0)).where(
            OrderIntent.portfolio == portfolio,
            func.upper(OrderIntent.action) == "BUY",
            OrderIntent.status.in_(ACTIVE_RESERVATION_STATUSES),
        )
        if account_id is not None:
            stmt = stmt.where(OrderIntent.account_id == account_id)
        if exclude_recommendation_id is not None:
            stmt = stmt.where(
                OrderIntent.recommendation_id != exclude_recommendation_id
            )
        return float(self.session.scalar(stmt) or 0.0)

    def load_pending_orders(
        self, *, account_id: str | None = None
    ) -> list[OrderIntent]:
        stmt = (
            select(OrderIntent)
            .where(OrderIntent.status.in_(PENDING_ORDER_STATUSES))
            .order_by(OrderIntent.id)
        )
        if account_id is not None:
            stmt = stmt.where(OrderIntent.account_id == account_id)
        return list(self.session.scalars(stmt))

    def mark_published(
        self,
        recommendation_id: str,
        *,
        published_at: datetime | None = None,
    ) -> OrderIntent:
        intent = self._locked(recommendation_id, required=True)
        if intent.published_at is None:
            now = published_at or _utcnow()
            intent.published_at = now
            intent.updated_at = now
            self.session.flush()
        return intent

    def _locked(self, recommendation_id: str, *, required: bool) -> OrderIntent | None:
        stmt = (
            select(OrderIntent)
            .where(OrderIntent.recommendation_id == recommendation_id)
            .with_for_update()
        )
        intent = self.session.scalar(stmt)
        if intent is None and required:
            raise OrderIntentNotFound(
                f"no order intent for recommendation {recommendation_id}"
            )
        return intent

    @staticmethod
    def _proposal_values(proposal: Any) -> dict[str, Any]:
        return {
            "account_id": proposal.account_id,
            "mode": proposal.mode,
            "portfolio": proposal.portfolio,
            "con_id": proposal.con_id,
            "symbol": proposal.symbol,
            "exchange": proposal.exchange,
            "currency": proposal.currency,
            "action": proposal.action,
            "requested_quantity": proposal.quantity,
            "limit_price": proposal.limit_price,
            "order_type": proposal.order_type,
        }

    @staticmethod
    def _ensure_same_economics(intent: OrderIntent, values: dict[str, Any]) -> None:
        conflicts = [
            field
            for field in IMMUTABLE_ECONOMIC_FIELDS
            if getattr(intent, field) != values[field]
        ]
        if conflicts:
            raise ConflictingOrderIntent(
                f"recommendation {intent.recommendation_id} conflicts on: "
                + ", ".join(conflicts)
            )
