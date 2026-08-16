"""KAN-19 — GTC stops resting at IB, sized by the IPS stop rule.

The spike (KAN-18) established that a GTC stop survives a Gateway process
restart, which is the whole reason for placing one: it is enforced by IB when
Redis, Postgres, Docker, or the host is not there to enforce anything.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.execution.ib_executor import IBExecutor
from shared.broker_state import BrokerOpenOrder


class TestExecutorStopPath:
    """``submit_stop_order`` — a new method, not a parameter on the existing two."""

    def _executor(self) -> IBExecutor:
        executor = IBExecutor("h", 7497, 1, account_id="DUN551088")
        executor._ib = MagicMock()
        trade = MagicMock()
        trade.order.orderId = 4242
        executor._ib.placeOrder.return_value = trade
        return executor

    async def test_places_a_gtc_sell_stop_at_the_requested_price(self):
        executor = self._executor()

        order_id = await executor.submit_stop_order(
            "AAPL", 21, 187.55, recommendation_id="stop-DUN551088-core-265598-0"
        )

        assert order_id == "4242"
        _, order = executor._ib.placeOrder.call_args[0]
        assert order.orderType == "STP"
        assert order.action == "SELL"
        assert order.totalQuantity == 21
        assert order.auxPrice == pytest.approx(187.55)
        assert order.tif == "GTC"
        assert order.orderRef == "stop-DUN551088-core-265598-0"

    async def test_stamps_the_configured_account(self):
        """KAN-11: a protective order on the wrong account protects nothing."""
        executor = self._executor()

        await executor.submit_stop_order("AAPL", 21, 187.55)

        _, order = executor._ib.placeOrder.call_args[0]
        assert order.account == "DUN551088"

    async def test_outside_rth_is_deliberate_not_inherited(self):
        """Spike Q1: outsideRth false leaves the stop dormant outside RTH.

        The caller decides, so the gap exposure is a recorded choice rather
        than an IB default nobody looked at.
        """
        executor = self._executor()

        await executor.submit_stop_order("AAPL", 21, 187.55, outside_rth=True)

        _, order = executor._ib.placeOrder.call_args[0]
        assert order.outsideRth is True

    async def test_registers_the_trade_so_its_fill_is_attributed(self):
        executor = self._executor()

        order_id = await executor.submit_stop_order("AAPL", 21, 187.55)

        assert order_id in executor._trades
        assert executor._trade_meta[order_id] == ("AAPL", "sell")

    async def test_rounds_to_whole_shares_like_every_other_order(self):
        """Spike Q5 left the fractional case untested; it must not diverge."""
        executor = self._executor()

        await executor.submit_stop_order("AAPL", 21.6, 187.55)

        _, order = executor._ib.placeOrder.call_args[0]
        assert order.totalQuantity == 21

    async def test_refuses_to_place_when_disconnected(self):
        """A stop that was never placed must never report success."""
        from services.execution.ib_executor import NotConnectedError

        executor = IBExecutor("h", 7497, 1)
        executor._ib = None

        with pytest.raises(NotConnectedError):
            await executor.submit_stop_order("AAPL", 21, 187.55)


class TestBrokerOpenOrderDescribesAStop:
    """AC5 — a resting stop must be describable by the reader KAN-20 will use."""

    def test_carries_order_type_aux_price_and_tif(self):
        order = BrokerOpenOrder(
            account_id="DUN551088",
            ib_order_id="4242",
            con_id=265598,
            symbol="AAPL",
            action="SELL",
            total_quantity=21.0,
            filled_quantity=0.0,
            status="PreSubmitted",
            order_type="STP",
            aux_price=187.55,
            tif="GTC",
        )

        assert order.order_type == "STP"
        assert order.aux_price == pytest.approx(187.55)
        assert order.tif == "GTC"

    def test_the_new_fields_are_optional_so_existing_readers_still_build_one(self):
        order = BrokerOpenOrder(
            account_id="DUN551088",
            ib_order_id="7",
            con_id=265598,
            symbol="AAPL",
            action="BUY",
            total_quantity=10.0,
            filled_quantity=0.0,
            status="Submitted",
        )

        assert order.order_type is None
        assert order.aux_price is None
        assert order.tif is None


class TestSnapshotReadsStopParameters:
    """The account snapshot must surface what a resting stop actually is."""

    def _ib_holding_a_resting_stop(self):
        from tests.services.execution.test_ib_account import _fake_ib

        ib = _fake_ib()
        contract = SimpleNamespace(
            conId=265598, symbol="AAPL", localSymbol="AAPL",
            exchange="SMART", currency="USD",
        )
        stop = SimpleNamespace(
            contract=contract,
            order=SimpleNamespace(
                orderId=4242,
                account="DUN551088",
                action="SELL",
                totalQuantity=21.0,
                orderType="STP",
                auxPrice=187.55,
                tif="GTC",
            ),
            orderStatus=SimpleNamespace(filled=0.0, status="PreSubmitted"),
        )
        ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[stop])
        return ib

    async def test_snapshot_populates_the_stop_fields(self):
        from services.execution.ib_account import IBAccountReader

        snapshot = await IBAccountReader(
            self._ib_holding_a_resting_stop(),
            expected_mode="paper",
            expected_base_currency="SGD",
            trading_currency="USD",
        ).snapshot()

        order = snapshot.open_orders["4242"]
        assert order.order_type == "STP"
        assert order.aux_price == pytest.approx(187.55)
        assert order.tif == "GTC"

    async def test_ibs_unset_sentinel_reads_as_absent_not_as_a_price(self):
        """Spike Q3: IB fills unset numeric order fields with DBL_MAX.

        Taken literally that is a stop price of 1.8e308, which would make a
        naive verifier call an unprotected position protected.
        """
        import sys

        from services.execution.ib_account import IBAccountReader

        ib = self._ib_holding_a_resting_stop()
        (await ib.reqAllOpenOrdersAsync())[0].order.auxPrice = sys.float_info.max * 2

        snapshot = await IBAccountReader(
            ib,
            expected_mode="paper",
            expected_base_currency="SGD",
            trading_currency="USD",
        ).snapshot()

        assert snapshot.open_orders["4242"].aux_price is None


class TestOrderManagerTracksStops:
    """A stop the execution client placed must be reachable by the kill path.

    Spike Q4: ``cancel_all_orders`` iterates ``open_orders``, and a stop that
    is not in there survives the kill, then rests against a position the
    liquidation already flattened — selling short on trigger.
    """

    def _manager(self):
        from services.execution.order_manager import OrderManager

        executor = AsyncMock()
        executor.find_order_by_ref = AsyncMock(return_value=None)
        executor.submit_stop_order = AsyncMock(return_value="4242")
        return OrderManager(
            executor=executor,
            redis_client=AsyncMock(),
            db_session=MagicMock(),
        ), executor

    async def test_submits_the_stop_and_tracks_it_as_open(self):
        mgr, executor = self._manager()

        order_id = await mgr.submit_stop(
            ticker="AAPL",
            quantity=21,
            stop_price=187.55,
            recommendation_id="stop-1",
        )

        assert order_id == "4242"
        executor.submit_stop_order.assert_awaited_once()
        assert mgr.open_orders["4242"]["order_type"] == "stop"
        assert mgr.open_orders["4242"]["quantity"] == 21
        assert mgr.open_orders["4242"]["stop_price"] == pytest.approx(187.55)

    async def test_the_kill_path_cancels_a_resting_stop(self):
        mgr, executor = self._manager()
        await mgr.submit_stop(
            ticker="AAPL", quantity=21, stop_price=187.55,
            recommendation_id="stop-1",
        )

        cancelled = await mgr.cancel_all_orders()

        assert cancelled == ["4242"]
        executor.cancel_order.assert_awaited_once_with("4242")
        assert mgr.open_orders == {}

    async def test_a_repeat_placement_is_idempotent(self):
        mgr, executor = self._manager()

        first = await mgr.submit_stop(
            ticker="AAPL", quantity=21, stop_price=187.55,
            recommendation_id="stop-1",
        )
        second = await mgr.submit_stop(
            ticker="AAPL", quantity=21, stop_price=187.55,
            recommendation_id="stop-1",
        )

        assert first == second
        assert executor.submit_stop_order.await_count == 1

    async def test_passes_the_tif_and_rth_choice_through(self):
        mgr, executor = self._manager()

        await mgr.submit_stop(
            ticker="AAPL", quantity=21, stop_price=187.55,
            recommendation_id="stop-1", tif="GTC", outside_rth=True,
        )

        kwargs = executor.submit_stop_order.await_args.kwargs
        assert kwargs["tif"] == "GTC"
        assert kwargs["outside_rth"] is True


class TestUnfilledSweepLeavesStopsAlone:
    """A GTC stop is *supposed* to rest unfilled for weeks."""

    def _manager_holding_a_stop(self):
        from datetime import datetime, timedelta, timezone

        from services.execution.order_manager import OrderManager

        mgr = OrderManager(
            executor=AsyncMock(), redis_client=AsyncMock(), db_session=MagicMock()
        )
        now = datetime.now(timezone.utc)
        mgr.open_orders["4242"] = {
            "ticker": "AAPL",
            "quantity": 21,
            "limit_price": None,
            "stop_price": 187.55,
            "placed_at": now - timedelta(days=9),
            "last_repriced_at": now - timedelta(days=9),
            "reprice_count": 0,
            "recommendation_id": "stop-1",
            "order_type": "stop",
        }
        return mgr, now

    def test_a_resting_stop_is_never_repriced_or_aged(self):
        mgr, now = self._manager_holding_a_stop()
        calendar = MagicMock()
        calendar.get_next_market_close.return_value = now + __import__(
            "datetime"
        ).timedelta(hours=3)

        assert mgr.check_unfilled_orders({"AAPL": 190.0}, calendar) == []

    def test_a_resting_stop_is_not_cancelled_at_the_close(self):
        """The 15-minutes-to-close cancel would strip overnight protection."""
        from datetime import timedelta

        mgr, now = self._manager_holding_a_stop()
        calendar = MagicMock()
        calendar.get_next_market_close.return_value = now + timedelta(minutes=5)

        assert mgr.check_unfilled_orders({"AAPL": 190.0}, calendar) == []

    def test_a_restored_limit_order_is_still_swept(self):
        """Regression guard: ``restore_submission`` writes no ``order_type``.

        Skipping "anything not a limit" instead of "market or stop" would make
        every order recovered across a restart invisible to the sweep.
        """
        from datetime import timedelta

        mgr, now = self._manager_holding_a_stop()
        mgr.open_orders.clear()
        mgr.restore_submission(
            "rec-1", "order-1", ticker="AAPL", quantity=10, limit_price=150.0
        )
        calendar = MagicMock()
        calendar.get_next_market_close.return_value = now + timedelta(minutes=5)

        actions = mgr.check_unfilled_orders({"AAPL": 151.0}, calendar)

        assert [a.action_type for a in actions] == ["cancel"]
