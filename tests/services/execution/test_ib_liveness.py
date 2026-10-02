"""KAN-94: the execution loop keeps its IB connection alive on its own.

On 2026-09-25 the Gateway restarted at 13:53 SGT, between the 04:21 order
placement and the 21:30 open. Nothing in the idle loop called the executor, so
it never reconnected and IB's fill for order 219 (XLC) went to a dead socket.
These tests pin the liveness check that closes that gap.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.execution.ib_executor import (
    IBExecutor,
    NotConnectedError,
    WrongAccountTypeError,
)
from services.execution.order_manager import OrderManager
from services.execution.runner import ExecutionServiceRunner
from shared.config import AppConfig, ExecutionConfig, IBConfig
from shared.schemas.messages import AlertMessage

EXECUTED_AT = datetime(2026, 9, 25, 13, 30, 26, tzinfo=timezone.utc)


class Event:
    def __init__(self) -> None:
        self.callbacks = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self

    def emit(self, *args) -> None:
        for callback in self.callbacks:
            callback(*args)


def _runner(**execution_overrides):
    config = MagicMock(spec=AppConfig)
    config.execution = ExecutionConfig(**execution_overrides)
    config.ib = IBConfig()
    config.mode = "paper"
    config.risk = MagicMock()
    order_manager = AsyncMock()
    order_manager.open_orders = {"219": object()}
    redis = AsyncMock()
    runner = ExecutionServiceRunner(
        config=config, redis_client=redis, order_manager=order_manager
    )
    return runner, order_manager, redis


def _alerts(redis) -> list[AlertMessage]:
    return [
        AlertMessage.from_stream_dict(c.args[1])
        for c in redis.publish.await_args_list
        if c.args[0] == "stream:alerts"
    ]


class TestTheLoopReconnectsWithNothingInFlight:
    async def test_a_dropped_socket_is_reconnected_with_no_order_on_any_stream(self):
        runner, order_manager, _ = _runner()
        order_manager.ensure_broker_connection = AsyncMock(return_value=True)

        assert await runner.maybe_check_ib_connection(0.0) is True

        order_manager.ensure_broker_connection.assert_awaited_once()

    async def test_the_check_waits_out_its_interval(self):
        runner, order_manager, _ = _runner(ib_liveness_interval_seconds=60)
        order_manager.ensure_broker_connection = AsyncMock(return_value=False)

        assert await runner.maybe_check_ib_connection(0.0) is True
        assert await runner.maybe_check_ib_connection(30.0) is False
        assert await runner.maybe_check_ib_connection(60.0) is True
        assert order_manager.ensure_broker_connection.await_count == 2

    async def test_a_healthy_connection_publishes_nothing(self):
        runner, order_manager, redis = _runner()
        order_manager.ensure_broker_connection = AsyncMock(return_value=False)

        for now in (0.0, 60.0, 120.0):
            await runner.maybe_check_ib_connection(now)

        assert _alerts(redis) == []


class TestAPersistentDisconnectPagesOnce:
    async def test_a_failing_reconnect_never_raises_out_of_the_loop(self):
        runner, order_manager, redis = _runner()
        order_manager.ensure_broker_connection = AsyncMock(
            side_effect=NotConnectedError("Gateway restarting")
        )

        assert await runner.maybe_check_ib_connection(0.0) is True
        assert _alerts(redis) == []

    async def test_one_alert_after_the_threshold_and_none_before(self):
        runner, order_manager, redis = _runner(
            ib_liveness_interval_seconds=60, ib_disconnect_alert_seconds=600
        )
        order_manager.ensure_broker_connection = AsyncMock(
            side_effect=NotConnectedError("Gateway restarting")
        )

        for now in range(0, 600, 60):
            await runner.maybe_check_ib_connection(float(now))
        assert _alerts(redis) == []

        await runner.maybe_check_ib_connection(600.0)
        await runner.maybe_check_ib_connection(660.0)
        await runner.maybe_check_ib_connection(1260.0)

        alerts = _alerts(redis)
        assert [a.event_type for a in alerts] == ["ib_disconnected"]
        assert alerts[0].priority == "high"
        assert "10 min" in alerts[0].message
        assert alerts[0].context["tracked_open_orders"] == "1"

    async def test_recovery_after_a_page_says_so_and_rearms(self):
        runner, order_manager, redis = _runner(
            ib_liveness_interval_seconds=60, ib_disconnect_alert_seconds=600
        )
        order_manager.ensure_broker_connection = AsyncMock(
            side_effect=NotConnectedError("down")
        )
        await runner.maybe_check_ib_connection(0.0)
        await runner.maybe_check_ib_connection(600.0)

        order_manager.ensure_broker_connection = AsyncMock(return_value=True)
        await runner.maybe_check_ib_connection(660.0)

        order_manager.ensure_broker_connection = AsyncMock(
            side_effect=NotConnectedError("down again")
        )
        await runner.maybe_check_ib_connection(720.0)
        await runner.maybe_check_ib_connection(1320.0)

        assert [a.event_type for a in _alerts(redis)] == [
            "ib_disconnected",
            "ib_reconnected",
            "ib_disconnected",
        ]
        assert _alerts(redis)[1].priority == "low"

    async def test_a_short_blip_that_never_paged_sends_no_recovery_notice(self):
        runner, order_manager, redis = _runner(ib_disconnect_alert_seconds=600)
        order_manager.ensure_broker_connection = AsyncMock(
            side_effect=NotConnectedError("down")
        )
        await runner.maybe_check_ib_connection(0.0)
        order_manager.ensure_broker_connection = AsyncMock(return_value=True)
        await runner.maybe_check_ib_connection(60.0)

        assert _alerts(redis) == []

    async def test_a_wrong_account_session_is_not_swallowed(self):
        runner, order_manager, _ = _runner()
        order_manager.ensure_broker_connection = AsyncMock(
            side_effect=WrongAccountTypeError("LIVE session on the paper port")
        )

        with pytest.raises(WrongAccountTypeError):
            await runner.maybe_check_ib_connection(0.0)


class TestTheMainLoopRunsTheCheck:
    async def test_run_calls_the_liveness_check_each_iteration(self):
        runner, order_manager, redis = _runner()
        runner.setup = AsyncMock()

        async def read_group(*args, **kwargs):
            await asyncio.sleep(0)  # a real XREADGROUP suspends; let wait_for fire
            return []

        redis.read_group = read_group
        calls = []

        async def check(now):
            calls.append(now)
            runner._running = False
            return True

        runner.maybe_check_ib_connection = check
        with patch("services.execution.runner.write_heartbeat"):
            # Bounded: a loop that never calls the check would spin forever.
            await asyncio.wait_for(runner.run(), timeout=5)

        assert len(calls) == 1


class TestTheExecutorReconnectsOnDemand:
    async def test_a_connected_executor_makes_no_ib_call(self):
        executor = IBExecutor("h", 7497, 1)
        executor._ib = MagicMock()
        executor._ib.isConnected.return_value = True
        executor.connect = AsyncMock()

        assert await executor.ensure_connected() is False

        executor.connect.assert_not_awaited()
        executor._ib.reqOpenOrders.assert_not_called()

    async def test_a_dropped_executor_reconnects_and_a_later_fill_is_published_once(self):
        executor = IBExecutor("h", 7497, 1)
        handler = AsyncMock()
        executor.set_fill_handler(handler)
        executor.set_order_status_handler(AsyncMock())

        old_trade = SimpleNamespace(
            order=SimpleNamespace(orderId=219),
            commissionReportEvent=Event(),
            statusEvent=Event(),
        )
        executor._register_trade("219", old_trade, "XLC", "buy")
        executor._ib = MagicMock()
        executor._ib.isConnected.return_value = False  # Gateway restarted

        new_trade = SimpleNamespace(
            order=SimpleNamespace(orderId=219),
            orderStatus=SimpleNamespace(status="PreSubmitted", filled=0),
            commissionReportEvent=Event(),
            statusEvent=Event(),
            isDone=lambda: False,
        )
        fake_ib = MagicMock()
        fake_ib.connectAsync = AsyncMock()
        fake_ib.managedAccounts.return_value = ["DUN551088"]
        fake_ib.openTrades.return_value = [new_trade]
        fake_ib.accountValues.return_value = []

        with patch("ib_insync.IB", return_value=fake_ib):
            assert await executor.ensure_connected() is True

        fill = SimpleNamespace(
            execution=SimpleNamespace(
                execId="0001f4e8.xlc.01",
                acctNumber="DUN551088",
                shares=26,
                cumQty=26,
                price=113.35,
                time=EXECUTED_AT,
            ),
            contract=SimpleNamespace(conId=322317077, exchange="ARCA", currency="USD"),
            commissionReport=SimpleNamespace(commission=0.0, currency=""),
        )
        new_trade.commissionReportEvent.emit(
            new_trade, fill, SimpleNamespace(commission=1.0, currency="USD")
        )
        await asyncio.sleep(0)

        assert handler.await_count == 1
        assert handler.await_args.args[0]["quantity"] == 26

    async def test_a_failed_reconnect_raises_not_connected(self):
        executor = IBExecutor("h", 7497, 1)
        executor._ib = None
        fake_ib = MagicMock()
        fake_ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError())

        with patch("ib_insync.IB", return_value=fake_ib):
            with pytest.raises(NotConnectedError):
                await executor.ensure_connected()


class TestTheOrderManagerForwards:
    async def test_ensure_broker_connection_delegates_to_the_executor(self):
        executor = AsyncMock()
        executor.ensure_connected = AsyncMock(return_value=True)
        manager = OrderManager(
            executor=executor, redis_client=AsyncMock(), db_session=MagicMock()
        )

        assert await manager.ensure_broker_connection() is True
        executor.ensure_connected.assert_awaited_once()


class TestReviewHardening:
    """Post-review: the check must never be quieter than what it replaced."""

    async def test_a_wrong_account_session_pages_before_it_stops_the_service(self):
        runner, order_manager, redis = _runner()
        order_manager.ensure_broker_connection = AsyncMock(
            side_effect=WrongAccountTypeError("LIVE account U123 on port 7497")
        )

        with pytest.raises(WrongAccountTypeError):
            await runner.maybe_check_ib_connection(0.0)

        (alert,) = _alerts(redis)
        assert alert.event_type == "ib_wrong_account"
        assert alert.priority == "critical"
        assert "U123" in alert.message

    async def test_a_redis_failure_while_paging_cannot_hide_a_wrong_account(self):
        runner, order_manager, redis = _runner()
        order_manager.ensure_broker_connection = AsyncMock(
            side_effect=WrongAccountTypeError("LIVE account U123")
        )
        redis.publish = AsyncMock(side_effect=ConnectionError("redis down"))

        with pytest.raises(WrongAccountTypeError):
            await runner.maybe_check_ib_connection(0.0)

    async def test_a_redis_failure_while_paging_never_escapes_the_check(self):
        runner, order_manager, redis = _runner(ib_disconnect_alert_seconds=60)
        order_manager.ensure_broker_connection = AsyncMock(
            side_effect=NotConnectedError("down")
        )
        redis.publish = AsyncMock(side_effect=ConnectionError("redis down"))
        await runner.maybe_check_ib_connection(0.0)

        assert await runner.maybe_check_ib_connection(60.0) is True

        order_manager.ensure_broker_connection = AsyncMock(return_value=True)
        assert await runner.maybe_check_ib_connection(120.0) is True

    async def test_a_liveness_reconnect_is_logged_for_the_operator(self):
        runner, order_manager, _ = _runner()
        order_manager.ensure_broker_connection = AsyncMock(return_value=True)
        runner._logger = MagicMock()

        await runner.maybe_check_ib_connection(0.0)

        messages = [c.args[0] for c in runner._logger.info.call_args_list]
        assert "IB session restored by the liveness check" in messages

    def test_a_zero_interval_is_refused_not_silently_clamped(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ExecutionConfig(ib_liveness_interval_seconds=0)
        with pytest.raises(ValidationError):
            ExecutionConfig(ib_disconnect_alert_seconds=0)


class TestTheLoopSurvivesAndStops:
    def _looping_runner(self, iterations: int):
        runner, order_manager, redis = _runner(ib_liveness_interval_seconds=1)
        runner.setup = AsyncMock()
        runner.shutdown = AsyncMock()
        reads = {"n": 0}

        async def read_group(*args, **kwargs):
            reads["n"] += 1
            if reads["n"] >= iterations * 2:  # two stream reads per iteration
                runner._running = False
            await asyncio.sleep(0)
            return []

        redis.read_group = read_group
        return runner, order_manager, redis, reads

    async def test_a_reconnect_failing_every_time_keeps_the_loop_running(self):
        runner, order_manager, _, reads = self._looping_runner(iterations=3)
        order_manager.ensure_broker_connection = AsyncMock(
            side_effect=NotConnectedError("down")
        )
        # Every iteration checks; config refuses 0, so set the runner directly.
        runner._ib_liveness_interval_seconds = 0

        with patch("services.execution.runner.write_heartbeat") as heartbeat:
            await asyncio.wait_for(runner.run(), timeout=5)

        assert heartbeat.call_count == 3
        assert reads["n"] == 6
        assert order_manager.ensure_broker_connection.await_count == 3

    async def test_a_wrong_account_stop_is_logged_with_its_cause(self):
        runner, order_manager, _, _ = self._looping_runner(iterations=5)
        order_manager.ensure_broker_connection = AsyncMock(
            side_effect=WrongAccountTypeError("LIVE account U123")
        )
        runner._logger = MagicMock()

        with patch("services.execution.runner.write_heartbeat"):
            await asyncio.wait_for(runner.run(), timeout=5)

        runner._logger.exception.assert_called()
        runner.shutdown.assert_awaited_once()
