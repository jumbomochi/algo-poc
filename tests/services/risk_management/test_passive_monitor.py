from __future__ import annotations

import pytest

from services.risk_management.passive_monitor import BreachAction, PassiveBreachMonitor
from services.risk_management.engine import PortfolioState
from shared.config import RiskConfig


def make_portfolio(
    nav: float = 100_000,
    positions: dict | None = None,
    margin_utilization_pct: float = 0.0,
) -> PortfolioState:
    return PortfolioState(
        nav=nav,
        peak_nav=nav,
        positions=positions or {},
        sector_exposure={},
        total_exposure_pct=0.0,
        margin_utilization_pct=margin_utilization_pct,
    )


@pytest.fixture()
def default_config() -> RiskConfig:
    return RiskConfig(
        soft_ceiling_pct=7.0,
        hard_ceiling_pct=15.0,
        margin_warning_pct=70.0,
        margin_critical_pct=85.0,
    )


class TestPositionCeilings:
    def test_position_below_soft_ceiling_no_breach(self, default_config):
        """Position at 5% of NAV (below 7% soft) -> no breach actions."""
        monitor = PassiveBreachMonitor(config=default_config)
        portfolio = make_portfolio(
            nav=100_000,
            positions={"AAPL": {"quantity": 50, "sector": "Technology"}},
        )
        current_prices = {"AAPL": 100.0}  # 50 * 100 = $5000 = 5% of NAV
        breaches = monitor.scan_positions(portfolio, current_prices)
        ticker_breaches = [b for b in breaches if b.ticker == "AAPL"]
        assert len(ticker_breaches) == 0

    def test_position_above_soft_below_hard_notifies(self, default_config):
        """Position at 10% of NAV (above 7% soft, below 15% hard) -> notify only."""
        monitor = PassiveBreachMonitor(config=default_config)
        portfolio = make_portfolio(
            nav=100_000,
            positions={"AAPL": {"quantity": 100, "sector": "Technology"}},
        )
        current_prices = {"AAPL": 100.0}  # 100 * 100 = $10000 = 10% of NAV
        breaches = monitor.scan_positions(portfolio, current_prices)
        ticker_breaches = [b for b in breaches if b.ticker == "AAPL"]
        assert len(ticker_breaches) == 1
        assert ticker_breaches[0].action_type == "notify"
        assert ticker_breaches[0].current_pct == pytest.approx(10.0)

    def test_position_above_hard_ceiling_trims(self, default_config):
        """Position at 20% of NAV (above 15% hard) -> trim to 7%."""
        monitor = PassiveBreachMonitor(config=default_config)
        portfolio = make_portfolio(
            nav=100_000,
            positions={"AAPL": {"quantity": 200, "sector": "Technology"}},
        )
        current_prices = {"AAPL": 100.0}  # 200 * 100 = $20000 = 20% of NAV
        breaches = monitor.scan_positions(portfolio, current_prices)
        ticker_breaches = [b for b in breaches if b.ticker == "AAPL"]
        assert len(ticker_breaches) == 1
        assert ticker_breaches[0].action_type == "trim"
        assert ticker_breaches[0].target_pct == 7.0
        assert ticker_breaches[0].current_pct == pytest.approx(20.0)

    def test_position_exactly_at_soft_ceiling_notifies(self, default_config):
        """Position at exactly 7% -> soft notify."""
        monitor = PassiveBreachMonitor(config=default_config)
        portfolio = make_portfolio(
            nav=100_000,
            positions={"AAPL": {"quantity": 70, "sector": "Technology"}},
        )
        current_prices = {"AAPL": 100.0}  # 70 * 100 = $7000 = 7%
        breaches = monitor.scan_positions(portfolio, current_prices)
        ticker_breaches = [b for b in breaches if b.ticker == "AAPL"]
        assert len(ticker_breaches) == 1
        assert ticker_breaches[0].action_type == "notify"

    def test_multiple_positions_scanned(self, default_config):
        """Multiple positions, each checked independently."""
        monitor = PassiveBreachMonitor(config=default_config)
        portfolio = make_portfolio(
            nav=100_000,
            positions={
                "AAPL": {"quantity": 100, "sector": "Technology"},  # 10% -> notify
                "MSFT": {"quantity": 50, "sector": "Technology"},   # 5% -> ok
                "TSLA": {"quantity": 200, "sector": "Consumer"},    # 20% -> trim
            },
        )
        current_prices = {"AAPL": 100.0, "MSFT": 100.0, "TSLA": 100.0}
        breaches = monitor.scan_positions(portfolio, current_prices)
        tickers = {b.ticker for b in breaches}
        assert "AAPL" in tickers
        assert "MSFT" not in tickers
        assert "TSLA" in tickers


class TestMarginUtilization:
    def test_margin_warning_at_70_percent(self, default_config):
        """Margin utilization at 70% -> warning breach."""
        monitor = PassiveBreachMonitor(config=default_config)
        portfolio = make_portfolio(nav=100_000, margin_utilization_pct=70.0)
        current_prices = {}
        breaches = monitor.scan_positions(portfolio, current_prices)
        margin_breaches = [b for b in breaches if b.ticker == "__margin__"]
        assert len(margin_breaches) == 1
        assert margin_breaches[0].action_type == "notify"
        assert "margin" in margin_breaches[0].message.lower()

    def test_margin_critical_at_85_percent(self, default_config):
        """Margin utilization at 85% -> critical breach."""
        monitor = PassiveBreachMonitor(config=default_config)
        portfolio = make_portfolio(nav=100_000, margin_utilization_pct=85.0)
        current_prices = {}
        breaches = monitor.scan_positions(portfolio, current_prices)
        margin_breaches = [b for b in breaches if b.ticker == "__margin__"]
        assert len(margin_breaches) == 1
        assert margin_breaches[0].action_type == "trim"
        assert "margin" in margin_breaches[0].message.lower()

    def test_margin_below_warning_no_breach(self, default_config):
        """Margin at 50% -> no breach."""
        monitor = PassiveBreachMonitor(config=default_config)
        portfolio = make_portfolio(nav=100_000, margin_utilization_pct=50.0)
        current_prices = {}
        breaches = monitor.scan_positions(portfolio, current_prices)
        margin_breaches = [b for b in breaches if b.ticker == "__margin__"]
        assert len(margin_breaches) == 0

    def test_margin_between_warning_and_critical(self, default_config):
        """Margin at 75% -> warning (notify), not critical."""
        monitor = PassiveBreachMonitor(config=default_config)
        portfolio = make_portfolio(nav=100_000, margin_utilization_pct=75.0)
        current_prices = {}
        breaches = monitor.scan_positions(portfolio, current_prices)
        margin_breaches = [b for b in breaches if b.ticker == "__margin__"]
        assert len(margin_breaches) == 1
        assert margin_breaches[0].action_type == "notify"


class TestBreachActionDataclass:
    def test_breach_action_fields(self):
        b = BreachAction(
            ticker="AAPL",
            action_type="notify",
            target_pct=7.0,
            current_pct=10.0,
            message="Soft ceiling breach",
        )
        assert b.ticker == "AAPL"
        assert b.action_type == "notify"
        assert b.target_pct == 7.0
        assert b.current_pct == 10.0
        assert b.message == "Soft ceiling breach"
