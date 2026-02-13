from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class FundamentalRecord(Base):
    __tablename__ = "fundamental_records"
    __table_args__ = (
        Index("ix_fundamental_ticker_type_effective", "ticker", "metric_type", "effective_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    metric_type: Mapped[str] = mapped_column(String(50), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source_revision: Mapped[str] = mapped_column(String(100), nullable=False)
