"""add dual-currency accounting

Revision ID: f6c2d9a84b31
Revises: d8f10a4b72c3
Create Date: 2026-07-24

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6c2d9a84b31"
down_revision: str | Sequence[str] | None = "d8f10a4b72c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "capital_snapshots",
        sa.Column("base_currency", sa.String(8), nullable=True),
    )
    op.add_column(
        "capital_snapshots",
        sa.Column("trading_currency", sa.String(8), nullable=True),
    )
    op.add_column(
        "capital_snapshots",
        sa.Column("net_liquidation_base", sa.Float(), nullable=True),
    )
    op.add_column(
        "capital_snapshots",
        sa.Column(
            "net_liquidation_trading_equivalent", sa.Float(), nullable=True
        ),
    )
    op.add_column(
        "capital_snapshots",
        sa.Column("fx_base_per_trading", sa.Float(), nullable=True),
    )
    op.add_column(
        "capital_snapshots",
        sa.Column(
            "fx_captured_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "capital_snapshots",
        sa.Column("fractional_base", sa.Float(), nullable=True),
    )
    op.add_column(
        "capital_snapshots",
        sa.Column("settled_cash_trading", sa.Float(), nullable=True),
    )

    op.add_column(
        "portfolio_config",
        sa.Column(
            "currency",
            sa.String(8),
            nullable=False,
            server_default="USD",
        ),
    )

    op.add_column(
        "equity_snapshots",
        sa.Column("base_currency", sa.String(8), nullable=True),
    )
    op.add_column(
        "equity_snapshots",
        sa.Column("trading_currency", sa.String(8), nullable=True),
    )
    op.add_column(
        "equity_snapshots",
        sa.Column("equity_trading", sa.Float(), nullable=True),
    )
    op.add_column(
        "equity_snapshots",
        sa.Column("cash_trading", sa.Float(), nullable=True),
    )
    op.add_column(
        "equity_snapshots",
        sa.Column("market_value_trading", sa.Float(), nullable=True),
    )
    op.add_column(
        "equity_snapshots",
        sa.Column("fx_base_per_trading", sa.Float(), nullable=True),
    )
    op.add_column(
        "equity_snapshots",
        sa.Column("equity_base", sa.Float(), nullable=True),
    )
    op.add_column(
        "equity_snapshots",
        sa.Column("valuation_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "execution_fills",
        sa.Column("commission_currency", sa.String(8), nullable=True),
    )
    op.add_column(
        "execution_fills",
        sa.Column("commission_trading", sa.Float(), nullable=True),
    )
    op.add_column(
        "execution_fills",
        sa.Column(
            "commission_fx_base_per_trading", sa.Float(), nullable=True
        ),
    )

    op.create_table(
        "currency_conversions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(50), nullable=False),
        sa.Column("source_currency", sa.String(8), nullable=False),
        sa.Column("source_amount", sa.Float(), nullable=False),
        sa.Column("target_currency", sa.String(8), nullable=False),
        sa.Column("target_amount", sa.Float(), nullable=False),
        sa.Column("fx_base_per_trading", sa.Float(), nullable=False),
        sa.Column("fee_amount", sa.Float(), nullable=False),
        sa.Column("fee_currency", sa.String(8), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("operator", sa.String(100), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("currency_conversions")
    op.drop_column(
        "execution_fills", "commission_fx_base_per_trading"
    )
    op.drop_column("execution_fills", "commission_trading")
    op.drop_column("execution_fills", "commission_currency")
    op.drop_column("equity_snapshots", "valuation_at")
    op.drop_column("equity_snapshots", "equity_base")
    op.drop_column("equity_snapshots", "fx_base_per_trading")
    op.drop_column("equity_snapshots", "market_value_trading")
    op.drop_column("equity_snapshots", "cash_trading")
    op.drop_column("equity_snapshots", "equity_trading")
    op.drop_column("equity_snapshots", "trading_currency")
    op.drop_column("equity_snapshots", "base_currency")
    op.drop_column("portfolio_config", "currency")
    op.drop_column("capital_snapshots", "settled_cash_trading")
    op.drop_column("capital_snapshots", "fractional_base")
    op.drop_column("capital_snapshots", "fx_captured_at")
    op.drop_column("capital_snapshots", "fx_base_per_trading")
    op.drop_column(
        "capital_snapshots", "net_liquidation_trading_equivalent"
    )
    op.drop_column("capital_snapshots", "net_liquidation_base")
    op.drop_column("capital_snapshots", "trading_currency")
    op.drop_column("capital_snapshots", "base_currency")
