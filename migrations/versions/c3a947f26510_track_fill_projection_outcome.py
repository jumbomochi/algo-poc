"""track fill projection outcome

Upgrade precondition: ``execution_fills`` must be empty.  Rows created before
this revision do not record whether portfolio accounting committed, so the
migration cannot safely infer an applied/rejected outcome for them.

Revision ID: c3a947f26510
Revises: 8b6f2c1d4a90
Create Date: 2026-07-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3a947f26510"
down_revision: Union[str, Sequence[str], None] = "8b6f2c1d4a90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    scalar = getattr(bind, "scalar", None)
    if not callable(scalar):
        raise RuntimeError(
            "offline migration cannot verify the execution_fills empty "
            "precondition; run this upgrade online"
        )

    has_legacy_fills = scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM execution_fills LIMIT 1)")
    )
    if has_legacy_fills:
        raise RuntimeError(
            "execution_fills must be empty before adding projection_applied; "
            "pre-existing fill outcomes cannot be inferred safely and require "
            "manual reconciliation"
        )

    op.add_column(
        "execution_fills",
        sa.Column(
            "projection_applied",
            sa.Boolean(),
            nullable=False,
            # A new execution is unapplied until accounting commits.  Keeping
            # this safe default also avoids unsupported SQLite ALTER syntax.
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("execution_fills", "projection_applied")
