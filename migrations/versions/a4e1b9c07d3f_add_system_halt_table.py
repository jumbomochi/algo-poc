"""add system_halt table

Additive only: durable kill-switch / circuit-breaker halt state so the risk
service can reload a halt on restart and stay halted (fail-closed) until an
explicit human clear. No existing table is touched.

Revision ID: a4e1b9c07d3f
Revises: 52cd3dc99a3f
Create Date: 2026-08-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4e1b9c07d3f"
down_revision: Union[str, Sequence[str], None] = "52cd3dc99a3f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_halt",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("triggered_by", sa.String(120), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleared_by", sa.String(120), nullable=True),
    )
    op.create_index(
        "ix_system_halt_mode_active", "system_halt", ["mode", "active"]
    )


def downgrade() -> None:
    op.drop_index("ix_system_halt_mode_active", table_name="system_halt")
    op.drop_table("system_halt")
