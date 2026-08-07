"""add model_versions.content_hash

Additive only: one new nullable column. Stores the sha256 of the model
file recorded at save() time, so ModelRegistry.load_active() can verify a
model file's integrity before joblib.load without trusting a value stored
in the same filesystem location/permission domain as the artifact itself
(a filesystem-level attacker who can rewrite the model file can rewrite a
sidecar file in the same operation; they cannot also forge a Postgres
row without separate database credentials).

See docs/reviews/threads/T9-security-hardening.md and
docs/operations/api-security.md ("Model integrity").

Revision ID: e7a1c4d92f3b
Revises: 52cd3dc99a3f
Create Date: 2026-08-07

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7a1c4d92f3b"
down_revision: str | Sequence[str] | None = "52cd3dc99a3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_versions",
        sa.Column("content_hash", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model_versions", "content_hash")
