#!/usr/bin/env python3
"""Paper trading state persistence backed by PostgreSQL.

Manages position tracking, trade history, equity snapshots, and
per-portfolio capital/cash via SQLAlchemy models.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from shared.models.portfolio import Position, Trade
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.portfolio_config import PortfolioConfig


class PaperTradingState:
    """Manages paper trading state across multiple portfolios in the DB."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @classmethod
    def create_new(
        cls,
        portfolio_capitals: dict[str, float],
        session: Session,
    ) -> PaperTradingState:
        """Create fresh state with initial capital per portfolio."""
        now = datetime.now(timezone.utc)
        for name, capital in portfolio_capitals.items():
            config = PortfolioConfig(
                portfolio=name,
                capital=capital,
                cash=capital,
                created_at=now,
                updated_at=now,
            )
            session.add(config)
        session.flush()
        return cls(session)

    @classmethod
    def load(cls, session: Session) -> PaperTradingState:
        """Load state from DB. Raises ValueError if no state exists."""
        count = session.execute(
            select(PortfolioConfig.id).limit(1)
        ).scalar()
        if count is None:
            raise ValueError("No paper trading state found. Run with --init first.")
        return cls(session)

    def get_portfolio_names(self) -> list[str]:
        """Return list of portfolio names."""
        rows = self._session.execute(
            select(PortfolioConfig.portfolio).order_by(PortfolioConfig.portfolio)
        ).scalars().all()
        return list(rows)

    def get_cash(self, portfolio: str) -> float:
        """Return current cash for a portfolio."""
        row = self._session.execute(
            select(PortfolioConfig.cash).where(PortfolioConfig.portfolio == portfolio)
        ).scalar_one()
        return float(row)

    def get_capital(self, portfolio: str) -> float:
        """Return initial capital for a portfolio."""
        row = self._session.execute(
            select(PortfolioConfig.capital).where(PortfolioConfig.portfolio == portfolio)
        ).scalar_one()
        return float(row)

    def _update_cash(self, portfolio: str, delta: float) -> None:
        """Adjust cash for a portfolio by delta amount."""
        self._session.execute(
            update(PortfolioConfig)
            .where(PortfolioConfig.portfolio == portfolio)
            .values(
                cash=PortfolioConfig.cash + delta,
                updated_at=datetime.now(timezone.utc),
            )
        )
        self._session.flush()

    def record_fill(
        self,
        portfolio: str,
        ticker: str,
        action: str,
        quantity: float,
        price: float,
        fill_date: date,
        entry_signals: dict | None = None,
        bar_features: dict | None = None,
        exit_reason: str | None = None,
    ) -> None:
        """Record a fill (buy or sell) for a portfolio."""
        now = datetime.now(timezone.utc)

        if action == "buy":
            existing = self._session.execute(
                select(Position).where(
                    Position.portfolio == portfolio,
                    Position.ticker == ticker,
                    Position.status == "open",
                )
            ).scalar_one_or_none()

            if existing:
                old_qty = existing.quantity
                old_price = existing.avg_entry_price
                new_qty = old_qty + quantity
                existing.avg_entry_price = (old_price * old_qty + price * quantity) / new_qty
                existing.quantity = new_qty
                existing.current_price = price
                existing.peak_price = max(existing.peak_price, price)
                existing.highest_price_since_entry = max(existing.highest_price_since_entry, price)
                if entry_signals:
                    existing.entry_signals = entry_signals
            else:
                pos = Position(
                    ticker=ticker,
                    portfolio=portfolio,
                    quantity=quantity,
                    avg_entry_price=price,
                    current_price=price,
                    peak_price=price,
                    highest_price_since_entry=price,
                    entry_signals=entry_signals,
                    opened_at=datetime(fill_date.year, fill_date.month, fill_date.day, tzinfo=timezone.utc),
                    status="open",
                )
                self._session.add(pos)

            self._update_cash(portfolio, -(price * quantity))

        elif action == "sell":
            pos = self._session.execute(
                select(Position).where(
                    Position.portfolio == portfolio,
                    Position.ticker == ticker,
                    Position.status == "open",
                )
            ).scalar_one_or_none()

            if pos:
                pnl = (price - pos.avg_entry_price) * quantity
                trade = Trade(
                    ticker=ticker,
                    portfolio=portfolio,
                    side="sell",
                    quantity=quantity,
                    price=price,
                    entry_price=pos.avg_entry_price,
                    entry_date=pos.opened_at.date(),
                    exit_reason=exit_reason,
                    pnl=pnl,
                    entry_signals=pos.entry_signals,
                    bar_features=bar_features,
                    commission=0.0,
                    slippage=0.0,
                    executed_at=datetime(fill_date.year, fill_date.month, fill_date.day, tzinfo=timezone.utc),
                )
                self._session.add(trade)
                self._session.delete(pos)
                self._update_cash(portfolio, price * quantity)

        self._session.flush()

    def update_peak_prices(
        self, portfolio: str, current_prices: dict[str, float]
    ) -> None:
        """Update peak prices for all held positions in a portfolio."""
        positions = self._session.execute(
            select(Position).where(
                Position.portfolio == portfolio,
                Position.status == "open",
            )
        ).scalars().all()

        for pos in positions:
            if pos.ticker in current_prices:
                new_price = current_prices[pos.ticker]
                pos.peak_price = max(pos.peak_price, new_price)
                pos.highest_price_since_entry = max(pos.highest_price_since_entry, new_price)
                pos.current_price = new_price

        self._session.flush()

    def compute_equity(
        self, portfolio: str, current_prices: dict[str, float]
    ) -> float:
        """Compute current equity (cash + market value of positions)."""
        cash = self.get_cash(portfolio)
        positions = self._session.execute(
            select(Position).where(
                Position.portfolio == portfolio,
                Position.status == "open",
            )
        ).scalars().all()

        market_value = sum(
            pos.quantity * current_prices.get(pos.ticker, pos.avg_entry_price)
            for pos in positions
        )
        return cash + market_value

    def record_equity_snapshot(
        self,
        portfolio: str,
        snap_date: date,
        equity: float,
        cash: float,
        market_value: float,
    ) -> None:
        """Record (or update) an equity snapshot for a portfolio on a date."""
        now = datetime.now(timezone.utc)
        existing = self._session.execute(
            select(EquitySnapshot).where(
                EquitySnapshot.portfolio == portfolio,
                EquitySnapshot.date == snap_date,
            )
        ).scalar_one_or_none()

        if existing:
            existing.equity = equity
            existing.cash = cash
            existing.market_value = market_value
            existing.created_at = now
        else:
            snap = EquitySnapshot(
                portfolio=portfolio,
                date=snap_date,
                equity=equity,
                cash=cash,
                market_value=market_value,
                created_at=now,
            )
            self._session.add(snap)

        self._session.flush()

    def get_positions(self, portfolio: str) -> dict[str, dict]:
        """Return open positions for a portfolio as {ticker: {...}}."""
        rows = self._session.execute(
            select(Position).where(
                Position.portfolio == portfolio,
                Position.status == "open",
            )
        ).scalars().all()

        return {
            pos.ticker: {
                "quantity": pos.quantity,
                "avg_entry_price": pos.avg_entry_price,
                "entry_price": pos.avg_entry_price,
                "peak_price": pos.peak_price,
                "entry_date": str(pos.opened_at.date()),
                "entry_signals": pos.entry_signals,
            }
            for pos in rows
        }

    def get_trades(self, portfolio: str) -> list[dict]:
        """Return completed trades for a portfolio."""
        rows = self._session.execute(
            select(Trade)
            .where(Trade.portfolio == portfolio)
            .order_by(Trade.executed_at)
        ).scalars().all()

        return [
            {
                "ticker": t.ticker,
                "portfolio": t.portfolio,
                "entry_price": t.entry_price,
                "exit_price": t.price,
                "quantity": t.quantity,
                "entry_date": str(t.entry_date),
                "exit_date": str(t.executed_at.date()),
                "pnl": t.pnl,
                "exit_reason": t.exit_reason,
                "entry_signals": t.entry_signals,
                "bar_features": t.bar_features,
            }
            for t in rows
        ]

    def get_all_trades(self) -> list[dict]:
        """Return all completed trades across all portfolios (for ML training)."""
        rows = self._session.execute(
            select(Trade).order_by(Trade.executed_at)
        ).scalars().all()

        return [
            {
                "ticker": t.ticker,
                "portfolio": t.portfolio,
                "entry_price": t.entry_price,
                "exit_price": t.price,
                "quantity": t.quantity,
                "entry_date": str(t.entry_date),
                "exit_date": str(t.executed_at.date()),
                "pnl": t.pnl,
                "exit_reason": t.exit_reason,
                "entry_signals": t.entry_signals,
                "bar_features": t.bar_features,
            }
            for t in rows
        ]

    def get_equity_history(self, portfolio: str) -> list[dict]:
        """Return equity snapshots for a portfolio."""
        rows = self._session.execute(
            select(EquitySnapshot)
            .where(EquitySnapshot.portfolio == portfolio)
            .order_by(EquitySnapshot.date)
        ).scalars().all()

        return [
            {
                "date": str(s.date),
                "equity": s.equity,
                "cash": s.cash,
                "market_value": s.market_value,
            }
            for s in rows
        ]
