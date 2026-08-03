from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class SentimentMessage(Base):
    """Raw sentiment archive — append-only, never mutated.

    posted_at/collected_at mirror the repo's effective_at/ingested_at
    point-in-time convention so later backtests stay honest.
    """

    __tablename__ = "sentiment_messages"
    __table_args__ = (
        UniqueConstraint("source", "source_id", "ticker", name="uq_sentiment_message"),
        Index("ix_sentiment_messages_ticker_posted", "ticker", "posted_at"),
        Index("ix_sentiment_messages_source_posted", "source", "posted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    source_id: Mapped[str] = mapped_column(String(120), nullable=False)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    author: Mapped[str | None] = mapped_column(String(100), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    local_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_model: Mapped[str | None] = mapped_column(String(50), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class SentimentDaily(Base):
    """Per (ticker, NYSE session, source) aggregate. Rebuildable from raw."""

    __tablename__ = "sentiment_daily"
    __table_args__ = (
        UniqueConstraint("ticker", "session_date", "source", name="uq_sentiment_daily"),
        Index("ix_sentiment_daily_source_date", "source", "session_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    mean_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    weighted_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_std: Mapped[float | None] = mapped_column(Float, nullable=True)
    unique_authors: Mapped[int] = mapped_column(Integer, nullable=False)
    sentiment_zscore: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_zscore: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SentimentCursor(Base):
    """Last-fetched position per source (or per source:channel for Discord).

    position is an ISO-8601 UTC datetime string; a failed cycle leaves it
    unadvanced so collection gaps stay visible.
    """

    __tablename__ = "sentiment_cursors"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    position: Mapped[str] = mapped_column(String(50), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
