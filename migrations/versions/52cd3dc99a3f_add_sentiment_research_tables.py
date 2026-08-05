"""add sentiment research tables

Additive only: three new tables for the social-sentiment research pipeline
(docs/superpowers/specs/2026-08-02-social-sentiment-research-design.md).
No existing table is touched.

Revision ID: 52cd3dc99a3f
Revises: 9b3d1c7e4a20
Create Date: 2026-08-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "52cd3dc99a3f"
down_revision: Union[str, Sequence[str], None] = "9b3d1c7e4a20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sentiment_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("source_id", sa.String(120), nullable=False),
        sa.Column("ticker", sa.String(10), nullable=False),
        sa.Column("author", sa.String(100), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_score", sa.Float(), nullable=True),
        sa.Column("local_score", sa.Float(), nullable=True),
        sa.Column("score_model", sa.String(50), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.UniqueConstraint("source", "source_id", "ticker", name="uq_sentiment_message"),
    )
    op.create_index(
        "ix_sentiment_messages_ticker_posted",
        "sentiment_messages",
        ["ticker", "posted_at"],
    )
    op.create_index(
        "ix_sentiment_messages_source_posted",
        "sentiment_messages",
        ["source", "posted_at"],
    )
    op.create_table(
        "sentiment_daily",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticker", sa.String(10), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("mean_score", sa.Float(), nullable=True),
        sa.Column("weighted_score", sa.Float(), nullable=True),
        sa.Column("score_std", sa.Float(), nullable=True),
        sa.Column("unique_authors", sa.Integer(), nullable=False),
        sa.Column("sentiment_zscore", sa.Float(), nullable=True),
        sa.Column("volume_zscore", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("ticker", "session_date", "source", name="uq_sentiment_daily"),
    )
    op.create_index(
        "ix_sentiment_daily_source_date",
        "sentiment_daily",
        ["source", "session_date"],
    )
    op.create_table(
        "sentiment_cursors",
        sa.Column("key", sa.String(120), primary_key=True),
        sa.Column("position", sa.String(50), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sentiment_cursors")
    op.drop_table("sentiment_daily")
    op.drop_table("sentiment_messages")
