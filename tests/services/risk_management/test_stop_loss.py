from __future__ import annotations

import pytest

from services.risk_management.engine import PortfolioState, RiskDecision, RiskEngine


def make_portfolio(nav: float = 100_000, peak_nav: float | None = None) -> PortfolioState:
    return PortfolioState(
        nav=nav,
        peak_nav=peak_nav if peak_nav is not None else nav,
        positions={},
        sector_exposure={},
        total_exposure_pct=0.0,
        margin_utilization_pct=0.0,
    )


class TestTrailingStopLoss:
    def test_stop_loss_triggers_at_threshold(self):
        """Price drops exactly at the trailing stop percentage -> trigger."""
        engine = RiskEngine(stop_loss_trailing_pct=15.0)
        decision = engine.check_stop_loss(
            ticker="AAPL",
            current_price=85.0,
            highest_price_since_entry=100.0,
            stop_loss_trailing_pct=15.0,
        )
        assert decision.approved is False
        assert "stop-loss" in decision.reason.lower() or "stop" in decision.reason.lower()

    def test_stop_loss_triggers_beyond_threshold(self):
        """Price drops more than the trailing stop percentage -> trigger."""
        engine = RiskEngine(stop_loss_trailing_pct=15.0)
        decision = engine.check_stop_loss(
            ticker="AAPL",
            current_price=75.0,
            highest_price_since_entry=100.0,
            stop_loss_trailing_pct=15.0,
        )
        assert decision.approved is False

    def test_stop_loss_does_not_trigger_within_limit(self):
        """Price drops less than the trailing stop -> no trigger."""
        engine = RiskEngine(stop_loss_trailing_pct=15.0)
        decision = engine.check_stop_loss(
            ticker="AAPL",
            current_price=90.0,
            highest_price_since_entry=100.0,
            stop_loss_trailing_pct=15.0,
        )
        assert decision.approved is True

    def test_stop_loss_uses_engine_default(self):
        """When no explicit pct is passed, engine uses its configured default."""
        engine = RiskEngine(stop_loss_trailing_pct=10.0)
        # 10% drop from 100 -> current = 90 should trigger at 10%
        decision = engine.check_stop_loss(
            ticker="AAPL",
            current_price=90.0,
            highest_price_since_entry=100.0,
        )
        assert decision.approved is False

    def test_stop_loss_no_trigger_at_high(self):
        """Price at highest -> no trigger."""
        engine = RiskEngine(stop_loss_trailing_pct=15.0)
        decision = engine.check_stop_loss(
            ticker="AAPL",
            current_price=100.0,
            highest_price_since_entry=100.0,
        )
        assert decision.approved is True


class TestPortfolioDrawdown:
    def test_drawdown_pause_at_10_percent(self):
        """10% drawdown from peak NAV should pause new buys."""
        engine = RiskEngine(drawdown_pause_pct=10.0, drawdown_circuit_breaker_pct=20.0)
        portfolio = make_portfolio(nav=90_000, peak_nav=100_000)
        decision = engine.check_portfolio_drawdown(portfolio)
        assert decision.approved is False
        assert "drawdown" in decision.reason.lower()

    def test_circuit_breaker_at_20_percent(self):
        """20% drawdown from peak NAV should trigger circuit breaker."""
        engine = RiskEngine(drawdown_pause_pct=10.0, drawdown_circuit_breaker_pct=20.0)
        portfolio = make_portfolio(nav=80_000, peak_nav=100_000)
        decision = engine.check_portfolio_drawdown(portfolio)
        assert decision.approved is False
        assert "circuit breaker" in decision.reason.lower()

    def test_drawdown_within_limits(self):
        """5% drawdown -> no action."""
        engine = RiskEngine(drawdown_pause_pct=10.0, drawdown_circuit_breaker_pct=20.0)
        portfolio = make_portfolio(nav=95_000, peak_nav=100_000)
        decision = engine.check_portfolio_drawdown(portfolio)
        assert decision.approved is True

    def test_no_drawdown(self):
        """NAV at peak -> no drawdown."""
        engine = RiskEngine(drawdown_pause_pct=10.0, drawdown_circuit_breaker_pct=20.0)
        portfolio = make_portfolio(nav=100_000, peak_nav=100_000)
        decision = engine.check_portfolio_drawdown(portfolio)
        assert decision.approved is True

    def test_circuit_breaker_has_higher_precedence_than_pause(self):
        """At 25% drawdown, circuit breaker reason should appear, not just pause."""
        engine = RiskEngine(drawdown_pause_pct=10.0, drawdown_circuit_breaker_pct=20.0)
        portfolio = make_portfolio(nav=75_000, peak_nav=100_000)
        decision = engine.check_portfolio_drawdown(portfolio)
        assert decision.approved is False
        assert "circuit breaker" in decision.reason.lower()


class TestDrawdownOnBookEquity:
    """Drawdown must measure real marked book equity, not the deployment
    budget. When book_equity/book_peak_equity are populated they take
    precedence over nav/peak_nav (which, on the live path, is the capped
    deployable_capital and reads a phantom ~0% drawdown)."""

    def test_book_equity_takes_precedence_over_nav(self):
        """nav pinned at the cap reads no drawdown, but a real 15% book-equity
        drop must engage the pause."""
        engine = RiskEngine(drawdown_pause_pct=10.0, drawdown_circuit_breaker_pct=20.0)
        # nav == peak_nav (deployable_capital pinned at cap -> phantom 0%)
        portfolio = make_portfolio(nav=100_000, peak_nav=100_000)
        portfolio.book_equity = 85_000
        portfolio.book_peak_equity = 100_000
        decision = engine.check_portfolio_drawdown(portfolio)
        assert decision.approved is False
        assert "drawdown" in decision.reason.lower()

    def test_book_equity_engages_circuit_breaker(self):
        """A real 20% book-equity drawdown engages the breaker even when nav
        shows none."""
        engine = RiskEngine(drawdown_pause_pct=10.0, drawdown_circuit_breaker_pct=20.0)
        portfolio = make_portfolio(nav=100_000, peak_nav=100_000)
        portfolio.book_equity = 80_000
        portfolio.book_peak_equity = 100_000
        decision = engine.check_portfolio_drawdown(portfolio)
        assert decision.approved is False
        assert "circuit breaker" in decision.reason.lower()

    def test_book_equity_within_limits_approves(self):
        """Real book equity only 5% below peak -> approved, regardless of nav."""
        engine = RiskEngine(drawdown_pause_pct=10.0, drawdown_circuit_breaker_pct=20.0)
        portfolio = make_portfolio(nav=100_000, peak_nav=100_000)
        portfolio.book_equity = 95_000
        portfolio.book_peak_equity = 100_000
        decision = engine.check_portfolio_drawdown(portfolio)
        assert decision.approved is True

    def test_falls_back_to_nav_when_book_equity_absent(self):
        """With no book_equity populated, behavior is unchanged (nav/peak_nav)."""
        engine = RiskEngine(drawdown_pause_pct=10.0, drawdown_circuit_breaker_pct=20.0)
        portfolio = make_portfolio(nav=85_000, peak_nav=100_000)
        assert portfolio.book_equity is None
        decision = engine.check_portfolio_drawdown(portfolio)
        assert decision.approved is False
        assert "drawdown" in decision.reason.lower()


class TestDecisionPrecedence:
    """Verify the documented precedence order:
    1. Kill switch / circuit breaker
    2. Critical margin protection
    3. Stop-loss exits
    4. Hard compliance constraints
    5. Soft/advisory controls
    """

    def test_circuit_breaker_takes_precedence_over_drawdown_pause(self):
        """Circuit breaker (level 1) should override drawdown pause (level 4)."""
        engine = RiskEngine(drawdown_pause_pct=10.0, drawdown_circuit_breaker_pct=20.0)
        # 20% drawdown matches circuit breaker
        portfolio = make_portfolio(nav=80_000, peak_nav=100_000)
        decision = engine.check_portfolio_drawdown(portfolio)
        assert decision.approved is False
        assert "circuit breaker" in decision.reason.lower()

    def test_stop_loss_triggers_exit(self):
        """Stop-loss exit is an independent check that triggers sell decisions."""
        engine = RiskEngine(stop_loss_trailing_pct=15.0)
        decision = engine.check_stop_loss(
            ticker="AAPL",
            current_price=84.0,
            highest_price_since_entry=100.0,
        )
        assert decision.approved is False
        assert "stop" in decision.reason.lower()

    def test_entry_compliance_checks_order(self):
        """Total exposure rejection should take precedence over sector checks."""
        engine = RiskEngine(
            position_entry_limit_pct=5.0,
            sector_concentration_pct=20.0,
            total_exposure_limit_pct=150.0,
        )
        portfolio = make_portfolio(nav=100_000)
        portfolio.total_exposure_pct = 160.0
        portfolio.sector_exposure = {"Technology": 25.0}
        decision = engine.check_entry(
            "AAPL", quantity=10, price=100.0, sector="Technology", portfolio=portfolio
        )
        assert decision.approved is False
        assert "exposure" in decision.reason.lower()
