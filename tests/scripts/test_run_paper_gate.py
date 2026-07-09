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

    def test_sells_are_never_gated(self, state):
        """Exits always process, matching the backtest runner."""
        state.record_fill(portfolio="test_sleeve", ticker="AAPL", action="buy",
                          quantity=5.0, price=100.0, fill_date=date(2026, 7, 1))

        def sell_fn(ticker, bars):
            return {"action": "sell", "limit_price": 110.0, "quantity": 0}

        signals = run_daily(state, build_portfolio(sell_fn), {"AAPL": make_bars(110.0)})

        assert any(s["action"] == "sell" for s in signals)
        assert state.get_positions("test_sleeve") == {}
        assert state.get_cash("test_sleeve") == pytest.approx(10_000.0 + 50.0)

    def test_rejected_signal_not_published(self, state):
        """Signals the sleeve gate rejects must not reach signals_generated
        (and therefore never get published to the pipeline)."""

        def greedy_fn(ticker, bars):
            return {"action": "buy", "limit_price": 100.0, "quantity": 200.0}

        # First run fills up to the position limit; second run's re-entry is
        # blocked by max_lots_per_ticker=1 and must not appear in signals.
        portfolios = build_portfolio(greedy_fn)
        run_daily(state, portfolios, {"AAPL": make_bars()})
        signals = run_daily(state, portfolios, {"AAPL": make_bars()})
        assert signals == []
