"""Add paper trading schema: extend positions/trades, add equity_snapshots and portfolio_config.

Revision ID: 001
Revises:
Create Date: 2026-04-13
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- positions table ---
    # Add new columns
    op.add_column("positions", sa.Column("portfolio", sa.String(50), nullable=False, server_default="unknown"))
    op.add_column("positions", sa.Column("peak_price", sa.Float(), nullable=False, server_default="0"))
    op.add_column("positions", sa.Column("entry_signals", sa.JSON(), nullable=True))
    # Make sector nullable
    op.alter_column("positions", "sector", existing_type=sa.String(50), nullable=True)
    # Change quantity from Integer to Float
    op.alter_column("positions", "quantity", existing_type=sa.Integer(), type_=sa.Float())
    # Remove server defaults (only needed for migration of existing rows)
    op.alter_column("positions", "portfolio", server_default=None)
    op.alter_column("positions", "peak_price", server_default=None)
    # Add index
    op.create_index("ix_positions_portfolio", "positions", ["portfolio"])

    # --- trades table ---
    # Add new columns
    op.add_column("trades", sa.Column("portfolio", sa.String(50), nullable=False, server_default="unknown"))
    op.add_column("trades", sa.Column("entry_price", sa.Float(), nullable=False, server_default="0"))
    op.add_column("trades", sa.Column("entry_date", sa.Date(), nullable=False, server_default="2020-01-01"))
    op.add_column("trades", sa.Column("exit_reason", sa.String(50), nullable=True))
    op.add_column("trades", sa.Column("pnl", sa.Float(), nullable=False, server_default="0"))
    op.add_column("trades", sa.Column("entry_signals", sa.JSON(), nullable=True))
    op.add_column("trades", sa.Column("bar_features", sa.JSON(), nullable=True))
    # Make recommendation_id and order_type nullable
    op.alter_column("trades", "recommendation_id", existing_type=sa.String(50), nullable=True)
    op.alter_column("trades", "order_type", existing_type=sa.String(20), nullable=True)
    # Change quantity from Integer to Float
    op.alter_column("trades", "quantity", existing_type=sa.Integer(), type_=sa.Float())
    # Remove server defaults
    op.alter_column("trades", "portfolio", server_default=None)
    op.alter_column("trades", "entry_price", server_default=None)
    op.alter_column("trades", "entry_date", server_default=None)
    op.alter_column("trades", "pnl", server_default=None)
    # Add index
    op.create_index("ix_trade_portfolio", "trades", ["portfolio"])

    # --- equity_snapshots table (new) ---
    op.create_table(
        "equity_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("portfolio", sa.String(50), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False),
        sa.Column("market_value", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_equity_portfolio_date", "equity_snapshots",
        ["portfolio", "date"], unique=True,
    )

    # --- portfolio_config table (new) ---
    op.create_table(
        "portfolio_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("portfolio", sa.String(50), nullable=False, unique=True),
        sa.Column("capital", sa.Float(), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("portfolio_config")
    op.drop_index("ix_equity_portfolio_date", table_name="equity_snapshots")
    op.drop_table("equity_snapshots")

    # trades: drop new columns, restore non-nullable, restore Integer
    op.drop_index("ix_trade_portfolio", table_name="trades")
    op.drop_column("trades", "bar_features")
    op.drop_column("trades", "entry_signals")
    op.drop_column("trades", "pnl")
    op.drop_column("trades", "exit_reason")
    op.drop_column("trades", "entry_date")
    op.drop_column("trades", "entry_price")
    op.drop_column("trades", "portfolio")
    op.alter_column("trades", "recommendation_id", existing_type=sa.String(50), nullable=False)
    op.alter_column("trades", "order_type", existing_type=sa.String(20), nullable=False)
    op.alter_column("trades", "quantity", existing_type=sa.Float(), type_=sa.Integer())

    # positions: drop new columns, restore non-nullable sector, restore Integer
    op.drop_index("ix_positions_portfolio", table_name="positions")
    op.drop_column("positions", "entry_signals")
    op.drop_column("positions", "peak_price")
    op.drop_column("positions", "portfolio")
    op.alter_column("positions", "sector", existing_type=sa.String(50), nullable=False)
    op.alter_column("positions", "quantity", existing_type=sa.Float(), type_=sa.Integer())
