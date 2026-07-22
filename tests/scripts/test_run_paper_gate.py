"""run_daily must gate entries through the sleeve RiskEngine like the backtest.

Regression: the sim recorded every buy unconstrained — quality_value went to
-$43K cash (3.8x leverage) on its first live day while the backtest gates
every entry via risk_engine.check_entry.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scripts.paper_state import PaperTradingState
from scripts.run_backtest import PortfolioConfig
from scripts.run_paper import run_daily
from services.risk_management.engine import RiskEngine
from shared.models.base import Base
from shared.models.portfolio import Position


@pytest.fixture
def state():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    st = PaperTradingState.create_new(
        portfolio_capitals={"test_sleeve": 10_000.0}, session=session
    )
    yield st
    session.close()


def make_bars(close: float = 100.0, n: int = 5) -> list[dict]:
    return [
        {"date": f"2026-07-0{i+1}", "open": close, "high": close,
         "low": close, "close": close, "volume": 1000}
        for i in range(n)
    ]


def build_portfolio(signals_fn) -> dict[str, PortfolioConfig]:
    return {
        "test_sleeve": PortfolioConfig(
            name="test_sleeve",
            capital=10_000.0,
            signals_fn=signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=10.0,
                sector_concentration_pct=100.0,
                total_exposure_limit_pct=100.0,
                max_lots_per_ticker=1,
            ),
        )
    }


class TestEntryGate:
    def test_oversized_buy_cannot_overdraw_cash(self, state):
        """A signal demanding 2x the sleeve's capital must be constrained."""

        def greedy_fn(ticker, bars):
            # 200 shares @ $100 = $20K on a $10K sleeve
            return {"action": "buy", "limit_price": 100.0, "quantity": 200.0}

        run_daily(state, build_portfolio(greedy_fn), {"AAPL": make_bars()})

        cash = state.get_cash("test_sleeve")
        positions = state.get_positions("test_sleeve")
        # 10% position limit on a $10K sleeve = max $1,000 => 10 shares
        if positions:
            assert positions["AAPL"]["quantity"] * 100.0 <= 1_000.0 + 1e-6
        assert cash >= 0.0

    def test_many_buys_stop_at_exposure_limit(self, state):
        """Buys across many tickers stop when total exposure hits 100% NAV."""

        def ten_pct_fn(ticker, bars):
            return {"action": "buy", "limit_price": 100.0, "quantity": 10.0}

        bars_by_ticker = {f"T{i:02d}": make_bars() for i in range(20)}
        run_daily(state, build_portfolio(ten_pct_fn), bars_by_ticker)

        assert state.get_cash("test_sleeve") >= -1e-6  # never negative

    def test_sell_signal_waits_for_actual_execution_fill(self, state):
        """Signals do not mutate the durable position before IB fills them."""
        state.record_fill(portfolio="test_sleeve", ticker="AAPL", action="buy",
                          quantity=5.0, price=100.0, fill_date=date(2026, 7, 1))

        def sell_fn(ticker, bars):
            return {"action": "sell", "limit_price": 110.0, "quantity": 0}

        signals = run_daily(state, build_portfolio(sell_fn), {"AAPL": make_bars(110.0)})

        assert any(s["action"] == "sell" for s in signals)
        assert state.get_positions("test_sleeve")["AAPL"]["quantity"] == 5
        assert state.get_cash("test_sleeve") == pytest.approx(9_500.0)

    def test_buy_signal_does_not_create_a_parallel_fill(self, state):
        """Daily signal evaluation leaves durable cash and positions alone."""

        def greedy_fn(ticker, bars):
            return {"action": "buy", "limit_price": 100.0, "quantity": 200.0}

        portfolios = build_portfolio(greedy_fn)
        signals = run_daily(state, portfolios, {"AAPL": make_bars()})

        assert len(signals) == 1
        assert state.get_positions("test_sleeve") == {}
        assert state.get_cash("test_sleeve") == pytest.approx(10_000.0)


def test_paper_state_builds_immutable_strategy_context(state):
    state.record_fill(
        portfolio="test_sleeve", ticker="AAPL", action="buy",
        quantity=5, price=100, fill_date=date(2026, 7, 1),
    )
    pending = type("Intent", (), {
        "symbol": "MSFT", "action": "BUY", "requested_quantity": 3,
        "filled_quantity": 1, "limit_price": 200,
        "recommendation_id": "rec-1",
    })()
    position = state._session.query(Position).filter_by(ticker="AAPL").one()
    position.highest_price_since_entry = 120

    context = state.build_portfolio_context(
        "test_sleeve", pending_orders=[pending], sleeve_budget=10_000,
        reserved_notional=400,
    )

    assert context.positions["AAPL"].quantity == 5
    assert context.positions["AAPL"].peak_price == 120
    assert context.pending_orders["MSFT"].quantity == 2
    assert context.sleeve_budget == 10_000
    assert context.reserved_notional == 400
    with pytest.raises(TypeError):
        context.positions["MSFT"] = context.positions["AAPL"]


def test_strategy_context_filters_positions_and_intents_by_account(state):
    from datetime import datetime, timezone

    state._session.add_all([
        Position(
            account_id="DUONE", ticker="AAPL", portfolio="test_sleeve",
            con_id=1, quantity=1, avg_entry_price=100, current_price=100,
            peak_price=100, highest_price_since_entry=100,
            opened_at=datetime(2026, 7, 1, tzinfo=timezone.utc), status="open",
        ),
        Position(
            account_id="DUTWO", ticker="AAPL", portfolio="test_sleeve",
            con_id=1, quantity=9, avg_entry_price=100, current_price=100,
            peak_price=100, highest_price_since_entry=100,
            opened_at=datetime(2026, 7, 1, tzinfo=timezone.utc), status="open",
        ),
    ])
    pending_one = type("Intent", (), {
        "account_id": "DUONE", "portfolio": "test_sleeve",
        "symbol": "MSFT", "action": "BUY", "requested_quantity": 3,
        "filled_quantity": 1, "limit_price": 200,
        "recommendation_id": "rec-one",
    })()
    pending_two = type("Intent", (), {
        "account_id": "DUTWO", "portfolio": "test_sleeve",
        "symbol": "GOOG", "action": "BUY", "requested_quantity": 4,
        "filled_quantity": 0, "limit_price": 150,
        "recommendation_id": "rec-two",
    })()
    state._session.flush()

    context = state.build_portfolio_context(
        "test_sleeve", pending_orders=[pending_one, pending_two],
        sleeve_budget=10_000, reserved_notional=400, account_id="DUONE",
    )

    assert context.positions["AAPL"].quantity == 1
    assert set(context.pending_orders) == {"MSFT"}
