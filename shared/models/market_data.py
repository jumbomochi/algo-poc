from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Float, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class OHLCVDaily(Base):
    __tablename__ = "ohlcv_daily"
    __table_args__ = (
        Index("ix_ohlcv_ticker_date", "ticker", "date", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
