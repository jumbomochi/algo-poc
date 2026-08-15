from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, exists, false, func, literal, or_, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.models import ExecutionFill, OrderIntent, OrderStatus, Position


class OrderLedgerError(RuntimeError):
    """Base error for durable order ledger operations."""


class OrderIntentNotFound(OrderLedgerError):
    """Raised when a recommendation has no durable order intent."""


class InvalidOrderTransition(OrderLedgerError):
    """Raised when an order lifecycle transition is not monotonic."""


class ConflictingOrderIntent(OrderLedgerError):
    """Raised when a recommendation ID is reused for a different order."""


ALLOWED_TRANSITIONS = {
    OrderStatus.PROPOSED: {
        OrderStatus.RISK_REJECTED,
        OrderStatus.APPROVED,
        OrderStatus.CANCELLED,
    },
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
# Everything a terminal status is not — including PROPOSED, which
# PENDING_ORDER_STATUSES deliberately omits. An exit must not be emitted while
# *any* of these is outstanding for the position, and a PROPOSED sell is still
# an order somebody intends to place.
NONTERMINAL_STATUSES = tuple(
    status.value for status in OrderStatus if status not in TERMINAL_STATUSES
)

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


def _unpublished_exit_clause():
    """An exit the risk service committed but never got onto the stream.

    Risk approves its own exits and then publishes them, so an APPROVED SELL
    with no ``published_at`` is an intent nothing downstream has ever seen: a
    crash in that window leaves it there forever. It is deliberately narrower
    than "nonterminal and unpublished":

    * ``APPROVED`` only — a PROPOSED unpublished sell belongs to run_paper's
      recommendation outbox, which publishes it to ``stream:recommendations``;
      republishing that to execution would place an order risk never approved.
    * ``SUBMITTED``/``PARTIALLY_FILLED`` are excluded because the broker already
      has them — the publish landed and only the bookkeeping was lost.

    Used from both ends: :meth:`OrderLedger.unpublished_exit_intents` selects
    this set to re-publish, and :meth:`OrderLedger.nonterminal_sell_exists`
    subtracts it so an orphan cannot masquerade as an exit in flight.
    """
    return and_(
        func.upper(OrderIntent.action) == "SELL",
        OrderIntent.status == OrderStatus.APPROVED.value,
        OrderIntent.published_at.is_(None),
    )


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

    def execution_fill_exists(
        self, account_id: str, execution_id: str
    ) -> bool:
        """Return whether a broker execution is already durably recorded."""
        return self.session.scalar(
            select(ExecutionFill.id).where(
                ExecutionFill.account_id == account_id,
                ExecutionFill.execution_id == execution_id,
            )
        ) is not None

    def managed_position_snapshot(
        self, execution_keys: Iterable[tuple[str, str]]
    ) -> tuple[list[tuple[str, float]], set[tuple[str, str]]]:
        """Read managed positions and projected identities in one snapshot."""
        keys = list(execution_keys)
        managed_sleeve = exists(
            select(OrderIntent.id).where(
                OrderIntent.account_id == Position.account_id,
                OrderIntent.portfolio == Position.portfolio,
            )
        )
        position_rows = select(
            literal("position").label("row_kind"),
            literal(None).label("account_id"),
            literal(None).label("execution_id"),
            Position.ticker.label("ticker"),
            Position.quantity.label("quantity"),
        ).where(
            Position.status == "open",
            Position.quantity > 0,
            Position.account_id.is_not(None),
            managed_sleeve,
        )
        projected_rows = select(
            literal("execution").label("row_kind"),
            ExecutionFill.account_id.label("account_id"),
            ExecutionFill.execution_id.label("execution_id"),
            literal(None).label("ticker"),
            literal(None).label("quantity"),
        ).where(
            (
                tuple_(
                    ExecutionFill.account_id, ExecutionFill.execution_id
                ).in_(keys)
                if keys
                else false()
            ),
            ExecutionFill.projection_applied.is_(True),
        )

        positions: list[tuple[str, float]] = []
        projected_keys: set[tuple[str, str]] = set()
        for row in self.session.execute(position_rows.union_all(projected_rows)):
            if row.row_kind == "position":
                positions.append((row.ticker, float(row.quantity)))
            else:
                projected_keys.add((row.account_id, row.execution_id))
        return positions, projected_keys

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

    def active_buy_reservations_for_account(
        self,
        account_id: str,
        *,
        commission_per_share: float,
        minimum_commission: float,
        exclude_recommendation_id: str | None = None,
    ) -> float:
        per_share = self._nonnegative_finite(
            commission_per_share, "commission_per_share"
        )
        minimum = self._nonnegative_finite(
            minimum_commission, "minimum_commission"
        )
        statement = select(OrderIntent).where(
            OrderIntent.account_id == account_id,
            func.upper(OrderIntent.action) == "BUY",
            OrderIntent.limit_price.is_not(None),
            or_(
                OrderIntent.status.in_(ACTIVE_RESERVATION_STATUSES),
                and_(
                    OrderIntent.status == OrderStatus.PROPOSED.value,
                    OrderIntent.published_at.is_not(None),
                ),
            ),
        )
        if exclude_recommendation_id is not None:
            statement = statement.where(
                OrderIntent.recommendation_id != exclude_recommendation_id
            )
        total = 0.0
        for intent in self.session.scalars(statement):
            requested = self._nonnegative_finite(
                intent.requested_quantity, "requested_quantity"
            )
            filled = self._nonnegative_finite(
                intent.filled_quantity, "filled_quantity"
            )
            price = self._nonnegative_finite(intent.limit_price, "limit_price")
            remaining = requested - filled
            if remaining < -1e-6:
                raise ValueError("filled_quantity exceeds requested_quantity")
            remaining = max(0.0, remaining)
            total += remaining * price + max(minimum, remaining * per_share)
        return total

    def buy_fill_spend_for_account_since(
        self, account_id: str, *, captured_after: datetime
    ) -> float:
        if not isinstance(captured_after, datetime):
            raise ValueError("capital snapshot captured_at is required")
        statement = select(ExecutionFill).where(
            ExecutionFill.account_id == account_id,
            func.upper(ExecutionFill.side) == "BUY",
            func.upper(ExecutionFill.currency) == "USD",
            ExecutionFill.executed_at > captured_after,
        )
        total = 0.0
        for fill in self.session.scalars(statement):
            quantity = self._nonnegative_finite(fill.quantity, "fill quantity")
            price = self._nonnegative_finite(fill.price, "fill price")
            if fill.commission_trading is not None:
                commission = self._nonnegative_finite(
                    fill.commission_trading, "trading commission"
                )
            elif (fill.commission_currency or "USD").upper() == "USD":
                commission = self._nonnegative_finite(
                    fill.commission, "USD commission"
                )
            else:
                raise ValueError(
                    "non-USD fill commission requires commission_trading"
                )
            total += quantity * price + commission
        return total

    def nonterminal_sell_exists(
        self,
        *,
        account_id: str | None,
        portfolio: str | None,
        con_id: int | None,
        exclude_unpublished_exits: bool = False,
    ) -> bool:
        """True if an unfinished SELL intent exists for this identity scope.

        Two open sells against one position is never correct: the second one
        oversells the moment the first fills. Recurring exits (stop-loss,
        passive trim) re-evaluate every scan and would otherwise re-emit for as
        long as the breach persists, so they ask this first.

        Scoped by ``{account_id, portfolio, con_id}`` — the same scope
        :func:`shared.liquidation.load_liquidation_targets` aggregates by — and
        deliberately not by ``kind``: a plain non-exit sell blocks a stop-loss
        just as an outstanding stop-loss does.

        ``exclude_unpublished_exits`` (KAN-8) drops the orphan class described
        in :func:`_unpublished_exit_clause` from the answer. An intent that was
        never published is not in flight — nothing downstream has it — so
        counting it as one mutes the ticker's stop-loss permanently.
        """
        conditions = [
            OrderIntent.account_id == account_id,
            OrderIntent.portfolio == portfolio,
            OrderIntent.con_id == con_id,
            func.upper(OrderIntent.action) == "SELL",
            OrderIntent.status.in_(NONTERMINAL_STATUSES),
        ]
        if exclude_unpublished_exits:
            conditions.append(~_unpublished_exit_clause())
        stmt = select(exists().where(*conditions))
        return bool(self.session.scalar(stmt))

    def unpublished_exit_intents(
        self, *, mode: str | None = None
    ) -> list[OrderIntent]:
        """Committed-but-never-published risk exits, oldest first.

        See :func:`_unpublished_exit_clause` for what qualifies. The risk
        service re-publishes these at the top of every periodic scan; the
        deterministic recommendation id makes a downstream replay a no-op.
        """
        stmt = (
            select(OrderIntent)
            .where(_unpublished_exit_clause())
            .order_by(OrderIntent.id)
        )
        if mode is not None:
            stmt = stmt.where(OrderIntent.mode == mode)
        return list(self.session.scalars(stmt))

    def count_intents_with_id_prefix(self, prefix: str) -> int:
        """How many intents already carry a recommendation id under ``prefix``.

        Feeds the ``seq`` of :func:`shared.liquidation.exit_intent_id`: the
        callers ask only once every prior exit in that family is terminal, so
        the count is the next free sequence number and a legitimate repeat exit
        (a post-fill re-entry that breaches again the same day) gets its own id
        instead of colliding with the filled one.
        """
        escaped = (
            prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        stmt = (
            select(func.count())
            .select_from(OrderIntent)
            .where(OrderIntent.recommendation_id.like(f"{escaped}%", escape="\\"))
        )
        return int(self.session.scalar(stmt) or 0)

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

    @staticmethod
    def _nonnegative_finite(value: Any, field: str) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a finite non-negative number") from exc
        if not math.isfinite(numeric) or numeric < 0:
            raise ValueError(f"{field} must be a finite non-negative number")
        return numeric
