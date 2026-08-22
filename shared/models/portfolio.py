from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    Index,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_ticker_status", "ticker", "status"),
        Index("ix_positions_portfolio", "portfolio"),
        Index("ix_positions_account_contract", "account_id", "con_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    portfolio: Mapped[str] = mapped_column(String(50), nullable=False)
    con_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    avg_entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    current_price: Mapped[float] = mapped_column(Float, nullable=False)
    peak_price: Mapped[float] = mapped_column(Float, nullable=False)
    sector: Mapped[str | None] = mapped_column(String(50), nullable=True)
    highest_price_since_entry: Mapped[float] = mapped_column(Float, nullable=False)
    entry_signals: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="open"
    )


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trade_ticker_executed", "ticker", "executed_at"),
        Index("ix_trade_recommendation", "recommendation_id"),
        Index("ix_trade_portfolio", "portfolio"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    portfolio: Mapped[str] = mapped_column(String(50), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    order_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 255 to match the same identifier on order_intents and execution_fills
    # (shared/models/order_ledger.py:52 and :110). At String(50) this column
    # rejected every account-scoped id and crash-looped the projector (KAN-61).
    recommendation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Text, not a wider String: this is populated from OrderIntent.reason, which
    # is itself Text and unbounded, so any varchar bound here is a guess.
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    pnl: Mapped[float] = mapped_column(Float, nullable=False)
    entry_signals: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    bar_features: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
