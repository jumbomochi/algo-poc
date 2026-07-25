from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class CurrencyConversion(Base):
    __tablename__ = "currency_conversions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String(50), nullable=False)
    source_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    source_amount: Mapped[float] = mapped_column(Float, nullable=False)
    target_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    target_amount: Mapped[float] = mapped_column(Float, nullable=False)
    fx_base_per_trading: Mapped[float] = mapped_column(Float, nullable=False)
    fee_amount: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    fee_currency: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    operator: Mapped[str | None] = mapped_column(String(100), nullable=True)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
