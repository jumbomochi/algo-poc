"""add fill commission FX rate

Revision ID: b17c8e4a6d92
Revises: f6c2d9a84b31
Create Date: 2026-07-24

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b17c8e4a6d92"
down_revision: str | Sequence[str] | None = "f6c2d9a84b31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "execution_fills",
        sa.Column(
            "commission_fx_base_per_trading", sa.Float(), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "execution_fills", "commission_fx_base_per_trading"
    )
