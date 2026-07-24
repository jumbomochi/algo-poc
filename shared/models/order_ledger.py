from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class OrderStatus(StrEnum):
    PROPOSED = "PROPOSED"
    RISK_REJECTED = "RISK_REJECTED"
    APPROVED = "APPROVED"
    SUBMISSION_FAILED = "SUBMISSION_FAILED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


ORDER_STATUS_VALUES = tuple(status.value for status in OrderStatus)
ORDER_STATUS_CHECK = "status IN ({})".format(
    ", ".join(f"'{status}'" for status in ORDER_STATUS_VALUES)
)


class OrderIntent(Base):
    __tablename__ = "order_intents"
    __table_args__ = (
        UniqueConstraint(
            "recommendation_id", name="uq_order_intent_recommendation"
        ),
        CheckConstraint(ORDER_STATUS_CHECK, name="ck_order_intent_status"),
        Index("ix_order_intent_active", "status", "portfolio"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    recommendation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    account_id: Mapped[str] = mapped_column(String(50), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    portfolio: Mapped[str] = mapped_column(String(50), nullable=False)
    con_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    requested_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_type: Mapped[str] = mapped_column(String(20), nullable=False)
    reserved_notional: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    filled_quantity: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=OrderStatus.PROPOSED.value
    )
    ib_order_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    terminal_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ExecutionFill(Base):
    __tablename__ = "execution_fills"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "execution_id",
            name="uq_execution_fill_account_exec",
        ),
        Index("ix_execution_fill_order", "account_id", "ib_order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String(50), nullable=False)
    execution_id: Mapped[str] = mapped_column(String(100), nullable=False)
    ib_order_id: Mapped[str] = mapped_column(String(50), nullable=False)
    recommendation_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    portfolio: Mapped[str | None] = mapped_column(String(50), nullable=True)
    con_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    commission_currency: Mapped[str | None] = mapped_column(
        String(8), nullable=True
    )
    commission_trading: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    cumulative_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    projection_applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


class CapitalSnapshot(Base):
    __tablename__ = "capital_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String(50), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    net_liquidation: Mapped[float] = mapped_column(Float, nullable=False)
    base_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    trading_currency: Mapped[str | None] = mapped_column(
        String(8), nullable=True
    )
    net_liquidation_base: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    net_liquidation_trading_equivalent: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    fx_base_per_trading: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    fx_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fractional_base: Mapped[float | None] = mapped_column(Float, nullable=True)
    settled_cash_trading: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    deployment_fraction: Mapped[float] = mapped_column(Float, nullable=False)
    max_deployable_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    deployable_capital: Mapped[float] = mapped_column(Float, nullable=False)
    sleeve_budgets: Mapped[dict] = mapped_column(JSON, nullable=False)
    reconciliation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class CapitalAdjustment(Base):
    __tablename__ = "capital_adjustments"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String(50), nullable=False)
    portfolio: Mapped[str] = mapped_column(String(50), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    operator: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ReconciliationReport(Base):
    __tablename__ = "reconciliation_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String(50), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    entries_allowed: Mapped[bool] = mapped_column(nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
