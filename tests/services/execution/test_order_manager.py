from __future__ import annotations

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
