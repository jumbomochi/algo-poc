from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.execution.order_manager import OrderAction, OrderManager


class TestUnfilledOrders:
    def _make_manager(
        self,
        reprice_interval_minutes: int = 60,
        max_reprice_attempts: int = 3,
    ) -> OrderManager:
        mock_executor = AsyncMock()
        mock_executor.submit_limit_order = AsyncMock(return_value="order-001")
        mgr = OrderManager(
            executor=mock_executor,
            redis_client=AsyncMock(),
            db_session=MagicMock(),
            reprice_interval_minutes=reprice_interval_minutes,
            max_reprice_attempts=max_reprice_attempts,
        )
        return mgr

    def test_order_unfilled_for_one_hour_triggers_reprice(self):
        """Order unfilled for 1 hour should produce a reprice action."""
        mgr = self._make_manager()
        now = datetime.now(timezone.utc)
        # Simulate an open order placed 61 minutes ago
        mgr.open_orders["order-001"] = {
            "ticker": "AAPL",
            "quantity": 50,
            "limit_price": 150.0,
            "placed_at": now - timedelta(minutes=61),
            "last_repriced_at": now - timedelta(minutes=61),
            "reprice_count": 0,
            "recommendation_id": "rec-001",
        }
        current_prices = {"AAPL": 151.0}
        mock_calendar = MagicMock()
        mock_calendar.get_next_market_close.return_value = now + timedelta(hours=3)

        actions = mgr.check_unfilled_orders(current_prices, mock_calendar)

        assert len(actions) == 1
        assert actions[0].order_id == "order-001"
        assert actions[0].action_type == "reprice"
        assert actions[0].new_price == 151.0

    def test_order_unfilled_after_three_reprices_triggers_cancel(self):
        """Order that has been repriced 3 times should be cancelled."""
        mgr = self._make_manager()
        now = datetime.now(timezone.utc)
        mgr.open_orders["order-001"] = {
            "ticker": "AAPL",
            "quantity": 50,
            "limit_price": 150.0,
            "placed_at": now - timedelta(hours=4),
            "last_repriced_at": now - timedelta(minutes=61),
            "reprice_count": 3,
            "recommendation_id": "rec-001",
        }
        current_prices = {"AAPL": 151.0}
        mock_calendar = MagicMock()
        mock_calendar.get_next_market_close.return_value = now + timedelta(hours=2)

        actions = mgr.check_unfilled_orders(current_prices, mock_calendar)

        assert len(actions) == 1
        assert actions[0].order_id == "order-001"
        assert actions[0].action_type == "cancel"
        assert actions[0].new_price is None

    def test_order_unfilled_at_market_close_triggers_cancel(self):
        """Order unfilled at market close should be cancelled."""
        mgr = self._make_manager()
        now = datetime.now(timezone.utc)
        mgr.open_orders["order-001"] = {
            "ticker": "AAPL",
            "quantity": 50,
            "limit_price": 150.0,
            "placed_at": now - timedelta(minutes=30),
            "last_repriced_at": now - timedelta(minutes=30),
            "reprice_count": 0,
            "recommendation_id": "rec-001",
        }
        current_prices = {"AAPL": 151.0}
        mock_calendar = MagicMock()
        # Market closes in 5 minutes
        mock_calendar.get_next_market_close.return_value = now + timedelta(minutes=5)

        actions = mgr.check_unfilled_orders(current_prices, mock_calendar)

        assert len(actions) == 1
        assert actions[0].order_id == "order-001"
        assert actions[0].action_type == "cancel"

    def test_recently_placed_order_no_action(self):
        """Recently placed order should not trigger any action."""
        mgr = self._make_manager()
        now = datetime.now(timezone.utc)
        mgr.open_orders["order-001"] = {
            "ticker": "AAPL",
            "quantity": 50,
            "limit_price": 150.0,
            "placed_at": now - timedelta(minutes=10),
            "last_repriced_at": now - timedelta(minutes=10),
            "reprice_count": 0,
            "recommendation_id": "rec-001",
        }
        current_prices = {"AAPL": 151.0}
        mock_calendar = MagicMock()
        mock_calendar.get_next_market_close.return_value = now + timedelta(hours=5)

        actions = mgr.check_unfilled_orders(current_prices, mock_calendar)

        assert len(actions) == 0

    def test_order_action_dataclass(self):
        """OrderAction should be a proper dataclass."""
        action = OrderAction(
            order_id="order-001",
            action_type="reprice",
            new_price=155.0,
        )
        assert action.order_id == "order-001"
        assert action.action_type == "reprice"
        assert action.new_price == 155.0

    def test_cancel_action_has_no_new_price(self):
        """Cancel actions should have new_price=None."""
        action = OrderAction(
            order_id="order-001",
            action_type="cancel",
            new_price=None,
        )
        assert action.new_price is None
