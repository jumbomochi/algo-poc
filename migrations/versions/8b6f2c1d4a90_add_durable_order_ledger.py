"""add durable order ledger

Revision ID: 8b6f2c1d4a90
Revises: 1f7ead32f0fa
Create Date: 2026-07-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8b6f2c1d4a90"
down_revision: Union[str, Sequence[str], None] = "1f7ead32f0fa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ORDER_STATUS_CHECK = (
    "status IN ('PROPOSED', 'RISK_REJECTED', 'APPROVED', "
    "'SUBMISSION_FAILED', 'SUBMITTED', 'PARTIALLY_FILLED', 'FILLED', "
    "'CANCELLED', 'EXPIRED')"
)


def upgrade() -> None:
    op.add_column("positions", sa.Column("con_id", sa.BigInteger(), nullable=True))
    op.add_column("positions", sa.Column("exchange", sa.String(32), nullable=True))
    op.add_column("positions", sa.Column("currency", sa.String(8), nullable=True))

    op.create_table(
        "order_intents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("recommendation_id", sa.String(255), nullable=False),
        sa.Column("account_id", sa.String(50), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("portfolio", sa.String(50), nullable=False),
        sa.Column("con_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("requested_quantity", sa.Float(), nullable=False),
        sa.Column("limit_price", sa.Float(), nullable=True),
        sa.Column("order_type", sa.String(20), nullable=False),
        sa.Column("reserved_notional", sa.Float(), nullable=False),
        sa.Column("filled_quantity", sa.Float(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("ib_order_id", sa.String(50), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(ORDER_STATUS_CHECK, name="ck_order_intent_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "recommendation_id", name="uq_order_intent_recommendation"
        ),
    )
    op.create_index(
        "ix_order_intent_active",
        "order_intents",
        ["status", "portfolio"],
        unique=False,
    )

    op.create_table(
        "execution_fills",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(50), nullable=False),
        sa.Column("execution_id", sa.String(100), nullable=False),
        sa.Column("ib_order_id", sa.String(50), nullable=False),
        sa.Column("recommendation_id", sa.String(255), nullable=True),
        sa.Column("portfolio", sa.String(50), nullable=True),
        sa.Column("con_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("commission", sa.Float(), nullable=False),
        sa.Column("cumulative_quantity", sa.Float(), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "execution_id",
            name="uq_execution_fill_account_exec",
        ),
    )
    op.create_index(
        "ix_execution_fill_order",
        "execution_fills",
        ["account_id", "ib_order_id"],
        unique=False,
    )

    op.create_table(
        "capital_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(50), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("net_liquidation", sa.Float(), nullable=False),
        sa.Column("deployment_fraction", sa.Float(), nullable=False),
        sa.Column("max_deployable_usd", sa.Float(), nullable=True),
        sa.Column("deployable_capital", sa.Float(), nullable=False),
        sa.Column("sleeve_budgets", sa.JSON(), nullable=False),
        sa.Column("reconciliation_status", sa.String(32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "capital_adjustments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(50), nullable=False),
        sa.Column("portfolio", sa.String(50), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operator", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "reconciliation_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(50), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("entries_allowed", sa.Boolean(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("reconciliation_reports")
    op.drop_table("capital_adjustments")
    op.drop_table("capital_snapshots")
    op.drop_index("ix_execution_fill_order", table_name="execution_fills")
    op.drop_table("execution_fills")
    op.drop_index("ix_order_intent_active", table_name="order_intents")
    op.drop_table("order_intents")
    op.drop_column("positions", "currency")
    op.drop_column("positions", "exchange")
    op.drop_column("positions", "con_id")
