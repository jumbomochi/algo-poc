"""add execution_fills.recovery_source

Additive only: one new nullable column, no backfill, no data change.
NULL means the live execDetails callback wrote the row — which is every
row that exists when this migration runs. "ib_execution_sweep" is written
only by the KAN-87 daily sweep.

Safe to apply on its own and safe to leave unapplied for a while: nothing
reads the column until the sweep and the digest ship together. It still
must be applied BEFORE the next 04:15 paper run on any host that has
pulled this code, because run_paper.sh compares the DB revision against
the code's head and aborts on a mismatch (the 2026-08-26 abort).
"""

from alembic import op
import sqlalchemy as sa

revision = "8f3a41bf29f8"
down_revision = "f2c9a6d81b74"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_fills",
        sa.Column("recovery_source", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execution_fills", "recovery_source")
