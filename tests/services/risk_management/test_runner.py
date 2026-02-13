from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.risk_management.engine import PortfolioState, RiskDecision
from services.risk_management.runner import RiskServiceRunner
from shared.config import AppConfig, RiskConfig
from shared.schemas.messages import (
    ApprovedOrderMessage,
    KillMessage,
    RecommendationMessage,
)


def make_portfolio(
    nav: float = 100_000,
    peak_nav: float | None = None,
    positions: dict | None = None,
) -> PortfolioState:
    return PortfolioState(
        nav=nav,
        peak_nav=peak_nav if peak_nav is not None else nav,
        positions=positions or {},
        sector_exposure={},
        total_exposure_pct=0.0,
        margin_utilization_pct=0.0,
    )


def make_recommendation(
    ticker: str = "AAPL",
    action: str = "buy",
    confidence: float = 0.85,
) -> RecommendationMessage:
    return RecommendationMessage(
        ticker=ticker,
        timestamp=datetime.now(timezone.utc),
        action=action,
        confidence=confidence,
        top_features={"support_proximity": 0.3},
        recommendation_id=str(uuid.uuid4()),
    )


@pytest.fixture()
def mock_config():
    config = MagicMock(spec=AppConfig)
    config.risk = RiskConfig()
    return config


@pytest.fixture()
def mock_redis():
    redis = AsyncMock()
    redis.publish = AsyncMock(return_value="msg-id-123")
    redis.create_consumer_group = AsyncMock()
    redis.read_group = AsyncMock(return_value=[])
    redis.ack = AsyncMock()
    return redis


@pytest.fixture()
def mock_portfolio():
    return make_portfolio(nav=100_000)


@pytest.fixture()
def runner(mock_config, mock_redis):
    r = RiskServiceRunner(
        config=mock_config,
        redis_client=mock_redis,
    )
    return r


class TestRecommendationProcessing:
    @pytest.mark.asyncio
    async def test_approved_buy_publishes_to_approved_orders(
        self, runner, mock_redis, mock_portfolio
    ):
        """Approved buy recommendation -> published to stream:approved_orders."""
        runner._portfolio = mock_portfolio
        rec = make_recommendation(ticker="AAPL", action="buy")

        with patch.object(
            runner._engine,
            "check_entry",
            return_value=RiskDecision(approved=True, reason="ok", adjusted_quantity=50),
        ), patch.object(
            runner._kill_switch, "check",
            return_value=RiskDecision(approved=True, reason="inactive"),
        ), patch.object(
            runner._engine,
            "check_portfolio_drawdown",
            return_value=RiskDecision(approved=True, reason="ok"),
        ):
            await runner.process_recommendation(rec)

        mock_redis.publish.assert_called()
        call_args = mock_redis.publish.call_args
        assert call_args[0][0] == "stream:approved_orders"

    @pytest.mark.asyncio
    async def test_rejected_entry_does_not_publish(
        self, runner, mock_redis, mock_portfolio
    ):
        """Rejected entry -> nothing published to approved_orders."""
        runner._portfolio = mock_portfolio
        rec = make_recommendation(ticker="AAPL", action="buy")

        with patch.object(
            runner._engine,
            "check_entry",
            return_value=RiskDecision(approved=False, reason="sector limit", adjusted_quantity=0),
        ), patch.object(
            runner._kill_switch, "check",
            return_value=RiskDecision(approved=True, reason="inactive"),
        ), patch.object(
            runner._engine,
            "check_portfolio_drawdown",
            return_value=RiskDecision(approved=True, reason="ok"),
        ):
            await runner.process_recommendation(rec)

        # Should publish alert but not approved order
        published_streams = [c[0][0] for c in mock_redis.publish.call_args_list]
        assert "stream:approved_orders" not in published_streams

    @pytest.mark.asyncio
    async def test_sell_recommendation_passes_through(
        self, runner, mock_redis, mock_portfolio
    ):
        """Sell recommendation should pass through risk checks."""
        runner._portfolio = mock_portfolio
        rec = make_recommendation(ticker="AAPL", action="sell")

        with patch.object(
            runner._kill_switch, "check",
            return_value=RiskDecision(approved=True, reason="inactive"),
        ), patch.object(
            runner._engine,
            "check_portfolio_drawdown",
            return_value=RiskDecision(approved=True, reason="ok"),
        ):
            await runner.process_recommendation(rec)

        mock_redis.publish.assert_called()
        call_args = mock_redis.publish.call_args
        assert call_args[0][0] == "stream:approved_orders"

    @pytest.mark.asyncio
    async def test_hold_recommendation_ignored(
        self, runner, mock_redis, mock_portfolio
    ):
        """Hold recommendations should not produce any orders."""
        runner._portfolio = mock_portfolio
        rec = make_recommendation(ticker="AAPL", action="hold")

        await runner.process_recommendation(rec)

        published_streams = [c[0][0] for c in mock_redis.publish.call_args_list]
        assert "stream:approved_orders" not in published_streams


class TestKillSwitchIntegration:
    @pytest.mark.asyncio
    async def test_kill_switch_active_rejects_buy(
        self, runner, mock_redis, mock_portfolio
    ):
        """Active kill switch should reject all entries."""
        runner._portfolio = mock_portfolio
        rec = make_recommendation(ticker="AAPL", action="buy")

        with patch.object(
            runner._kill_switch, "check",
            return_value=RiskDecision(approved=False, reason="Kill switch active: margin call"),
        ):
            await runner.process_recommendation(rec)

        published_streams = [c[0][0] for c in mock_redis.publish.call_args_list]
        assert "stream:approved_orders" not in published_streams

    @pytest.mark.asyncio
    async def test_kill_message_activates_switch_and_liquidates(
        self, runner, mock_redis
    ):
        """Kill message should activate switch and emit sell orders for all positions."""
        runner._portfolio = make_portfolio(
            nav=100_000,
            positions={
                "AAPL": {"quantity": 100, "sector": "Technology"},
                "MSFT": {"quantity": 50, "sector": "Technology"},
            },
        )
        runner._current_prices = {"AAPL": 150.0, "MSFT": 300.0}

        kill_msg = KillMessage(
            timestamp=datetime.now(timezone.utc),
            triggered_by="admin",
            reason="emergency shutdown",
        )

        await runner.process_kill(kill_msg)

        assert runner._kill_switch.is_active is True
        # Should have published sell orders for both positions
        published_calls = mock_redis.publish.call_args_list
        order_calls = [c for c in published_calls if c[0][0] == "stream:approved_orders"]
        assert len(order_calls) == 2


class TestDrawdownCheck:
    @pytest.mark.asyncio
    async def test_drawdown_rejects_new_buy(
        self, runner, mock_redis
    ):
        """Portfolio drawdown above pause threshold rejects new buys."""
        runner._portfolio = make_portfolio(nav=85_000, peak_nav=100_000)
        rec = make_recommendation(ticker="AAPL", action="buy")

        with patch.object(
            runner._kill_switch, "check",
            return_value=RiskDecision(approved=True, reason="inactive"),
        ):
            await runner.process_recommendation(rec)

        published_streams = [c[0][0] for c in mock_redis.publish.call_args_list]
        assert "stream:approved_orders" not in published_streams


class TestPassiveMonitoring:
    @pytest.mark.asyncio
    async def test_passive_scan_publishes_alerts(self, runner, mock_redis):
        """Passive monitoring should publish alerts for breaches."""
        runner._portfolio = make_portfolio(
            nav=100_000,
            positions={
                "AAPL": {"quantity": 200, "sector": "Technology"},  # 20% of NAV -> hard trim
            },
        )
        runner._current_prices = {"AAPL": 100.0}

        await runner.run_passive_scan()

        published_calls = mock_redis.publish.call_args_list
        alert_calls = [c for c in published_calls if c[0][0] == "stream:alerts"]
        assert len(alert_calls) >= 1


class TestAuditLogging:
    @pytest.mark.asyncio
    async def test_decisions_are_logged(self, runner, mock_redis, mock_portfolio):
        """All risk decisions should be logged to audit."""
        runner._portfolio = mock_portfolio
        mock_logger = MagicMock()
        runner._logger = mock_logger
        rec = make_recommendation(ticker="AAPL", action="buy")

        with patch.object(
            runner._engine,
            "check_entry",
            return_value=RiskDecision(approved=True, reason="ok", adjusted_quantity=50),
        ), patch.object(
            runner._kill_switch, "check",
            return_value=RiskDecision(approved=True, reason="inactive"),
        ), patch.object(
            runner._engine,
            "check_portfolio_drawdown",
            return_value=RiskDecision(approved=True, reason="ok"),
        ):
            await runner.process_recommendation(rec)

        # Logger should have been called (at least info level)
        assert mock_logger.info.call_count >= 1
