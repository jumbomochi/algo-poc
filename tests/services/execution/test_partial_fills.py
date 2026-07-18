from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.execution.order_manager import OrderManager, PartialFillDecision


class TestPartialFills:
    def _make_manager(self) -> OrderManager:
        return OrderManager(
            executor=AsyncMock(),
            redis_client=AsyncMock(),
            db_session=MagicMock(),
        )

    def test_sixty_percent_filled_accepted(self):
        """60% fill is above the 40% minimum -> accept."""
        mgr = self._make_manager()
        decision = mgr.handle_partial_fill(
            order_id="order-001",
            filled_quantity=60,
            total_quantity=100,
            min_viable_fill_pct=40.0,
        )
        assert decision.action == "accept"
        assert decision.filled_pct == pytest.approx(60.0)
        assert "accept" in decision.message.lower() or "undersized" in decision.message.lower()

    def test_thirty_percent_filled_flagged(self):
        """30% fill is below the 40% minimum -> flag for review."""
        mgr = self._make_manager()
        decision = mgr.handle_partial_fill(
            order_id="order-001",
            filled_quantity=30,
            total_quantity=100,
            min_viable_fill_pct=40.0,
        )
        assert decision.action == "flag_for_review"
        assert decision.filled_pct == pytest.approx(30.0)
        assert "review" in decision.message.lower()

    def test_hundred_percent_filled_accepted(self):
        """100% fill -> accept (fully filled)."""
        mgr = self._make_manager()
        decision = mgr.handle_partial_fill(
            order_id="order-001",
            filled_quantity=100,
            total_quantity=100,
            min_viable_fill_pct=40.0,
        )
        assert decision.action == "accept"
        assert decision.filled_pct == pytest.approx(100.0)

    def test_exactly_at_threshold_accepted(self):
        """Exactly at min viable fill pct -> accept."""
        mgr = self._make_manager()
        decision = mgr.handle_partial_fill(
            order_id="order-001",
            filled_quantity=40,
            total_quantity=100,
            min_viable_fill_pct=40.0,
        )
        assert decision.action == "accept"
        assert decision.filled_pct == pytest.approx(40.0)

    def test_partial_fill_decision_dataclass(self):
        """PartialFillDecision should be a proper dataclass."""
        decision = PartialFillDecision(
            action="accept",
            filled_pct=75.0,
            message="Accepted as undersized position",
        )
        assert decision.action == "accept"
        assert decision.filled_pct == 75.0
        assert decision.message == "Accepted as undersized position"

    def test_just_below_threshold_flagged(self):
        """39.9% fill is just below 40% minimum -> flag for review."""
        mgr = self._make_manager()
        decision = mgr.handle_partial_fill(
            order_id="order-001",
            filled_quantity=399,
            total_quantity=1000,
            min_viable_fill_pct=40.0,
        )
        assert decision.action == "flag_for_review"
        assert decision.filled_pct == pytest.approx(39.9)


class TestIBExecutionIdentity:
    def test_inactive_reason_uses_latest_ib_trade_log_message(self):
        from services.execution.ib_executor import IBExecutor

        executor = IBExecutor("h", 7497, 1)
        trade = MagicMock()
        trade.orderStatus.whyHeld = ""
        trade.log = [
            SimpleNamespace(message="Submitted"),
            SimpleNamespace(message="Error 201: order rejected"),
        ]

        assert executor._status_reason(trade) == "Error 201: order rejected"

    def test_fill_payload_contains_broker_execution_identity(self):
        from services.execution.ib_executor import IBExecutor

        executor = IBExecutor("h", 7497, 1)
        handler = AsyncMock()
        executor.set_fill_handler(handler)
        trade = MagicMock()
        trade.isDone.return_value = False
        fill_callback = None

        class Event:
            def __iadd__(self, callback):
                nonlocal fill_callback
                fill_callback = callback
                return self

        class StatusEvent:
            def __iadd__(self, callback):
                return self

        trade.fillEvent = Event()
        trade.statusEvent = StatusEvent()
        fill = MagicMock()
        fill.execution.execId = "exec-1"
        fill.execution.acctNumber = "DUN551088"
        fill.execution.shares = 2
        fill.execution.cumQty = 5
        fill.execution.price = 149.5
        fill.contract.conId = 265598
        fill.contract.symbol = "AAPL"
        fill.contract.exchange = ""
        fill.contract.currency = ""
        fill.commissionReport.commission = 0.2

        executor._register_trade("9", trade, ticker="AAPL", side="buy")
        fill_callback(trade, fill)

        payload = handler.call_args.args[0]
        assert payload == {
            "execution_id": "exec-1",
            "account_id": "DUN551088",
            "order_id": "9",
            "con_id": 265598,
            "ticker": "AAPL",
            "exchange": "SMART",
            "currency": "USD",
            "side": "buy",
            "quantity": 2.0,
            "cumulative_quantity": 5.0,
            "fill_price": 149.5,
            "commission": 0.2,
            "order_done": False,
        }

    @pytest.mark.asyncio
    async def test_order_manager_recovers_matching_order_ref_without_resubmit(self):
        executor = AsyncMock()
        executor.find_order_by_ref.return_value = "77"
        manager = OrderManager(executor, AsyncMock(), MagicMock())

        order_id = await manager.submit_entry(
            "AAPL", 5, 150.0, recommendation_id="rec-1"
        )

        assert order_id == "77"
        executor.submit_limit_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_executor_finds_completed_order_ref_before_resubmission(self):
        from services.execution.ib_executor import IBExecutor

        executor = IBExecutor("h", 7497, 1)
        fake_ib = MagicMock()
        fake_ib.isConnected.return_value = True
        fake_ib.openTrades.return_value = []
        completed = MagicMock()
        completed.order.orderRef = "rec-1"
        completed.order.orderId = 77
        fake_ib.reqCompletedOrdersAsync = AsyncMock(return_value=[completed])
        executor._ib = fake_ib

        assert await executor.find_order_by_ref("rec-1") == "77"

    @pytest.mark.asyncio
    async def test_restore_fails_closed_when_order_missing_at_broker(self):
        executor = AsyncMock()
        executor.restore_order_by_ref.return_value = None
        manager = OrderManager(executor, AsyncMock(), MagicMock())
        manager.restore_submission(
            "rec-1", "77", ticker="AAPL", quantity=5, limit_price=150
        )

        with pytest.raises(RuntimeError, match="missing at IB"):
            await manager.restore_broker_tracking()

    @pytest.mark.asyncio
    async def test_order_ref_is_forwarded_to_broker_submission(self):
        executor = AsyncMock()
        executor.find_order_by_ref.return_value = None
        executor.submit_limit_order.return_value = "77"
        manager = OrderManager(executor, AsyncMock(), MagicMock())

        await manager.submit_entry("AAPL", 5, 150.0, recommendation_id="rec-1")

        executor.submit_limit_order.assert_awaited_once_with(
            "AAPL", 5, 150.0, recommendation_id="rec-1"
        )

    @pytest.mark.asyncio
    async def test_restart_uses_completed_history_to_confirm_expiry(self):
        from services.execution.ib_executor import IBExecutor

        executor = IBExecutor("h", 7497, 1)
        fake_ib = MagicMock()
        fake_ib.isConnected.return_value = True
        fake_ib.openTrades.return_value = []
        completed = MagicMock()
        completed.order.orderRef = "rec-1"
        completed.order.orderId = 9
        completed.orderStatus.status = "Expired"
        fake_ib.reqCompletedOrdersAsync = AsyncMock(return_value=[completed])
        executor._ib = fake_ib
        handler = AsyncMock()
        executor.set_order_status_handler(handler)

        restored = await executor.restore_order_by_ref("rec-1", "9")

        assert restored is False
        handler.assert_awaited_once_with({
            "order_id": "9",
            "status": "Expired",
            "reason": "",
            "completed_order_confirmed": True,
        })
