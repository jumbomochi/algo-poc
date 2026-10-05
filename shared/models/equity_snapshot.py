from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (
        Index("ix_equity_portfolio_date", "portfolio", "date", unique=True),
        # Not unique: a Tuesday run after a US Monday holiday, or a weekend
        # catch-up, legitimately re-values the previous session (KAN-103).
        Index(
            "ix_equity_portfolio_session_date", "portfolio", "session_date"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio: Mapped[str] = mapped_column(String(50), nullable=False)
    # The SGT wall-clock date of the run that wrote the row. NOT the US
    # session it valued: at 04:15 SGT the row dated Tuesday holds Monday's
    # close. Readers that mean "a session" read ``session_date``.
    date: Mapped[date] = mapped_column(Date, nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    market_value: Mapped[float] = mapped_column(Float, nullable=False)
    base_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    trading_currency: Mapped[str | None] = mapped_column(
        String(8), nullable=True
    )
    equity_trading: Mapped[float | None] = mapped_column(Float, nullable=True)
    cash_trading: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_value_trading: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    fx_base_per_trading: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    equity_base: Mapped[float | None] = mapped_column(Float, nullable=True)
    valuation_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The US NYSE session whose closing prices marked the book (KAN-103),
    # stamped from the bars actually priced. NULL when the run valued a
    # session that had not closed (partial bar), and on rows written before
    # the column existed until the operator backfill fills them.
    session_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
