"""add research candidates

Revision ID: 9b3d1c7e4a20
Revises: 1f7ead32f0fa
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9b3d1c7e4a20"
down_revision: Union[str, Sequence[str], None] = "1f7ead32f0fa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_key", sa.String(length=64), nullable=False),
        sa.Column("portfolio", sa.String(length=50), nullable=False),
        sa.Column("ticker", sa.String(length=10), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column("raw_signal", sa.JSON(), nullable=False),
        sa.Column("factor_values", sa.JSON(), nullable=False),
        sa.Column("risk_approved", sa.Boolean(), nullable=False),
        sa.Column("risk_reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_key"),
    )
    op.create_index(
        "ix_research_candidates_portfolio",
        "research_candidates",
        ["portfolio"],
    )
    op.create_index(
        "ix_research_candidates_ticker",
        "research_candidates",
        ["ticker"],
    )
    op.create_index(
        "ix_research_candidates_as_of",
        "research_candidates",
        ["as_of"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_candidates_as_of", table_name="research_candidates"
    )
    op.drop_index(
        "ix_research_candidates_ticker", table_name="research_candidates"
    )
    op.drop_index(
        "ix_research_candidates_portfolio", table_name="research_candidates"
    )
    op.drop_table("research_candidates")
