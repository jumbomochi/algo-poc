from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from services.execution.order_manager import OrderAction, OrderManager
from shared.market_calendar import MarketCalendar


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


class TestUnfilledSweep:
    def _mgr(self):
        executor = AsyncMock()
        executor.cancel_order = AsyncMock(return_value=True)
        return OrderManager(
            executor=executor,
            redis_client=AsyncMock(),
            db_session=MagicMock(),
            reprice_interval_minutes=60,
            max_reprice_attempts=3,
        )

    def _calendar(self, close_in_hours=3):
        now = datetime.now(timezone.utc)
        cal = MagicMock()
        cal.get_next_market_close.return_value = now + timedelta(hours=close_in_hours)
        return cal

    def test_check_unfilled_skips_market_orders(self):
        """A market exit tracked in open_orders must never be repriced/cancelled
        by the unfilled sweep (it is not an unfilled limit)."""
        mgr = self._mgr()
        now = datetime.now(timezone.utc)
        mgr.open_orders["exit-1"] = {
            "ticker": "AAPL",
            "quantity": 5,
            "limit_price": None,
            "placed_at": now - timedelta(hours=5),
            "last_repriced_at": now - timedelta(hours=5),
            "reprice_count": 0,
            "recommendation_id": "rec-exit",
            "order_type": "market",
        }
        actions = mgr.check_unfilled_orders({}, self._calendar(close_in_hours=0.1))
        assert actions == []

    @pytest.mark.asyncio
    async def test_sweep_applies_cancel_and_frees_open_order(self):
        mgr = self._mgr()
        now = datetime.now(timezone.utc)
        mgr.open_orders["order-1"] = {
            "ticker": "AAPL",
            "quantity": 50,
            "limit_price": 150.0,
            "placed_at": now - timedelta(minutes=5),
            "last_repriced_at": now - timedelta(minutes=5),
            "reprice_count": 3,  # at max -> cancel
            "recommendation_id": "rec-1",
            "order_type": "limit",
        }
        applied = await mgr.sweep_unfilled_orders({}, self._calendar())
        assert applied == 1
        mgr._executor.cancel_order.assert_awaited_once_with("order-1")
        assert "order-1" not in mgr.open_orders

    @pytest.mark.asyncio
    async def test_sweep_advances_reprice_bookkeeping_without_feed(self):
        """With no live quote feed, a stale limit can't be repriced to a better
        price, but its bookkeeping must advance so it eventually cancels."""
        mgr = self._mgr()
        now = datetime.now(timezone.utc)
        mgr.open_orders["order-1"] = {
            "ticker": "AAPL",
            "quantity": 50,
            "limit_price": 150.0,
            "placed_at": now - timedelta(minutes=61),
            "last_repriced_at": now - timedelta(minutes=61),
            "reprice_count": 0,
            "recommendation_id": "rec-1",
            "order_type": "limit",
        }
        await mgr.sweep_unfilled_orders({}, self._calendar())
        assert mgr.open_orders["order-1"]["reprice_count"] == 1


class _CalendarAt:
    """Real NYSE calendar answers, as of a fixed ET wall-clock instant.

    :meth:`OrderManager.check_unfilled_orders` reads ``datetime.now()`` itself,
    so a test cannot choose the instant it asks about. This maps whatever it
    passes onto ``pinned`` and answers from a real :class:`MarketCalendar` —
    the close is returned as the *offset* the real calendar would produce, so
    the ``next_close - now`` arithmetic under test stays faithful (including
    the negative offset that exists after the bell).
    """

    def __init__(self, pinned: datetime):
        self._cal = MarketCalendar()
        self._pinned = pinned

    def get_next_market_close(self, dt: datetime) -> datetime:
        return dt + (self._cal.get_next_market_close(self._pinned) - self._pinned)

    def is_market_open(self, dt: datetime) -> bool:
        return self._cal.is_market_open(self._pinned)


ET = ZoneInfo("America/New_York")

# A Wednesday NYSE session (09:30-16:00 ET).
_SESSION_DAY = date(2026, 9, 16)


def _at(hour: int, minute: int, *, day: date = _SESSION_DAY) -> _CalendarAt:
    return _CalendarAt(datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET))


class TestSweepOutsideMarketHours:
    """The unfilled-limit sweep is an intra-session rule.

    An order cannot fill while the market is shut, so neither the
    close-approaching cancel nor the reprice/attempt clock may advance then.
    Before this was enforced, every buy placed by the daily run (~16:22 ET,
    after the bell) was cancelled minutes later and no BUY could ever fill.
    """

    def _mgr(self, **kwargs) -> OrderManager:
        executor = AsyncMock()
        executor.cancel_order = AsyncMock(return_value=True)
        return OrderManager(
            executor=executor,
            redis_client=AsyncMock(),
            db_session=MagicMock(),
            reprice_interval_minutes=kwargs.get("reprice_interval_minutes", 60),
            max_reprice_attempts=kwargs.get("max_reprice_attempts", 3),
        )

    def _open_order(self, mgr: OrderManager, *, age_minutes: int, reprice_count: int = 0):
        now = datetime.now(timezone.utc)
        mgr.open_orders["order-001"] = {
            "ticker": "AAPL",
            "quantity": 50,
            "limit_price": 150.0,
            "placed_at": now - timedelta(minutes=age_minutes),
            "last_repriced_at": now - timedelta(minutes=age_minutes),
            "reprice_count": reprice_count,
            "recommendation_id": "rec-001",
            "order_type": "limit",
        }

    def test_buy_placed_after_the_bell_is_not_cancelled(self):
        """The production defect: the 04:15 run places at ~16:22 ET and the
        next sweep cancelled every order because time_to_close went negative."""
        mgr = self._mgr()
        self._open_order(mgr, age_minutes=12)

        actions = mgr.check_unfilled_orders({"AAPL": 151.0}, _at(16, 34))

        assert actions == []

    def test_order_resting_overnight_is_not_aged_toward_the_attempt_cancel(self):
        """Rule 2/3 must not run overnight either — otherwise an order that
        survives the close-cancel is still killed by the reprice clock hours
        before the next open, which is the same defect one rule further on."""
        mgr = self._mgr()
        self._open_order(mgr, age_minutes=180)

        actions = mgr.check_unfilled_orders({"AAPL": 151.0}, _at(20, 0))

        assert actions == []

    def test_order_waiting_for_the_open_is_not_aged_premarket(self):
        """04:15 ET premarket: time_to_close is POSITIVE here, so a lower bound
        on rule 1 alone would not save the order — rule 3 still ages it."""
        mgr = self._mgr()
        self._open_order(mgr, age_minutes=12 * 60)

        actions = mgr.check_unfilled_orders(
            {"AAPL": 151.0}, _at(4, 15, day=date(2026, 9, 17))
        )

        assert actions == []

    def test_order_is_not_aged_over_the_weekend(self):
        mgr = self._mgr()
        self._open_order(mgr, age_minutes=48 * 60)

        actions = mgr.check_unfilled_orders(
            {"AAPL": 151.0}, _at(12, 0, day=date(2026, 9, 19))
        )

        assert actions == []

    def test_cancel_still_fires_in_the_final_minutes_of_a_session(self):
        """The rule the sweep exists for must survive the fix."""
        mgr = self._mgr()
        self._open_order(mgr, age_minutes=30)

        actions = mgr.check_unfilled_orders({"AAPL": 151.0}, _at(15, 50))

        assert len(actions) == 1
        assert actions[0].action_type == "cancel"

    def test_reprice_still_fires_mid_session(self):
        mgr = self._mgr()
        self._open_order(mgr, age_minutes=61)

        actions = mgr.check_unfilled_orders({"AAPL": 151.0}, _at(11, 0))

        assert len(actions) == 1
        assert actions[0].action_type == "reprice"

    async def test_sweep_outside_hours_cancels_nothing_and_does_not_age(self):
        """End-to-end through the sweep: no broker cancel, clock unmoved."""
        mgr = self._mgr()
        self._open_order(mgr, age_minutes=180)

        cancelled = await mgr.sweep_unfilled_orders({"AAPL": 151.0}, _at(20, 0))

        assert cancelled == 0
        mgr._executor.cancel_order.assert_not_awaited()
        assert "order-001" in mgr.open_orders
        assert mgr.open_orders["order-001"]["reprice_count"] == 0
