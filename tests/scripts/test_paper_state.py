"""Tests for ``scripts/paper_state.py`` fill accounting.

Regression coverage for the quantity=0 sell bug: every sell signal emitted by
the signal functions hard-codes ``"quantity": 0`` (meaning "close the full
position", matching the backtest runner's semantics), but ``record_fill``
previously used that 0 for P&L and cash credit — recording $0 P&L and
returning $0 cash on every paper sell.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from scripts.paper_state import PaperTradingState
from shared.models.base import Base
from shared.models.portfolio import Position, Trade
from shared.models.portfolio_config import PortfolioConfig


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture
def state(session):
    return PaperTradingState.create_new(
        portfolio_capitals={"momentum": 10_000.0}, session=session
    )


def get_cash(session) -> float:
    return session.execute(
        select(PortfolioConfig.cash).where(PortfolioConfig.portfolio == "momentum")
    ).scalar_one()


def buy(state, qty: float = 10.0, price: float = 100.0) -> None:
    state.record_fill(
        portfolio="momentum",
        ticker="AAPL",
        action="buy",
        quantity=qty,
        price=price,
        fill_date=date(2026, 7, 1),
    )


class TestSellAccounting:
    def test_sell_with_quantity_zero_closes_full_position(self, state, session):
        """quantity=0 (every signal fn's sell) must sell the entire held lot."""
        buy(state, qty=10.0, price=100.0)
        assert get_cash(session) == pytest.approx(9_000.0)

        state.record_fill(
            portfolio="momentum",
            ticker="AAPL",
            action="sell",
            quantity=0,
            price=110.0,
            fill_date=date(2026, 7, 2),
        )

        trade = session.execute(select(Trade)).scalar_one()
        assert trade.quantity == pytest.approx(10.0)
        assert trade.pnl == pytest.approx(100.0)  # (110 - 100) * 10
        assert get_cash(session) == pytest.approx(10_100.0)  # 9000 + 110*10
        assert session.execute(select(Position)).scalar_one_or_none() is None

    def test_explicit_partial_sell_decrements_position(self, state, session):
        buy(state, qty=10.0, price=100.0)

        state.record_fill(
            portfolio="momentum",
            ticker="AAPL",
            action="sell",
            quantity=4.0,
            price=110.0,
            fill_date=date(2026, 7, 2),
        )

        pos = session.execute(select(Position)).scalar_one()
        assert pos.quantity == pytest.approx(6.0)
        trade = session.execute(select(Trade)).scalar_one()
        assert trade.quantity == pytest.approx(4.0)
        assert trade.pnl == pytest.approx(40.0)
        assert get_cash(session) == pytest.approx(9_000.0 + 440.0)

    def test_oversized_sell_clamps_to_held_quantity(self, state, session):
        buy(state, qty=10.0, price=100.0)

        state.record_fill(
            portfolio="momentum",
            ticker="AAPL",
            action="sell",
            quantity=999.0,
            price=110.0,
            fill_date=date(2026, 7, 2),
        )

        trade = session.execute(select(Trade)).scalar_one()
        assert trade.quantity == pytest.approx(10.0)
        assert session.execute(select(Position)).scalar_one_or_none() is None
        assert get_cash(session) == pytest.approx(10_100.0)

    def test_round_trip_preserves_equity_at_flat_price(self, state, session):
        """Buy then sell at the same price must return cash to exactly initial."""
        buy(state, qty=7.5, price=200.0)  # fractional shares
        state.record_fill(
            portfolio="momentum",
            ticker="AAPL",
            action="sell",
            quantity=0,
            price=200.0,
            fill_date=date(2026, 7, 2),
        )
        assert get_cash(session) == pytest.approx(10_000.0)

    def test_sell_without_position_is_noop(self, state, session):
        state.record_fill(
            portfolio="momentum",
            ticker="AAPL",
            action="sell",
            quantity=0,
            price=110.0,
            fill_date=date(2026, 7, 2),
        )
        assert session.execute(select(Trade)).scalar_one_or_none() is None
        assert get_cash(session) == pytest.approx(10_000.0)


class TestBuyAccounting:
    def test_buy_debits_cash_and_opens_position(self, state, session):
        buy(state, qty=10.0, price=100.0)
        pos = session.execute(select(Position)).scalar_one()
        assert pos.quantity == pytest.approx(10.0)
        assert pos.avg_entry_price == pytest.approx(100.0)
        assert get_cash(session) == pytest.approx(9_000.0)

    def test_second_buy_averages_entry_price(self, state, session):
        buy(state, qty=10.0, price=100.0)
        buy(state, qty=10.0, price=120.0)
        pos = session.execute(select(Position)).scalar_one()
        assert pos.quantity == pytest.approx(20.0)
        assert pos.avg_entry_price == pytest.approx(110.0)
        assert get_cash(session) == pytest.approx(10_000.0 - 1000.0 - 1200.0)
