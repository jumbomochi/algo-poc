"""add equity_snapshots.session_date

KAN-103. ``equity_snapshots.date`` is the SGT run date, so the row dated
Tuesday holds Monday's US close and the divergence monitor grades live one
session off its shadow. This records the US session each snapshot valued, as
a fact at write time, beside ``date`` (which keeps its meaning).

Nullable and left NULL on every existing row: the operator fills history with
``scripts/ops/backfill_snapshot_sessions.py`` (dry-run first, then --apply).
The index is NOT unique, because a Tuesday after a US Monday holiday and a
weekend catch-up both legitimately re-value the previous session.

Revision ID: b3d5f7a9c1e2
Revises: 007388445941
Create Date: 2026-10-05

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b3d5f7a9c1e2"
down_revision = "007388445941"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "equity_snapshots",
        sa.Column("session_date", sa.Date(), nullable=True),
    )
    op.create_index(
        "ix_equity_portfolio_session_date",
        "equity_snapshots",
        ["portfolio", "session_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_equity_portfolio_session_date", table_name="equity_snapshots"
    )
    op.drop_column("equity_snapshots", "session_date")
