"""add durable position account ownership

Legacy rows remain NULL deliberately. Their ownership cannot be inferred and
startup reconciliation must fail closed until an operator reviews them.

Revision ID: d8f10a4b72c3
Revises: c3a947f26510
Create Date: 2026-07-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8f10a4b72c3"
down_revision: Union[str, Sequence[str], None] = "c3a947f26510"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("account_id", sa.String(50), nullable=True),
    )
    op.create_index(
        "ix_positions_account_contract",
        "positions",
        ["account_id", "con_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_positions_account_contract", table_name="positions")
    op.drop_column("positions", "account_id")
