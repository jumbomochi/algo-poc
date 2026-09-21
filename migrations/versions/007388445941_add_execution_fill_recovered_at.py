"""add execution_fills.recovered_at

recovery_source (8f3a41bf29f8) says HOW a fill reached the book. This
says WHEN. The two are not the same question and only one of them can be
answered from executed_at: KAN-88 rebuilds executions from 2026-09-18
out of an IB statement days later, so a digest bounded on the broker's
clock reports zero recoveries on precisely the night the repair ran.

Nullable, and left NULL for every row written before this: the digest
falls back to executed_at through a coalesce, which is exactly the
behaviour those rows had.

Revision ID: 007388445941
Revises: 8f3a41bf29f8
Create Date: 2026-09-21

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "007388445941"
down_revision = "8f3a41bf29f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_fills",
        sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execution_fills", "recovered_at")
