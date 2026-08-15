"""add evidence state store

Additive only: the four observation tables the capital ladder reads —
``divergence_daily``, ``gate_epochs``, ``gate_epoch_events`` and
``drill_outcomes``. No existing table is touched and no data is migrated.

These tables hold observations, never derived truth (direction doc D15):
there is deliberately no stored streak length, no ``is_blind``/``is_clean``
flag, and no epoch ``status``/``ended_at`` — blindness is derived from the
*absence* of a ``divergence_daily`` row on an NYSE trading day, and an epoch's
state is the fold of its ``gate_epoch_events`` rows.

``sa.JSON`` (not JSONB) matches every other JSON column in this schema and
keeps the sqlite-based migration tests runnable.

Revision ID: d4b8e1f5a207
Revises: 82623f87013d
Create Date: 2026-08-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from shared.models.evidence import DIVERGENCE_STATUS_CHECK, DRILL_TYPE_CHECK


revision: str = "d4b8e1f5a207"
down_revision: Union[str, Sequence[str], None] = "82623f87013d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "divergence_daily",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sleeve", sa.String(64), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("baseline_id", sa.String(128), nullable=False),
        sa.Column("window_sessions", sa.Integer(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("metric_value", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "sleeve",
            "session_date",
            "baseline_id",
            name="uq_divergence_daily_sleeve_date_baseline",
        ),
        sa.CheckConstraint(
            DIVERGENCE_STATUS_CHECK, name="ck_divergence_daily_status"
        ),
    )
    op.create_index(
        "ix_divergence_daily_session_date", "divergence_daily", ["session_date"]
    )
    op.create_index(
        "ix_divergence_daily_sleeve_date",
        "divergence_daily",
        ["sleeve", "session_date"],
    )

    op.create_table(
        "gate_epochs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("rung", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("label", name="uq_gate_epochs_label"),
    )
    op.create_index(
        "ix_gate_epochs_rung_started", "gate_epochs", ["rung", "started_at"]
    )

    op.create_table(
        "gate_epoch_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("epoch_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("rung_after", sa.Integer(), nullable=True),
        sa.Column("incident_id", sa.String(64), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_gate_epoch_events_epoch_occurred",
        "gate_epoch_events",
        ["epoch_id", "occurred_at"],
    )

    op.create_table(
        "drill_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("epoch_id", sa.Integer(), nullable=False),
        sa.Column("drill_type", sa.String(32), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.String(2000), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(DRILL_TYPE_CHECK, name="ck_drill_outcomes_type"),
    )
    op.create_index(
        "ix_drill_outcomes_epoch_type", "drill_outcomes", ["epoch_id", "drill_type"]
    )


def downgrade() -> None:
    # Dropping a table drops its indexes on both Postgres and sqlite, so no
    # explicit drop_index calls here — on sqlite they can error.
    op.drop_table("drill_outcomes")
    op.drop_table("gate_epoch_events")
    op.drop_table("gate_epochs")
    op.drop_table("divergence_daily")
