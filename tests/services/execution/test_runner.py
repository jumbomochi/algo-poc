from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.execution.runner import ExecutionServiceRunner
from shared.config import AppConfig, ExecutionConfig, IBConfig
from shared.schemas.messages import ApprovedOrderMessage, KillMessage


def make_approved_order(
    ticker: str = "AAPL",
    action: str = "buy",
    quantity: int = 50,
    order_type: str = "limit",
    limit_price: float | None = 150.0,
    recommendation_id: str = "rec-001",
) -> ApprovedOrderMessage:
    return ApprovedOrderMessage(
        ticker=ticker,
        timestamp=datetime.now(timezone.utc),
        action=action,
        quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
        recommendation_id=recommendation_id,
    )


@pytest.fixture()
def mock_config():
    config = MagicMock(spec=AppConfig)
    config.execution = ExecutionConfig()
    config.ib = IBConfig()
    config.mode = "paper"
    config.risk = MagicMock()
    config.risk.min_viable_fill_pct = 40.0
    return config


@pytest.fixture()
def mock_redis():
    redis = AsyncMock()
    redis.publish = AsyncMock(return_value="msg-id-001")
    redis.create_consumer_group = AsyncMock()
    redis.read_group = AsyncMock(return_value=[])
    redis.ack = AsyncMock()
    return redis


@pytest.fixture()
def mock_order_manager():
    mgr = AsyncMock()
    mgr.submit_entry = AsyncMock(return_value="order-001")
    mgr.submit_exit = AsyncMock(return_value="order-002")
    mgr.open_orders = {}
    mgr.cancel_all_orders = AsyncMock()
    return mgr


@pytest.fixture()
def runner(mock_config, mock_redis, mock_order_manager):
    r = ExecutionServiceRunner(
        config=mock_config,
        redis_client=mock_redis,
        order_manager=mock_order_manager,
    )
    return r


class TestApprovedOrderProcessing:
    @pytest.mark.asyncio
    async def test_process_buy_approved_order(
        self, runner, mock_redis, mock_order_manager
    ):
        """Buy approved order should submit limit entry via OrderManager."""
        order = make_approved_order(
            ticker="AAPL", action="buy", order_type="limit", limit_price=150.0
        )

        await runner.process_approved_order(order)

        mock_order_manager.submit_entry.assert_called_once_with(
            ticker="AAPL",
            quantity=50,
            limit_price=150.0,
            recommendation_id="rec-001",
        )

    @pytest.mark.asyncio
    async def test_process_sell_approved_order(
        self, runner, mock_redis, mock_order_manager
    ):
        """Sell approved order should submit market exit via OrderManager."""
        order = make_approved_order(
            ticker="AAPL",
            action="sell",
            order_type="market",
            limit_price=None,
            recommendation_id="rec-002",
        )

        await runner.process_approved_order(order)

        mock_order_manager.submit_exit.assert_called_once_with(
            ticker="AAPL",
            quantity=50,
            recommendation_id="rec-002",
        )

    @pytest.mark.asyncio
    async def test_buy_order_publishes_fill(
        self, runner, mock_redis, mock_order_manager
    ):
        """After submitting a buy, a fill message should be published."""
        order = make_approved_order(
            ticker="AAPL", action="buy", limit_price=150.0
        )

        await runner.process_approved_order(order)

        mock_redis.publish.assert_called()
        # Check that stream:fills was used
        call_args = mock_redis.publish.call_args
        assert call_args[0][0] == "stream:fills"

    @pytest.mark.asyncio
    async def test_sell_order_publishes_fill(
        self, runner, mock_redis, mock_order_manager
    ):
        """After submitting a sell, a fill message should be published."""
        order = make_approved_order(
            ticker="AAPL",
            action="sell",
            order_type="market",
            limit_price=None,
        )

        await runner.process_approved_order(order)

        mock_redis.publish.assert_called()
        call_args = mock_redis.publish.call_args
        assert call_args[0][0] == "stream:fills"


class TestKillHandling:
    @pytest.mark.asyncio
    async def test_kill_event_cancels_all_and_sells(
        self, runner, mock_redis, mock_order_manager
    ):
        """Kill event should cancel all open orders and market-sell all positions."""
        mock_order_manager.open_orders = {
            "order-001": {
                "ticker": "AAPL",
                "quantity": 50,
                "recommendation_id": "rec-001",
            },
        }
        runner._positions = {"AAPL": 100, "MSFT": 75}

        kill_msg = KillMessage(
            timestamp=datetime.now(timezone.utc),
            triggered_by="admin",
            reason="emergency",
        )

        await runner.process_kill(kill_msg)

        mock_order_manager.cancel_all_orders.assert_called_once()
        # Should have submitted market exits for all positions
        assert mock_order_manager.submit_exit.call_count == 2


class TestGracefulShutdown:
    @pytest.mark.asyncio
    async def test_graceful_shutdown_cleans_up(
        self, runner, mock_redis, mock_order_manager
    ):
        """Graceful shutdown should cancel all open orders."""
        mock_order_manager.open_orders = {
            "order-001": {
                "ticker": "AAPL",
                "quantity": 50,
                "recommendation_id": "rec-001",
            },
        }

        await runner.shutdown()

        mock_order_manager.cancel_all_orders.assert_called_once()

    @pytest.mark.asyncio
    async def test_setup_creates_consumer_groups(
        self, runner, mock_redis
    ):
        """Setup should create consumer groups for subscribed streams."""
        await runner.setup()

        assert mock_redis.create_consumer_group.call_count >= 2


class TestPaperMode:
    def test_paper_mode_uses_paper_port(self, mock_config):
        """Paper mode should use the paper port."""
        mock_config.mode = "paper"
        runner = ExecutionServiceRunner(
            config=mock_config,
            redis_client=AsyncMock(),
            order_manager=AsyncMock(),
        )
        assert runner.ib_port == mock_config.ib.paper_port

    def test_live_mode_uses_live_port(self, mock_config):
        """Live mode should use the live port."""
        mock_config.mode = "live"
        runner = ExecutionServiceRunner(
            config=mock_config,
            redis_client=AsyncMock(),
            order_manager=AsyncMock(),
        )
        assert runner.ib_port == mock_config.ib.live_port
