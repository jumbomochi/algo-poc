"""KAN-98: the execution sweep and the live fill path, side by side.

KAN-95 put the sweep on the live IB session. Two leftovers from its review:

* IB answers ``reqExecutions`` with a ``commissionReport`` per execution, and
  ib_insync emits that into the tracked trade's ``commissionReportEvent`` —
  the live fill callback. Every pass re-ran the live path for every booked
  execution, and a genuinely missed fill could be booked by it with no
  ``recovery_source`` and no page.
* The loop awaited the sweep inline, so a hung ``reqExecutions`` held
  ``stream:kill`` unread for up to 30 s.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from services.execution.execution_sweep import RECOVERY_SOURCE_SWEEP
from services.execution.ib_executor import IBExecutor
from services.execution.runner import ExecutionServiceRunner
from shared.config import AppConfig, ExecutionConfig, IBConfig
from shared.schemas.messages import AlertMessage, FillMessage

FILLED_AT = datetime(2026, 10, 5, 13, 30, 2, tzinfo=timezone.utc)


class Event:
    def __init__(self) -> None:
        self.callbacks = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self

    def emit(self, *args) -> None:
        for callback in self.callbacks:
            callback(*args)


def _trade(*, with_fill_event=True):
    trade = SimpleNamespace(
        order=SimpleNamespace(orderId=240),
        commissionReportEvent=Event(),
        statusEvent=Event(),
        isDone=lambda: True,
    )
    if with_fill_event:
        trade.fillEvent = Event()
    return trade


def _fill(exec_id="0001f4e8.amat.01"):
    return SimpleNamespace(
        execution=SimpleNamespace(
            execId=exec_id, acctNumber="DUN551088", shares=5.0, cumQty=5.0,
            price=539.10, time=FILLED_AT,
        ),
        contract=SimpleNamespace(conId=270639, exchange="NASDAQ", currency="USD"),
    )


def _report():
    return SimpleNamespace(commission=1.0, currency="USD")


def _executor_with_trade(trade):
    executor = IBExecutor("h", 7497, 1)
    executor._ib = MagicMock()
    executor._ib.accountValues.return_value = []
    handler = AsyncMock()
    executor.set_fill_handler(handler)
    executor._register_trade("240", trade, ticker="AMAT", side="buy")
    return executor, handler


class TestTheExecutorTellsReplaysFromLiveFills:
    async def test_a_live_fill_carries_no_recovery_source(self):
        trade = _trade()
        _, handler = _executor_with_trade(trade)

        trade.fillEvent.emit(trade, _fill())
        trade.commissionReportEvent.emit(trade, _fill(), _report())
        await asyncio.sleep(0)

        handler.assert_awaited_once()
        assert handler.await_args.args[0].get("recovery_source") is None

    async def test_a_replay_of_a_reported_execution_is_not_handed_over_again(self):
        trade = _trade()
        _, handler = _executor_with_trade(trade)
        trade.fillEvent.emit(trade, _fill())
        trade.commissionReportEvent.emit(trade, _fill(), _report())
        await asyncio.sleep(0)

        # The reqExecutions reply replays the commission report.
        trade.commissionReportEvent.emit(trade, _fill(), _report())
        await asyncio.sleep(0)

        assert handler.await_count == 1

    async def test_a_replay_after_a_failed_delivery_still_gets_through(self):
        """The duplicate delivery is that fill's retry; suppressing it would
        leave a fill whose publish failed to wait for the next sweep."""
        trade = _trade()
        executor, handler = _executor_with_trade(trade)
        handler.side_effect = [RuntimeError("transient Redis failure"), None]
        executor._logger = MagicMock()
        trade.fillEvent.emit(trade, _fill())
        trade.commissionReportEvent.emit(trade, _fill(), _report())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        trade.commissionReportEvent.emit(trade, _fill(), _report())
        await asyncio.sleep(0)

        assert handler.await_count == 2

    async def test_an_execution_never_seen_live_is_stamped_as_recovered(self):
        trade = _trade()
        _, handler = _executor_with_trade(trade)

        # Missed live; first seen as the reqExecutions replay.
        trade.commissionReportEvent.emit(trade, _fill(), _report())
        await asyncio.sleep(0)

        handler.assert_awaited_once()
        assert handler.await_args.args[0]["recovery_source"] == RECOVERY_SOURCE_SWEEP

    async def test_a_trade_without_a_fill_event_still_registers(self):
        trade = _trade(with_fill_event=False)
        _, handler = _executor_with_trade(trade)

        trade.commissionReportEvent.emit(trade, _fill(), _report())
        await asyncio.sleep(0)

        handler.assert_awaited_once()


def _runner():
    config = MagicMock(spec=AppConfig)
    config.execution = ExecutionConfig()
    config.ib = IBConfig()
    config.mode = "paper"
    config.risk = MagicMock()
    order_manager = AsyncMock()
    order_manager.open_orders = {}
    redis = AsyncMock()
    runner = ExecutionServiceRunner(
        config=config, redis_client=redis, order_manager=order_manager
    )
    return runner, order_manager, redis


def _fill_info(**overrides):
    info = {
        "execution_id": "0001f4e8.amat.01", "account_id": "DUN551088",
        "timestamp": FILLED_AT, "order_id": "240", "con_id": 270639,
        "ticker": "AMAT", "exchange": "NASDAQ", "currency": "USD",
        "side": "buy", "quantity": 5.0, "cumulative_quantity": 5.0,
        "fill_price": 539.10, "commission": 1.0, "commission_currency": "USD",
        "commission_trading": 1.0, "commission_fx_base_per_trading": None,
        "order_done": True,
    }
    info.update(overrides)
    return info


def _published(redis, stream):
    return [c.args[1] for c in redis.publish.await_args_list if c.args[0] == stream]


class TestTheLivePathKeepsProvenance:
    async def test_a_recovered_execution_is_published_with_its_source_and_paged(self):
        runner, _, redis = _runner()

        await runner.handle_ib_fill(_fill_info(recovery_source=RECOVERY_SOURCE_SWEEP))

        [payload] = _published(redis, "stream:fills")
        assert FillMessage.from_stream_dict(payload).recovery_source == RECOVERY_SOURCE_SWEEP
        [alert] = [AlertMessage.from_stream_dict(p)
                   for p in _published(redis, "stream:alerts")]
        assert alert.event_type == "execution_sweep_recovered"
        assert alert.priority == "medium"

    async def test_a_live_fill_is_not_paged(self):
        runner, _, redis = _runner()

        await runner.handle_ib_fill(_fill_info())

        assert len(_published(redis, "stream:fills")) == 1
        assert _published(redis, "stream:alerts") == []

    async def test_a_failed_page_does_not_lose_the_fill(self):
        runner, _, redis = _runner()

        async def publish(stream, payload):
            if stream == "stream:alerts":
                raise ConnectionError("redis down")
            return "id"

        redis.publish = AsyncMock(side_effect=publish)

        await runner.handle_ib_fill(_fill_info(recovery_source=RECOVERY_SOURCE_SWEEP))

        assert any(c.args[0] == "stream:fills" for c in redis.publish.await_args_list)


class TestAHungSweepNeverHoldsAKill:
    async def test_the_kill_stream_is_read_while_the_sweep_hangs(self):
        runner, _, redis = _runner()
        runner.setup = AsyncMock()
        runner.shutdown = AsyncMock()
        sweep_started = asyncio.Event()

        async def hung_sweep(now):
            sweep_started.set()
            await asyncio.sleep(3600)

        runner.maybe_run_execution_sweep = hung_sweep
        kill_reads = []

        async def read_group(stream, *args, **kwargs):
            await asyncio.sleep(0)
            if stream == "stream:kill" and sweep_started.is_set():
                kill_reads.append(stream)
                runner._running = False
            return []

        redis.read_group = read_group
        with patch("services.execution.runner.write_heartbeat"):
            await asyncio.wait_for(runner.run(), timeout=1)

        assert kill_reads == ["stream:kill"]

    async def test_only_one_sweep_runs_at_a_time(self):
        runner, _, _ = _runner()
        starts = []

        async def hung_sweep(now):
            starts.append(now)
            await asyncio.sleep(3600)

        runner.maybe_run_execution_sweep = hung_sweep

        assert runner._start_execution_sweep(0.0) is True
        await asyncio.sleep(0)
        assert runner._start_execution_sweep(1.0) is False
        await asyncio.sleep(0)

        assert starts == [0.0]
        runner._execution_sweep_task.cancel()

    async def test_shutdown_cancels_a_running_sweep(self):
        runner, _, _ = _runner()

        async def hung_sweep(now):
            await asyncio.sleep(3600)

        runner.maybe_run_execution_sweep = hung_sweep
        runner._start_execution_sweep(0.0)
        await asyncio.sleep(0)
        task = runner._execution_sweep_task

        await runner.shutdown()

        assert task.cancelled()
