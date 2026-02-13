from datetime import datetime

from sqlalchemy import DateTime, Float, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class SignalRecord(Base):
    __tablename__ = "signal_records"
    __table_args__ = (
        Index(
            "ix_signal_ticker_name_computed", "ticker", "signal_name", "computed_at"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    signal_name: Mapped[str] = mapped_column(String(50), nullable=False)
    signal_value: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
