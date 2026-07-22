from __future__ import annotations

from datetime import datetime, timezone
from math import isclose, isfinite
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from scripts.paper_state import PaperTradingState
from shared.models import ExecutionFill, OrderIntent, OrderStatus
from shared.order_ledger import OrderLedger
from shared.schemas.messages import FillMessage


class FillProjectionError(RuntimeError):
    """Base class for a fill that cannot be safely projected."""


class UnattributedFillError(FillProjectionError):
    """The broker fill cannot be matched exactly to its durable intent."""


class InvalidFillError(FillProjectionError):
    """The fill would violate durable order or sleeve accounting economics."""


class FillConflictError(FillProjectionError):
    """A broker execution identity was reused with different immutable data."""


_REQUIRED_IDENTITY_FIELDS = (
    "execution_id",
    "account_id",
    "portfolio",
    "con_id",
    "exchange",
    "currency",
)

_IMMUTABLE_FILL_FIELDS = (
    "account_id",
    "execution_id",
    "ib_order_id",
    "recommendation_id",
    "portfolio",
    "con_id",
    "symbol",
    "exchange",
    "currency",
    "side",
    "quantity",
    "price",
    "commission",
    "cumulative_quantity",
    "executed_at",
)


class FillProjector:
    """Idempotently project immutable IB executions into sleeve state.

    The projector owns its session transaction.  A rejected but identifiable
    broker execution is committed to ``execution_fills`` for audit while all
    materialized accounting state remains unchanged; the error is raised only
    after that audit transaction commits.
    """

    def __init__(self, session: Session):
        self.session = session
        self._ledger = OrderLedger(session)
        self._paper_state = PaperTradingState(session)

    def apply(self, fill: FillMessage) -> bool:
        values = self._fill_values(fill)
        self._end_read_only_autobegin()
        projection_error: FillProjectionError | None = None

        with self.session.begin():
            existing = self._existing_fill(values)
            if existing is not None:
                self._ensure_same_fill(existing, values)
                return False

            execution = ExecutionFill(**values)
            try:
                with self.session.begin_nested():
                    self.session.add(execution)
                    self.session.flush()
            except IntegrityError as exc:
                if not self._is_execution_identity_collision(exc):
                    raise
                existing = self._existing_fill(values)
                if existing is None:
                    raise
                self._ensure_same_fill(existing, values)
                return False

            intent = self.session.scalar(
                select(OrderIntent)
                .where(OrderIntent.recommendation_id == fill.recommendation_id)
                .with_for_update()
            )
            try:
                with self.session.begin_nested():
                    cumulative = self._validate(fill, intent, execution)
                    self._paper_state._apply_fill_accounting(
                        account_id=intent.account_id,
                        portfolio=intent.portfolio,
                        ticker=intent.symbol,
                        action=fill.side,
                        quantity=fill.quantity,
                        price=fill.fill_price,
                        fill_datetime=fill.timestamp,
                        commission=fill.commission,
                        recommendation_id=intent.recommendation_id,
                        con_id=intent.con_id,
                        exchange=intent.exchange,
                        currency=intent.currency,
                        strict_quantity=True,
                        exit_reason=intent.reason,
                    )
                    self._advance_intent(intent, cumulative)
                    execution.projection_applied = True
                    self.session.flush()
            except (FillProjectionError, ValueError) as exc:
                # Do not raise inside the transaction: the immutable execution
                # row is the durable audit record and must survive the failure.
                projection_error = (
                    exc if isinstance(exc, FillProjectionError)
                    else InvalidFillError(str(exc))
                )

        if projection_error is not None:
            raise projection_error
        return True

    def _end_read_only_autobegin(self) -> None:
        if not self.session.in_transaction():
            return
        if self.session.new or self.session.dirty or self.session.deleted:
            raise FillProjectionError(
                "projector session has uncommitted caller changes"
            )
        self.session.rollback()

    @staticmethod
    def _fill_values(fill: FillMessage) -> dict[str, Any]:
        missing = [
            name for name in _REQUIRED_IDENTITY_FIELDS
            if getattr(fill, name) is None or getattr(fill, name) == ""
        ]
        if missing:
            raise InvalidFillError(
                "fill lacks enriched broker identity: " + ", ".join(missing)
            )
        numeric = (fill.quantity, fill.fill_price, fill.commission)
        if fill.cumulative_quantity is not None:
            numeric += (fill.cumulative_quantity,)
        if not all(isfinite(value) for value in numeric):
            raise InvalidFillError("fill economics must be finite")
        return {
            "account_id": fill.account_id,
            "execution_id": fill.execution_id,
            "ib_order_id": str(fill.order_id),
            "recommendation_id": fill.recommendation_id,
            "portfolio": fill.portfolio,
            "con_id": fill.con_id,
            "symbol": fill.ticker,
            "exchange": fill.exchange,
            "currency": fill.currency,
            "side": fill.side.upper(),
            "quantity": fill.quantity,
            "price": fill.fill_price,
            "commission": fill.commission,
            "cumulative_quantity": fill.cumulative_quantity,
            "executed_at": fill.timestamp,
        }

    def _existing_fill(self, values: dict[str, Any]) -> ExecutionFill | None:
        return self.session.scalar(
            select(ExecutionFill)
            .where(
                ExecutionFill.account_id == values["account_id"],
                ExecutionFill.execution_id == values["execution_id"],
            )
            .with_for_update()
        )

    @staticmethod
    def _ensure_same_fill(
        existing: ExecutionFill, values: dict[str, Any]
    ) -> None:
        conflicts = [
            field
            for field in _IMMUTABLE_FILL_FIELDS
            if not _same_value(getattr(existing, field), values[field])
        ]
        if conflicts:
            raise FillConflictError(
                "execution identity conflicts on: " + ", ".join(conflicts)
            )

    @staticmethod
    def _is_execution_identity_collision(exc: IntegrityError) -> bool:
        message = str(exc.orig).lower()
        return (
            "uq_execution_fill_account_exec" in message
            or (
                "unique constraint failed" in message
                and "execution_fills.account_id" in message
                and "execution_fills.execution_id" in message
            )
        )

    def _validate(
        self,
        fill: FillMessage,
        intent: OrderIntent | None,
        execution: ExecutionFill,
    ) -> float:
        if intent is None:
            raise UnattributedFillError(
                f"unknown recommendation {fill.recommendation_id}"
            )

        expected = {
            "account": (intent.account_id, fill.account_id),
            "order": (str(intent.ib_order_id), str(fill.order_id)),
            "portfolio": (intent.portfolio, fill.portfolio),
            "contract": (intent.con_id, fill.con_id),
            "symbol": (intent.symbol, fill.ticker),
            "exchange": (intent.exchange, fill.exchange),
            "currency": (intent.currency, fill.currency),
            "side": (intent.action.upper(), fill.side.upper()),
        }
        mismatches = [
            name for name, (durable, actual) in expected.items()
            if durable != actual
        ]
        if mismatches:
            raise UnattributedFillError(
                "fill conflicts with durable intent on: " + ", ".join(mismatches)
            )

        if intent.status not in {
            OrderStatus.SUBMITTED.value,
            OrderStatus.PARTIALLY_FILLED.value,
            OrderStatus.FILLED.value,
        }:
            raise InvalidFillError(
                f"intent status {intent.status} cannot accept executions"
            )
        if fill.quantity <= 0:
            raise InvalidFillError("fill quantity must be positive")
        if fill.fill_price <= 0:
            raise InvalidFillError("fill price must be positive")
        if fill.commission < 0:
            raise InvalidFillError("fill commission cannot be negative")

        prior = (
            self._reconstructed_projected_quantity(intent, execution.id)
            if intent.status == OrderStatus.FILLED.value
            else float(intent.filled_quantity)
        )
        expected_cumulative = prior + fill.quantity
        cumulative = (
            expected_cumulative
            if fill.cumulative_quantity is None
            else float(fill.cumulative_quantity)
        )
        if cumulative <= prior or not isclose(
            cumulative, expected_cumulative, rel_tol=1e-9, abs_tol=1e-9
        ):
            raise InvalidFillError(
                "fill cumulative quantity is not monotonic with executions"
            )
        if cumulative > intent.requested_quantity and not isclose(
            cumulative, intent.requested_quantity, rel_tol=1e-9, abs_tol=1e-9
        ):
            raise InvalidFillError("fill would overfill durable intent")
        return cumulative

    def _reconstructed_projected_quantity(
        self, intent: OrderIntent, current_execution_id: int
    ) -> float:
        """Rebuild applied quantity for history-precompleted FILLED intents.

        Failed projection attempts remain immutable audit rows, so they cannot
        be counted blindly.  Only the monotonic execution prefix matching the
        durable intent contributes to already-projected quantity.
        """
        rows = self.session.scalars(
            select(ExecutionFill)
            .where(
                ExecutionFill.recommendation_id == intent.recommendation_id,
                ExecutionFill.account_id == intent.account_id,
                ExecutionFill.id != current_execution_id,
                ExecutionFill.projection_applied.is_(True),
            )
            .order_by(ExecutionFill.id)
        )
        projected = 0.0
        for row in rows:
            if not self._row_matches_intent(row, intent):
                continue
            next_quantity = projected + row.quantity
            cumulative = (
                next_quantity
                if row.cumulative_quantity is None
                else row.cumulative_quantity
            )
            if (
                row.quantity <= 0
                or next_quantity > intent.requested_quantity + 1e-9
                or not isclose(
                    cumulative, next_quantity, rel_tol=1e-9, abs_tol=1e-9
                )
            ):
                continue
            projected = next_quantity
        return projected

    @staticmethod
    def _row_matches_intent(row: ExecutionFill, intent: OrderIntent) -> bool:
        return (
            row.account_id == intent.account_id
            and row.ib_order_id == str(intent.ib_order_id)
            and row.portfolio == intent.portfolio
            and row.con_id == intent.con_id
            and row.symbol == intent.symbol
            and row.exchange == intent.exchange
            and row.currency == intent.currency
            and row.side == intent.action.upper()
        )

    def _advance_intent(self, intent: OrderIntent, cumulative: float) -> None:
        intent.filled_quantity = max(float(intent.filled_quantity), cumulative)
        if intent.status == OrderStatus.FILLED.value:
            self.session.flush()
            return
        new_status = (
            OrderStatus.FILLED
            if isclose(cumulative, intent.requested_quantity)
            else OrderStatus.PARTIALLY_FILLED
        )
        self._ledger.transition(intent.recommendation_id, new_status)


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, float) or isinstance(right, float):
        if left is None or right is None:
            return left is right
        return isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(left, datetime) and isinstance(right, datetime):
        return _as_utc(left) == _as_utc(right)
    return left == right


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
