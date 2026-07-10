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
    async def test_buy_order_does_not_publish_fill_at_submission(
        self, runner, mock_redis, mock_order_manager
    ):
        """Submission is not a fill: nothing goes to stream:fills yet."""
        order = make_approved_order(
            ticker="AAPL", action="buy", limit_price=150.0
        )

        await runner.process_approved_order(order)

        mock_redis.publish.assert_not_called()
        # The order is tracked so a later IB fill can be attributed.
        assert order in runner._pending_orders.values()

    @pytest.mark.asyncio
    async def test_ib_fill_publishes_fill_with_execution_data(
        self, runner, mock_redis, mock_order_manager
    ):
        """A real IB fill event publishes actual execution price/quantity."""
        order = make_approved_order(ticker="AAPL", action="buy", limit_price=150.0)
        await runner.process_approved_order(order)
        (order_id,) = runner._pending_orders.keys()

        await runner.handle_ib_fill({
            "order_id": order_id,
            "ticker": "AAPL",
            "side": "buy",
            "quantity": 100.0,
            "fill_price": 149.87,
            "commission": 0.5,
            "order_done": True,
        })

        mock_redis.publish.assert_called_once()
        stream, payload = mock_redis.publish.call_args[0]
        assert stream == "stream:fills"
        assert payload["fill_price"] == "149.87"
        assert payload["recommendation_id"] == order.recommendation_id
        # Completed order is no longer pending.
        assert order_id not in runner._pending_orders

    @pytest.mark.asyncio
    async def test_partial_fill_keeps_order_pending(
        self, runner, mock_redis, mock_order_manager
    ):
        """A partial fill publishes but keeps the order tracked."""
        order = make_approved_order(ticker="AAPL", action="buy", limit_price=150.0)
        await runner.process_approved_order(order)
        (order_id,) = runner._pending_orders.keys()

        await runner.handle_ib_fill({
            "order_id": order_id,
            "ticker": "AAPL",
            "side": "buy",
            "quantity": 40.0,
            "fill_price": 149.90,
            "commission": 0.2,
            "order_done": False,
        })

        mock_redis.publish.assert_called_once()
        assert order_id in runner._pending_orders

    @pytest.mark.asyncio
    async def test_sell_order_does_not_publish_fill_at_submission(
        self, runner, mock_redis, mock_order_manager
    ):
        """Market exits also wait for the real IB fill."""
        order = make_approved_order(
            ticker="AAPL",
            action="sell",
            order_type="market",
            limit_price=None,
        )

        await runner.process_approved_order(order)

        mock_redis.publish.assert_not_called()
        assert order in runner._pending_orders.values()


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


class TestPaperAccountGuard:
    """connect(expect_paper=True) must refuse a live-account Gateway session."""

    @pytest.mark.asyncio
    async def test_live_account_on_paper_port_refused(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from services.execution.ib_executor import (
            IBExecutor,
            WrongAccountTypeError,
        )

        executor = IBExecutor(host="h", port=7497, client_id=1)
        fake_ib = MagicMock()
        fake_ib.connectAsync = AsyncMock()
        fake_ib.managedAccounts.return_value = ["U17723819"]  # LIVE prefix

        with patch("ib_insync.IB", return_value=fake_ib):
            with pytest.raises(WrongAccountTypeError, match="LIVE"):
                await executor.connect(expect_paper=True)

        fake_ib.disconnect.assert_called_once()
        assert executor._ib is None

    @pytest.mark.asyncio
    async def test_paper_account_accepted(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from services.execution.ib_executor import IBExecutor

        executor = IBExecutor(host="h", port=7497, client_id=1)
        fake_ib = MagicMock()
        fake_ib.connectAsync = AsyncMock()
        fake_ib.managedAccounts.return_value = ["DUN551088"]  # paper prefix

        with patch("ib_insync.IB", return_value=fake_ib):
            await executor.connect(expect_paper=True)

        assert executor._ib is fake_ib


class TestWholeShareFallback:
    """Accounts without fractional API support round down; sub-1-share skips."""

    def _executor(self, allow_fractional=False):
        from services.execution.ib_executor import IBExecutor

        return IBExecutor(host="h", port=7497, client_id=1,
                          allow_fractional=allow_fractional)

    def test_fractional_rounds_down(self):
        ex = self._executor()
        assert ex._effective_quantity("PM", 8.3243) == 8.0

    def test_whole_quantity_untouched(self):
        ex = self._executor()
        assert ex._effective_quantity("PM", 8.0) == 8.0

    def test_sub_one_share_raises_skip(self):
        from services.execution.ib_executor import OrderSkippedError

        ex = self._executor()
        with pytest.raises(OrderSkippedError, match="rounds to zero"):
            ex._effective_quantity("ISRG", 0.4)

    def test_fractional_allowed_passthrough(self):
        ex = self._executor(allow_fractional=True)
        assert ex._effective_quantity("PM", 8.3243) == 8.3243

    @pytest.mark.asyncio
    async def test_runner_treats_skip_as_nonfailure(self, runner, mock_redis, mock_order_manager):
        """A skipped order acks cleanly: no pending entry, no exception."""
        from services.execution.ib_executor import OrderSkippedError

        mock_order_manager.submit_entry.side_effect = OrderSkippedError("too small")
        order = make_approved_order(ticker="ISRG", action="buy", limit_price=432.83)

        await runner.process_approved_order(order)  # must not raise

        assert order not in runner._pending_orders.values()
        mock_redis.publish.assert_not_called()
