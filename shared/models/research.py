from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class ResearchCandidate(Base):
    __tablename__ = "research_candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    portfolio: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    as_of: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    raw_signal: Mapped[dict] = mapped_column(JSON, nullable=False)
    factor_values: Mapped[dict] = mapped_column(JSON, nullable=False)
    provenance: Mapped[dict] = mapped_column(JSON, nullable=False)
    risk_approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    risk_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
