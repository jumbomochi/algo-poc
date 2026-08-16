"""add alert records

Additive only: one new table, ``alert_records``, and no existing table is
touched.

Alerts were fire-and-forget — published to ``stream:alerts``, fanned out to the
channels, then gone. The go-live checklist's reliability gate (gate 5) asks
whether any unresolved critical alert occurred in the trailing window, and with
no stored answer that gate would have returned 0 and passed by ignorance. This
table is that gate's evidence.

``sa.JSON`` (not JSONB) matches every other JSON column in this schema and keeps
the sqlite-based migration tests runnable.

Revision ID: a5f3c81d0e72
Revises: d4b8e1f5a207
Create Date: 2026-08-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a5f3c81d0e72"
down_revision: Union[str, Sequence[str], None] = "d4b8e1f5a207"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "alert_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        # NOT NULL: a unique index in Postgres permits unlimited NULLs, which
        # would silently disable the at-least-once replay guard.
        sa.Column("message_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("priority", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("raised_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(120), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("message_id", name="uq_alert_records_message_id"),
    )
    op.create_index(
        "ix_alert_records_priority_raised",
        "alert_records",
        ["priority", "raised_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_alert_records_priority_raised", table_name="alert_records")
    op.drop_table("alert_records")
