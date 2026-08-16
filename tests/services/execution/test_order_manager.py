from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.execution.order_manager import OrderManager


class TestOrderManager:
    @pytest.mark.asyncio
    async def test_submit_limit_entry(self):
        mock_executor = AsyncMock()
        mock_executor.submit_limit_order = AsyncMock(return_value="order-001")
        mgr = OrderManager(
            executor=mock_executor,
            redis_client=AsyncMock(),
            db_session=MagicMock(),
        )
        order_id = await mgr.submit_entry(
            "AAPL",
            quantity=50,
            limit_price=150.75,
            recommendation_id="rec-001",
        )
        assert order_id == "order-001"
        mock_executor.submit_limit_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_submit_market_exit(self):
        mock_executor = AsyncMock()
        mock_executor.submit_market_order = AsyncMock(return_value="order-002")
        mgr = OrderManager(
            executor=mock_executor,
            redis_client=AsyncMock(),
            db_session=MagicMock(),
        )
        order_id = await mgr.submit_exit(
            "AAPL", quantity=50, recommendation_id="rec-001"
        )
        assert order_id == "order-002"
        mock_executor.submit_market_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_submit_exit_tracked_in_open_orders(self):
        """Exits must be tracked so cancel_all_orders can reach a stuck exit."""
        mock_executor = AsyncMock()
        mock_executor.find_order_by_ref = AsyncMock(return_value=None)
        mock_executor.submit_market_order = AsyncMock(return_value="order-002")
        mgr = OrderManager(
            executor=mock_executor,
            redis_client=AsyncMock(),
            db_session=MagicMock(),
        )
        order_id = await mgr.submit_exit(
            "AAPL", quantity=50, recommendation_id="rec-exit"
        )
        assert order_id in mgr.open_orders
        assert mgr.open_orders[order_id]["order_type"] == "market"

    @pytest.mark.asyncio
    async def test_cancel_all_reaches_exit_orders(self):
        mock_executor = AsyncMock()
        mock_executor.find_order_by_ref = AsyncMock(return_value=None)
        mock_executor.submit_market_order = AsyncMock(return_value="order-002")
        mock_executor.cancel_order = AsyncMock(return_value=True)
        mgr = OrderManager(
            executor=mock_executor,
            redis_client=AsyncMock(),
            db_session=MagicMock(),
        )
        await mgr.submit_exit("AAPL", quantity=50, recommendation_id="rec-exit")

        cancelled = await mgr.cancel_all_orders()

        assert "order-002" in cancelled

    @pytest.mark.asyncio
    async def test_idempotency_prevents_duplicate(self):
        mock_executor = AsyncMock()
        mock_executor.submit_limit_order = AsyncMock(return_value="order-001")
        mgr = OrderManager(
            executor=mock_executor,
            redis_client=AsyncMock(),
            db_session=MagicMock(),
        )
        await mgr.submit_entry(
            "AAPL",
            quantity=50,
            limit_price=150.0,
            recommendation_id="rec-001",
        )
        await mgr.submit_entry(
            "AAPL",
            quantity=50,
            limit_price=150.0,
            recommendation_id="rec-001",
        )
        assert mock_executor.submit_limit_order.call_count == 1

    @pytest.mark.asyncio
    async def test_idempotency_returns_cached_order_id(self):
        mock_executor = AsyncMock()
        mock_executor.submit_limit_order = AsyncMock(return_value="order-001")
        mgr = OrderManager(
            executor=mock_executor,
            redis_client=AsyncMock(),
            db_session=MagicMock(),
        )
        first = await mgr.submit_entry(
            "AAPL",
            quantity=50,
            limit_price=150.0,
            recommendation_id="rec-001",
        )
        second = await mgr.submit_entry(
            "AAPL",
            quantity=50,
            limit_price=150.0,
            recommendation_id="rec-001",
        )
        assert first == second == "order-001"

    @pytest.mark.asyncio
    async def test_different_recommendation_ids_both_submit(self):
        mock_executor = AsyncMock()
        mock_executor.submit_limit_order = AsyncMock(
            side_effect=["order-001", "order-002"]
        )
        mgr = OrderManager(
            executor=mock_executor,
            redis_client=AsyncMock(),
            db_session=MagicMock(),
        )
        id1 = await mgr.submit_entry(
            "AAPL", quantity=50, limit_price=150.0, recommendation_id="rec-001"
        )
        id2 = await mgr.submit_entry(
            "AAPL", quantity=50, limit_price=150.0, recommendation_id="rec-002"
        )
        assert id1 == "order-001"
        assert id2 == "order-002"
        assert mock_executor.submit_limit_order.call_count == 2

    @pytest.mark.asyncio
    async def test_exit_idempotency(self):
        mock_executor = AsyncMock()
        mock_executor.submit_market_order = AsyncMock(return_value="order-002")
        mgr = OrderManager(
            executor=mock_executor,
            redis_client=AsyncMock(),
            db_session=MagicMock(),
        )
        await mgr.submit_exit("AAPL", quantity=50, recommendation_id="rec-exit-001")
        await mgr.submit_exit("AAPL", quantity=50, recommendation_id="rec-exit-001")
        assert mock_executor.submit_market_order.call_count == 1

    @pytest.mark.asyncio
    async def test_submit_entry_records_order(self):
        """Submitted entry should be tracked in open_orders."""
        mock_executor = AsyncMock()
        mock_executor.submit_limit_order = AsyncMock(return_value="order-001")
        mgr = OrderManager(
            executor=mock_executor,
            redis_client=AsyncMock(),
            db_session=MagicMock(),
        )
        await mgr.submit_entry(
            "AAPL", quantity=50, limit_price=150.0, recommendation_id="rec-001"
        )
        assert "order-001" in mgr.open_orders
        order_info = mgr.open_orders["order-001"]
        assert order_info["ticker"] == "AAPL"
        assert order_info["quantity"] == 50


class TestBrokerOrderPassthrough:
    """KAN-13: the post-halt sweep needs a broker-wide view, not this
    process's own open_orders."""

    @staticmethod
    def _manager(executor):
        return OrderManager(
            executor=executor,
            redis_client=AsyncMock(),
            db_session=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_list_open_broker_orders_delegates_to_the_executor(self):
        executor = AsyncMock()
        executor.list_open_orders = AsyncMock(return_value=["order"])
        mgr = self._manager(executor)

        assert await mgr.list_open_broker_orders() == ["order"]
        executor.list_open_orders.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_broker_order_drops_local_tracking_on_success(self):
        executor = AsyncMock()
        executor.cancel_broker_order = AsyncMock(return_value=True)
        mgr = self._manager(executor)
        mgr.open_orders["77"] = {"ticker": "AAPL"}

        assert await mgr.cancel_broker_order("77") is True
        assert "77" not in mgr.open_orders

    @pytest.mark.asyncio
    async def test_failed_cancel_keeps_the_order_tracked(self):
        """A failed cancel is still live at IB; forgetting it would stop the
        next attempt from retrying."""
        executor = AsyncMock()
        executor.cancel_broker_order = AsyncMock(return_value=False)
        mgr = self._manager(executor)
        mgr.open_orders["77"] = {"ticker": "AAPL"}

        assert await mgr.cancel_broker_order("77") is False
        assert "77" in mgr.open_orders


class TestOversellGuardPrimitives:
    """KAN-10: what the execution runner needs from the broker before it
    sizes an emergency sell — working BUYs gone, and a position count."""

    @staticmethod
    def _manager(executor):
        mgr = OrderManager(
            executor=executor,
            redis_client=AsyncMock(),
            db_session=MagicMock(),
        )
        # No real waiting in tests; the schedule's length is what matters.
        mgr._cancel_ack_backoff = (0.0, 0.0, 0.0)
        return mgr

    @staticmethod
    def _open_order(order_id: str = "77", order_ref: str = "buy-1"):
        return SimpleNamespace(
            order_id=order_id,
            order_ref=order_ref,
            action="BUY",
            ticker="AAPL",
            quantity=50.0,
            account_id="DUN551088",
        )

    @pytest.mark.asyncio
    async def test_cancel_is_confirmed_against_the_broker_not_the_request(self):
        """cancelOrder only *requests* a cancel. Sizing a sell the moment the
        request returns is the race the guard exists to close, so the ref has
        to be gone from the broker's open orders first."""
        executor = AsyncMock()
        executor.cancel_broker_order = AsyncMock(return_value=True)
        executor.list_open_orders = AsyncMock(
            side_effect=[[self._open_order()], []]
        )
        mgr = self._manager(executor)

        assert await mgr.cancel_working_orders([("77", "buy-1")]) == []
        assert executor.list_open_orders.await_count == 2

    @pytest.mark.asyncio
    async def test_a_ref_still_live_after_the_wait_is_reported(self):
        executor = AsyncMock()
        executor.cancel_broker_order = AsyncMock(return_value=True)
        executor.list_open_orders = AsyncMock(
            return_value=[self._open_order()]
        )
        mgr = self._manager(executor)

        assert await mgr.cancel_working_orders([("77", "buy-1")]) == ["buy-1"]

    @pytest.mark.asyncio
    async def test_an_order_already_gone_from_ib_is_confirmed_cancelled(self):
        """cancel_broker_order returns False for an order IB no longer lists —
        which is the state the caller wanted. The broker's book decides, not
        the return value of the request."""
        executor = AsyncMock()
        executor.cancel_broker_order = AsyncMock(return_value=False)
        executor.list_open_orders = AsyncMock(return_value=[])
        mgr = self._manager(executor)

        assert await mgr.cancel_working_orders([("77", "buy-1")]) == []

    @pytest.mark.asyncio
    async def test_a_raising_cancel_is_still_settled_by_the_brokers_book(self):
        """The book is the authority, not the request. An order IB does not
        list is not working, however the cancel that removed it returned."""
        executor = AsyncMock()
        executor.cancel_broker_order = AsyncMock(side_effect=RuntimeError("boom"))
        executor.list_open_orders = AsyncMock(return_value=[])
        mgr = self._manager(executor)

        assert await mgr.cancel_working_orders([("77", "buy-1")]) == []

    @pytest.mark.asyncio
    async def test_an_unreadable_book_leaves_the_ref_unconfirmed(self):
        """The disconnect case: nothing can be confirmed, so nothing is. This
        is what stops an emergency sell from being sized while a BUY may still
        be working."""
        executor = AsyncMock()
        executor.cancel_broker_order = AsyncMock(side_effect=RuntimeError("boom"))
        executor.list_open_orders = AsyncMock(side_effect=RuntimeError("no ib"))
        mgr = self._manager(executor)

        assert await mgr.cancel_working_orders([("77", "buy-1")]) == ["buy-1"]

    @pytest.mark.asyncio
    async def test_no_orders_makes_no_broker_call(self):
        executor = AsyncMock()
        mgr = self._manager(executor)

        assert await mgr.cancel_working_orders([]) == []
        executor.cancel_broker_order.assert_not_awaited()
        executor.list_open_orders.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_broker_position_delegates_to_the_executor(self):
        executor = AsyncMock()
        executor.broker_position = AsyncMock(return_value=7.0)
        mgr = self._manager(executor)

        assert await mgr.broker_position(265598) == 7.0
        executor.broker_position.assert_awaited_once_with(265598)
