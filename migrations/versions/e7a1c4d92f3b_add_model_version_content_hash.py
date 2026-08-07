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

ROLLOUT WARNING — this migration alone is NOT safe to deploy on its own.
Every existing ModelVersion row (including the currently-active one) will
have content_hash = NULL after `alembic upgrade head`, and
ModelRegistry.load_active() refuses to load any model whose content_hash
is unset (fail-closed, intentionally). Applying this migration without a
backfill step means the next load_active() call raises
ModelIntegrityError and the ml_model service cannot get a model until an
operator intervenes.

Required rollout sequence:
  1. `alembic upgrade head`               (this migration — adds the column)
  2. `python -m scripts.ops.backfill_model_hashes --apply`
                                           (computes + writes content_hash
                                            for every row whose model file
                                            still exists on disk)
  3. Verify: call ModelRegistry.load_active() (or restart the ml_model
     service and confirm it loads a model successfully) before considering
     the rollout complete.
See docs/operations/api-security.md ("Model integrity — rollout sequence")
for the full runbook.

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
