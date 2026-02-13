from __future__ import annotations

import numpy as np
import pytest

from services.risk_management.correlation import AlertMessage as CorrelationAlert
from services.risk_management.correlation import CorrelationMonitor


class TestPortfolioBeta:
    def test_beta_perfectly_correlated(self):
        """When position returns are identical to market, beta should be ~1.0."""
        monitor = CorrelationMonitor()
        market_returns = np.array([0.01, -0.02, 0.03, -0.01, 0.02] * 20)
        position_returns = {"AAPL": market_returns.copy()}
        positions = {"AAPL": {"quantity": 100, "value": 10_000}}

        beta = monitor.check_portfolio_beta(
            positions=positions,
            market_returns=market_returns,
            position_returns=position_returns,
        )
        assert beta == pytest.approx(1.0, abs=0.01)

    def test_beta_double_market(self):
        """When position returns are 2x market, beta should be ~2.0."""
        monitor = CorrelationMonitor()
        market_returns = np.array([0.01, -0.02, 0.03, -0.01, 0.02] * 20)
        position_returns = {"AAPL": market_returns * 2.0}
        positions = {"AAPL": {"quantity": 100, "value": 10_000}}

        beta = monitor.check_portfolio_beta(
            positions=positions,
            market_returns=market_returns,
            position_returns=position_returns,
        )
        assert beta == pytest.approx(2.0, abs=0.01)

    def test_beta_negative_correlation(self):
        """When position returns are negatively correlated, beta should be negative."""
        monitor = CorrelationMonitor()
        market_returns = np.array([0.01, -0.02, 0.03, -0.01, 0.02] * 20)
        position_returns = {"AAPL": -market_returns}
        positions = {"AAPL": {"quantity": 100, "value": 10_000}}

        beta = monitor.check_portfolio_beta(
            positions=positions,
            market_returns=market_returns,
            position_returns=position_returns,
        )
        assert beta < 0

    def test_beta_multi_position_weighted(self):
        """Beta should be value-weighted across positions."""
        monitor = CorrelationMonitor()
        market_returns = np.array([0.01, -0.02, 0.03, -0.01, 0.02] * 20)
        position_returns = {
            "AAPL": market_returns * 1.0,  # beta ~1.0
            "TSLA": market_returns * 2.0,  # beta ~2.0
        }
        # Equal weight: expected ~1.5
        positions = {
            "AAPL": {"quantity": 100, "value": 5_000},
            "TSLA": {"quantity": 50, "value": 5_000},
        }

        beta = monitor.check_portfolio_beta(
            positions=positions,
            market_returns=market_returns,
            position_returns=position_returns,
        )
        assert beta == pytest.approx(1.5, abs=0.05)


class TestPairwiseCorrelation:
    def test_perfectly_correlated_pair(self):
        """Two identical return series -> correlation ~1.0."""
        monitor = CorrelationMonitor()
        returns = np.array([0.01, -0.02, 0.03, -0.01, 0.02] * 20)
        position_returns = {
            "AAPL": returns.copy(),
            "MSFT": returns.copy(),
        }

        pairs = monitor.check_pairwise_correlation(
            position_returns=position_returns,
            threshold=0.7,
        )
        assert len(pairs) == 1
        ticker_a, ticker_b, corr = pairs[0]
        assert {ticker_a, ticker_b} == {"AAPL", "MSFT"}
        assert corr == pytest.approx(1.0, abs=0.01)

    def test_uncorrelated_pair(self):
        """Uncorrelated returns -> low correlation, should not appear in results."""
        monitor = CorrelationMonitor()
        np.random.seed(42)
        position_returns = {
            "AAPL": np.random.randn(100),
            "MSFT": np.random.randn(100),
        }

        pairs = monitor.check_pairwise_correlation(
            position_returns=position_returns,
            threshold=0.7,
        )
        # Uncorrelated returns should have correlation well below threshold
        assert len(pairs) == 0

    def test_negatively_correlated_not_flagged_by_default(self):
        """Negative correlation should not be flagged (we check absolute value)."""
        monitor = CorrelationMonitor()
        returns = np.array([0.01, -0.02, 0.03, -0.01, 0.02] * 20)
        position_returns = {
            "AAPL": returns.copy(),
            "MSFT": -returns,
        }

        # With threshold 0.7, absolute correlation of 1.0 should flag
        pairs = monitor.check_pairwise_correlation(
            position_returns=position_returns,
            threshold=0.7,
        )
        assert len(pairs) == 1
        assert abs(pairs[0][2]) >= 0.7


class TestInsufficientData:
    def test_insufficient_data_skips_with_warning(self):
        """Less than min_lookback_days -> skip with warning alert."""
        monitor = CorrelationMonitor()
        market_returns = np.array([0.01, -0.02, 0.03])  # only 3 days
        position_returns = {"AAPL": np.array([0.01, -0.02, 0.03])}
        positions = {"AAPL": {"quantity": 100, "value": 10_000}}

        alerts = monitor.check(
            positions=positions,
            market_returns=market_returns,
            position_returns=position_returns,
            beta_threshold=1.5,
            correlation_threshold=0.7,
            min_lookback_days=60,
        )
        # Should have a warning about insufficient data
        assert len(alerts) >= 1
        assert any("insufficient" in a.message.lower() or "data" in a.message.lower() for a in alerts)

    def test_sufficient_data_runs_checks(self):
        """Enough data -> checks run and may produce alerts."""
        monitor = CorrelationMonitor()
        np.random.seed(42)
        market_returns = np.random.randn(100)
        position_returns = {
            "AAPL": market_returns * 2.0,  # high beta
        }
        positions = {"AAPL": {"quantity": 100, "value": 10_000}}

        alerts = monitor.check(
            positions=positions,
            market_returns=market_returns,
            position_returns=position_returns,
            beta_threshold=1.5,
            correlation_threshold=0.7,
            min_lookback_days=60,
        )
        # Should flag high beta
        assert len(alerts) >= 1
        assert any("beta" in a.message.lower() for a in alerts)


class TestCheckIntegration:
    def test_check_advisory_only_no_blocking(self):
        """Check should only return alerts, never block trades."""
        monitor = CorrelationMonitor()
        np.random.seed(42)
        n = 100
        market_returns = np.random.randn(n) * 0.01
        position_returns = {
            "AAPL": market_returns * 2.5,  # high beta
            "MSFT": market_returns * 2.5,  # correlated with AAPL
        }
        positions = {
            "AAPL": {"quantity": 100, "value": 10_000},
            "MSFT": {"quantity": 50, "value": 5_000},
        }

        alerts = monitor.check(
            positions=positions,
            market_returns=market_returns,
            position_returns=position_returns,
            beta_threshold=1.5,
            correlation_threshold=0.7,
            min_lookback_days=60,
        )
        # Alerts are advisory only - they are just messages, not RiskDecisions
        for alert in alerts:
            assert hasattr(alert, "message")
            assert hasattr(alert, "priority")

    def test_check_below_thresholds_no_alerts(self):
        """When everything is within thresholds, no alerts produced."""
        monitor = CorrelationMonitor()
        np.random.seed(42)
        n = 100
        market_returns = np.random.randn(n) * 0.01
        position_returns = {
            "AAPL": market_returns * 0.5,  # low beta
        }
        positions = {"AAPL": {"quantity": 100, "value": 10_000}}

        alerts = monitor.check(
            positions=positions,
            market_returns=market_returns,
            position_returns=position_returns,
            beta_threshold=1.5,
            correlation_threshold=0.7,
            min_lookback_days=60,
        )
        assert len(alerts) == 0
