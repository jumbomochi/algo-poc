#!/usr/bin/env python3
"""Paper trading state persistence backed by PostgreSQL.

Manages position tracking, trade history, equity snapshots, and
per-portfolio capital/cash via SQLAlchemy models.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backtest.portfolio_context import HeldPosition, PendingOrder, PortfolioContext
from shared.models.portfolio import Position, Trade
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.portfolio_config import PortfolioConfig

# The eight currency-qualified columns on ``equity_snapshots``. NULL on every
# row written before KAN-44, and on any row written without an FX rate.
CURRENCY_COLUMNS: tuple[str, ...] = (
    "base_currency",
    "trading_currency",
    "equity_trading",
    "cash_trading",
    "market_value_trading",
    "fx_base_per_trading",
    "equity_base",
    "valuation_at",
)


def _currency_context(
    equity: float,
    cash: float,
    market_value: float,
    *,
    base_currency: str | None,
    trading_currency: str | None,
    fx_base_per_trading: float | None,
    valuation_at: datetime | None,
) -> dict[str, Any]:
    """Derive the eight currency columns, or all-NULL when no rate is given.

    A malformed rate raises rather than falling back to NULL: a silently
    unstamped row is indistinguishable from a legacy one, and the gap would
    only surface on gate day.
    """
    if fx_base_per_trading is None:
        return dict.fromkeys(CURRENCY_COLUMNS, None)
    if not math.isfinite(fx_base_per_trading) or fx_base_per_trading <= 0:
        raise ValueError(
            f"fx_base_per_trading must be finite and positive, "
            f"got {fx_base_per_trading!r}"
        )
    if not base_currency or not trading_currency:
        raise ValueError(
            "base_currency and trading_currency are required when "
            "fx_base_per_trading is supplied — a rate without the pair it "
            "converts between is not interpretable"
        )
    return {
        "base_currency": base_currency,
        "trading_currency": trading_currency,
        "equity_trading": equity,
        "cash_trading": cash,
        "market_value_trading": market_value,
        "fx_base_per_trading": fx_base_per_trading,
        "equity_base": equity * fx_base_per_trading,
        "valuation_at": valuation_at or datetime.now(timezone.utc),
    }


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

    def _apply_fill_accounting(
        self,
        account_id: str | None,
        portfolio: str,
        ticker: str,
        action: str,
        quantity: float,
        price: float,
        fill_datetime: datetime,
        commission: float = 0.0,
        recommendation_id: str | None = None,
        con_id: int | None = None,
        exchange: str | None = None,
        currency: str | None = None,
        strict_quantity: bool = False,
        entry_signals: dict | None = None,
        bar_features: dict | None = None,
        exit_reason: str | None = None,
        sector: str | None = None,
    ) -> None:
        """Apply already-validated fill economics without committing.

        The fill projector is the production caller.  Locking here keeps cash
        and position changes in the projectors' encompassing transaction.
        """
        now = datetime.now(timezone.utc)
        config = self._session.scalar(
            select(PortfolioConfig)
            .where(PortfolioConfig.portfolio == portfolio)
            .with_for_update()
        )
        if config is None:
            raise ValueError(f"unknown portfolio {portfolio}")

        if strict_quantity:
            if not account_id:
                raise ValueError("fill lacks position account ownership")
            if con_id is None:
                raise ValueError("fill lacks position broker contract identity")
            candidates = list(self._session.scalars(
                select(Position)
                .where(
                    Position.con_id == con_id,
                    Position.status == "open",
                )
                .with_for_update()
            ))
            if any(position.account_id is None for position in candidates):
                raise ValueError("position account ownership is unresolved")
            owned = [
                position for position in candidates
                if position.account_id == account_id
                and position.portfolio == portfolio
                and position.con_id == con_id
            ]
            if len(owned) > 1:
                raise ValueError("position account ownership is ambiguous")
            existing = owned[0] if owned else None
            if existing is not None and existing.ticker != ticker:
                raise ValueError("position broker ticker identity conflicts")
        else:
            candidates = list(self._session.scalars(
                select(Position)
                .where(
                    Position.portfolio == portfolio,
                    Position.ticker == ticker,
                    Position.status == "open",
                )
                .with_for_update()
            ))
            existing = candidates[0] if candidates else None

        if action == "buy":
            cash_delta = -(price * quantity + commission)
            if strict_quantity and config.cash + cash_delta < -1e-9:
                raise ValueError("fill would make sleeve cash negative")

            if existing:
                identity = (existing.con_id, existing.exchange, existing.currency)
                incoming = (con_id, exchange, currency)
                if strict_quantity and identity != incoming:
                    raise ValueError("position broker contract identity conflicts")
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
                    account_id=account_id,
                    ticker=ticker,
                    portfolio=portfolio,
                    quantity=quantity,
                    avg_entry_price=price,
                    current_price=price,
                    peak_price=price,
                    highest_price_since_entry=price,
                    sector=sector,
                    entry_signals=entry_signals,
                    opened_at=fill_datetime,
                    status="open",
                    con_id=con_id,
                    exchange=exchange,
                    currency=currency,
                )
                self._session.add(pos)

            config.cash += cash_delta
            config.updated_at = now

        elif action == "sell":
            pos = existing
            if strict_quantity and (pos is None or quantity > pos.quantity + 1e-9):
                raise ValueError("sell fill exceeds open position quantity")
            if strict_quantity and pos is not None:
                identity = (pos.con_id, pos.exchange, pos.currency)
                incoming = (con_id, exchange, currency)
                if identity != incoming:
                    raise ValueError("position broker contract identity conflicts")

            if pos:
                # Sell signals emit quantity=0 meaning "close the full position"
                # (the backtest runner has the same semantics: it always sells
                # the whole lot). An explicit positive quantity is honoured as a
                # partial sell, clamped to the held quantity.
                sell_qty = pos.quantity if quantity <= 0 else min(quantity, pos.quantity)
                pnl = (price - pos.avg_entry_price) * sell_qty
                trade = Trade(
                    ticker=ticker,
                    portfolio=portfolio,
                    side="sell",
                    quantity=sell_qty,
                    price=price,
                    entry_price=pos.avg_entry_price,
                    entry_date=pos.opened_at.date(),
                    recommendation_id=recommendation_id,
                    exit_reason=exit_reason,
                    pnl=pnl,
                    entry_signals=pos.entry_signals,
                    bar_features=bar_features,
                    commission=commission,
                    slippage=0.0,
                    executed_at=fill_datetime,
                )
                self._session.add(trade)
                if sell_qty >= pos.quantity:
                    self._session.delete(pos)
                else:
                    pos.quantity -= sell_qty
                    pos.current_price = price
                config.cash += price * sell_qty - commission
                config.updated_at = now

        else:
            raise ValueError(f"unsupported fill action {action}")

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
        sector: str | None = None,
    ) -> None:
        """Compatibility helper for historical backtest/training fixtures.

        Live paper execution must flow through ``FillProjector``; this wrapper
        deliberately has no broker identity or idempotency semantics.
        """
        self._apply_fill_accounting(
            account_id=None,
            portfolio=portfolio,
            ticker=ticker,
            action=action,
            quantity=quantity,
            price=price,
            fill_datetime=datetime(
                fill_date.year, fill_date.month, fill_date.day, tzinfo=timezone.utc
            ),
            entry_signals=entry_signals,
            bar_features=bar_features,
            exit_reason=exit_reason,
            sector=sector,
        )

    def update_peak_prices(
        self,
        portfolio: str,
        current_prices: dict[str, float],
        *,
        account_id: str | None = None,
    ) -> None:
        """Update peak prices for all held positions in a portfolio."""
        stmt = select(Position).where(
                Position.portfolio == portfolio,
                Position.status == "open",
            )
        if account_id is not None:
            stmt = stmt.where(Position.account_id == account_id)
        positions = self._session.scalars(stmt).all()

        for pos in positions:
            if pos.ticker in current_prices:
                new_price = current_prices[pos.ticker]
                pos.peak_price = max(pos.peak_price, new_price)
                pos.highest_price_since_entry = max(pos.highest_price_since_entry, new_price)
                pos.current_price = new_price

        self._session.flush()

    def compute_equity(
        self,
        portfolio: str,
        current_prices: dict[str, float],
        *,
        account_id: str | None = None,
    ) -> float:
        """Compute current equity (cash + market value of positions)."""
        cash = self.get_cash(portfolio)
        stmt = select(Position).where(
                Position.portfolio == portfolio,
                Position.status == "open",
            )
        if account_id is not None:
            stmt = stmt.where(Position.account_id == account_id)
        positions = self._session.scalars(stmt).all()

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
        *,
        base_currency: str | None = None,
        trading_currency: str | None = None,
        fx_base_per_trading: float | None = None,
        valuation_at: datetime | None = None,
    ) -> None:
        """Record (or update) an equity snapshot for a portfolio on a date.

        ``equity``/``cash``/``market_value`` are denominated in the trading
        currency (USD): sleeve budgets and bar prices are both USD. Supplying
        ``fx_base_per_trading`` (base per one unit of trading currency, e.g.
        SGD per USD) stamps that currency context onto the row so the D16
        drawdown bound — "measured on USD NAV" — is a checkable claim rather
        than an assumption. Without a rate the eight currency-qualified
        columns stay NULL and the behaviour is exactly as it was before, so
        no existing caller changes.

        The currency columns are rewritten on every update, including being
        cleared when no rate is supplied: a fresh equity figure sitting beside
        a stale rate would be internally inconsistent and indistinguishable
        from a correctly-stamped row.
        """
        currency = _currency_context(
            equity,
            cash,
            market_value,
            base_currency=base_currency,
            trading_currency=trading_currency,
            fx_base_per_trading=fx_base_per_trading,
            valuation_at=valuation_at,
        )
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
            for column, value in currency.items():
                setattr(existing, column, value)
        else:
            snap = EquitySnapshot(
                portfolio=portfolio,
                date=snap_date,
                equity=equity,
                cash=cash,
                market_value=market_value,
                created_at=now,
                **currency,
            )
            self._session.add(snap)

        self._session.flush()

    def get_positions(
        self, portfolio: str, *, account_id: str | None = None
    ) -> dict[str, dict]:
        """Return open positions for a portfolio as {ticker: {...}}."""
        stmt = select(Position).where(
                Position.portfolio == portfolio,
                Position.status == "open",
            )
        if account_id is not None:
            stmt = stmt.where(Position.account_id == account_id)
        rows = self._session.scalars(stmt).all()

        return {
            pos.ticker: {
                "quantity": pos.quantity,
                "avg_entry_price": pos.avg_entry_price,
                "entry_price": pos.avg_entry_price,
                "peak_price": max(pos.peak_price, pos.highest_price_since_entry),
                "entry_date": str(pos.opened_at.date()),
                "entry_signals": pos.entry_signals,
            }
            for pos in rows
        }

    def build_portfolio_context(
        self,
        portfolio: str,
        *,
        pending_orders: list[Any],
        sleeve_budget: float,
        reserved_notional: float,
        account_id: str | None = None,
    ) -> PortfolioContext:
        """Hydrate immutable strategy state from durable fills and intents."""
        positions = {
            ticker: HeldPosition(
                quantity=float(position["quantity"]),
                avg_entry_price=float(position["avg_entry_price"]),
                peak_price=float(position["peak_price"]),
                entry_date=date.fromisoformat(position["entry_date"]),
            )
            for ticker, position in self.get_positions(
                portfolio, account_id=account_id
            ).items()
        }
        pending = {}
        for intent in pending_orders:
            if getattr(intent, "portfolio", portfolio) != portfolio:
                continue
            if (
                account_id is not None
                and getattr(intent, "account_id", account_id) != account_id
            ):
                continue
            remaining = max(
                0.0,
                float(intent.requested_quantity) - float(intent.filled_quantity),
            )
            if remaining <= 0:
                continue
            key = (
                intent.symbol
                if intent.symbol not in pending
                else intent.recommendation_id
            )
            pending[key] = PendingOrder(
                ticker=intent.symbol,
                action=str(intent.action).lower(),
                quantity=remaining,
                limit_price=(
                    float(intent.limit_price)
                    if intent.limit_price is not None
                    else None
                ),
                recommendation_id=intent.recommendation_id,
            )
        return PortfolioContext(
            positions=positions,
            pending_orders=pending,
            sleeve_budget=float(sleeve_budget),
            reserved_notional=float(reserved_notional),
        )

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
        """Return equity snapshots for a portfolio.

        The currency context is carried through as-is — ``None`` on rows
        written without an FX rate — so a reader can tell whether a drawdown
        computed from this series is denominated or merely assumed.
        """
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
                **{name: getattr(s, name) for name in CURRENCY_COLUMNS},
            }
            for s in rows
        ]
