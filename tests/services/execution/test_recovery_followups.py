"""KAN-102: the KAN-98 / KAN-95 reviewer items on fill recovery.

* Live-path recoveries paged one alert per fill, while the sweep's own page
  is batched — a Gateway restart that dropped N fills sent N pages.
* A live fill could be stamped ``recovery_source`` (and paged "probably
  missed") in three narrow windows:
  (a) a live execution answered first by an in-flight sweep's reply, after
      which ib_insync fires no ``fillEvent`` for the live copy;
  (b) a live ``execDetails`` processed inside ``connectAsync``, before the
      trade callbacks are re-bound;
  (c) earlier executions of a restored, partly filled order, replayed by the
      first sweep after a restart.
* A replay arriving while the first delivery was still in flight reached the
  runner's dedupe and logged "Duplicate IB execution ignored".

Every executor test here drives a real ib_insync wrapper, with the real live
sequence (``execDetails`` with reqId -1, then ``commissionReport``) and the
real reply sequence (``execDetails`` with the request's id).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.execution.execution_sweep import RECOVERY_SOURCE_SWEEP
from services.execution.ib_executor import IBExecutor
from services.execution.order_manager import OrderManager
from services.execution.runner import (
    RECOVERY_PAGE_SETTLE_SECONDS,
    ExecutionServiceRunner,
)
from services.portfolio_accounting.projector import FillProjector
from shared.config import AppConfig, ExecutionConfig, IBConfig
from shared.models import Base
from shared.order_ledger import OrderLedger
from shared.schemas.messages import AlertMessage, FillMessage
from tests.services.execution.test_execution_sweep_in_service import (
    XLC_REC,
    _seed_xlc_submitted,
    _swept,
)

ACCOUNT = "DUN551088"
PERM_ID = 753926985
FILLED_AT = datetime(2026, 10, 5, 13, 30, 2, tzinfo=timezone.utc)
SWEEP_REQ_ID = 77


# --------------------------------------------------------------------------
# A real ib_insync wrapper behind a faked socket
# --------------------------------------------------------------------------


def _contract():
    from ib_insync import Contract

    return Contract(conId=322317077, symbol="XLC", exchange="ARCA",
                    currency="USD", secType="STK")


def _execution(exec_id, shares=10.0, cum_qty=10.0):
    from ib_insync import Execution

    return Execution(
        execId=exec_id, time=FILLED_AT, acctNumber=ACCOUNT, exchange="ARCA",
        side="BOT", shares=shares, price=113.35, permId=PERM_ID, clientId=1,
        orderId=219, cumQty=cum_qty, avgPrice=113.35,
    )


def _report(exec_id):
    from ib_insync import CommissionReport

    return CommissionReport(execId=exec_id, commission=1.0, currency="USD")


def _live(wrapper, exec_id):
    """IB's live delivery: execDetails on reqId -1, then commissionReport."""
    wrapper.execDetails(-1, _contract(), _execution(exec_id))
    wrapper.commissionReport(_report(exec_id))


def _startup_sync(*exec_ids):
    """connectAsync's own reqExecutions: every execution IB serves, not live."""

    def sync(wrapper):
        wrapper.startReq("startup-executions")
        for exec_id in exec_ids:
            wrapper.execDetails("startup-executions", _contract(), _execution(exec_id))
        wrapper.execDetailsEnd("startup-executions")

    return sync


async def _connected_executor(*, during_connect=None, handler=None):
    """An executor connected through ``connect()`` on a real ib_insync IB.

    ``during_connect`` runs inside the faked ``connectAsync``, after the
    order is known to the wrapper (connect's open-order sync) — that is where
    connect's execution sync and window (b) happen. The trade is registered
    after connect returns, as ``restore_order_by_ref`` does.
    """
    from ib_insync import IB, Order, OrderStatus, Trade

    ib = IB()
    trade = Trade(
        contract=_contract(),
        order=Order(orderId=219, permId=PERM_ID, clientId=1, action="BUY",
                    totalQuantity=26),
        orderStatus=OrderStatus(status="Submitted"),
    )

    async def connect_async(*args, **kwargs):
        ib.wrapper.permId2Trade[PERM_ID] = trade
        if during_connect is not None:
            during_connect(ib.wrapper)

    ib.connectAsync = connect_async
    ib.managedAccounts = lambda: [ACCOUNT]
    ib.isConnected = lambda: True
    executor = IBExecutor("h", 7497, 1)
    with patch("ib_insync.IB", return_value=ib):
        await executor.connect(expect_paper=True)

    delivered: list[dict] = []

    async def record(payload):
        delivered.append(payload)

    executor.set_fill_handler(handler or record)
    executor._register_trade("219", trade, ticker="XLC", side="buy")
    return executor, ib, delivered


def _sweep_replies(ib, replay):
    """Answer the sweep's reqExecutions by running ``replay`` on the wrapper."""

    async def req_executions(_filter):
        future = ib.wrapper.startReq(SWEEP_REQ_ID)
        replay(ib.wrapper)
        return await future

    ib.reqExecutionsAsync = req_executions


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------
# Window (a): a live execution answered first by the sweep's reply
# --------------------------------------------------------------------------


class TestALiveFillRacingTheSweepIsNotStamped:
    async def test_the_reply_seeing_it_first_does_not_make_it_recovered(self):
        executor, ib, delivered = await _connected_executor()

        def replay(wrapper):
            wrapper.execDetails(SWEEP_REQ_ID, _contract(), _execution("x.new"))
            wrapper.commissionReport(_report("x.new"))
            # The live copy arrives second: ib_insync already knows the
            # execId, so it fires no fillEvent for it.
            wrapper.execDetails(-1, _contract(), _execution("x.new"))
            wrapper.execDetailsEnd(SWEEP_REQ_ID)

        _sweep_replies(ib, replay)
        await executor.recent_executions()
        await _settle()

        [payload] = delivered
        assert payload["execution_id"] == "x.new"
        assert "recovery_source" not in payload

    async def test_a_commission_report_after_the_reply_ends_is_not_stamped(self):
        executor, ib, delivered = await _connected_executor()

        def replay(wrapper):
            wrapper.execDetails(SWEEP_REQ_ID, _contract(), _execution("x.new"))
            wrapper.execDetailsEnd(SWEEP_REQ_ID)

        _sweep_replies(ib, replay)
        await executor.recent_executions()
        _live(ib.wrapper, "x.new")
        await _settle()

        [payload] = delivered
        assert "recovery_source" not in payload

    async def test_an_execution_ib_already_had_before_the_sweep_is_still_recovered(
        self,
    ):
        """Missed while the session was down: connect's sync stored it, the
        live path never saw it. That is a genuine recovery."""
        executor, ib, delivered = await _connected_executor(
            during_connect=_startup_sync("x.missed")
        )

        def replay(wrapper):
            wrapper.execDetails(SWEEP_REQ_ID, _contract(), _execution("x.missed"))
            wrapper.commissionReport(_report("x.missed"))
            wrapper.execDetailsEnd(SWEEP_REQ_ID)

        _sweep_replies(ib, replay)
        await executor.recent_executions()
        await _settle()

        [payload] = delivered
        assert payload["recovery_source"] == RECOVERY_SOURCE_SWEEP

    async def test_after_a_connectivity_loss_a_first_sight_is_stamped(self):
        """After a 1100 IB may have executed while the Gateway was cut off, so
        an execution first seen in the reply could have been missed: stamp it
        (a false "probably" page is the safe side, a silent miss is not)."""
        executor, ib, delivered = await _connected_executor()
        executor._on_ib_error(-1, 1100, "connectivity lost", None)

        def replay(wrapper):
            wrapper.execDetails(SWEEP_REQ_ID, _contract(), _execution("x.new"))
            wrapper.commissionReport(_report("x.new"))
            wrapper.execDetailsEnd(SWEEP_REQ_ID)

        _sweep_replies(ib, replay)
        await executor.recent_executions()
        await _settle()

        [payload] = delivered
        assert payload["recovery_source"] == RECOVERY_SOURCE_SWEEP

    async def test_a_full_read_after_the_loss_closes_the_window_again(self):
        executor, ib, delivered = await _connected_executor()
        executor._on_ib_error(-1, 1100, "connectivity lost", None)
        executor._on_ib_error(-1, 1102, "restored, data maintained", None)
        _sweep_replies(ib, lambda wrapper: wrapper.execDetailsEnd(SWEEP_REQ_ID))
        await executor.recent_executions()  # IB's record, read after the loss

        def replay(wrapper):
            wrapper.execDetails(SWEEP_REQ_ID, _contract(), _execution("x.new"))
            wrapper.commissionReport(_report("x.new"))
            wrapper.execDetailsEnd(SWEEP_REQ_ID)

        _sweep_replies(ib, replay)
        await executor.recent_executions()
        await _settle()

        [payload] = delivered
        assert "recovery_source" not in payload

    async def test_a_reconnect_mid_request_does_not_borrow_the_old_window(self):
        """The window belongs to the IB client the request went out on; a
        fresh client's replay after a reconnect is judged on its own."""
        executor, ib, delivered = await _connected_executor()
        executor._sweep_window = (object(), executor._connectivity_epoch, frozenset())
        ib.wrapper.startReq("other")  # a reply, not a live delivery
        ib.wrapper.execDetails("other", _contract(), _execution("x.missed"))
        ib.wrapper.commissionReport(_report("x.missed"))
        await _settle()

        [payload] = delivered
        assert payload["recovery_source"] == RECOVERY_SOURCE_SWEEP

    async def test_the_sweep_returns_only_after_live_deliveries_from_its_reply(self):
        """The commission report can follow execDetailsEnd in the same read.
        Returning first let the sweep's own path book the live fill — stamped,
        with the live delivery then logged as a duplicate."""
        gate = asyncio.Event()
        delivered = []

        async def slow(payload):
            await gate.wait()
            delivered.append(payload)

        executor, ib, _ = await _connected_executor(handler=slow)

        def replay(wrapper):
            wrapper.execDetails(SWEEP_REQ_ID, _contract(), _execution("x.new"))
            wrapper.execDetailsEnd(SWEEP_REQ_ID)
            wrapper.commissionReport(_report("x.new"))
            asyncio.get_running_loop().call_later(0.01, gate.set)

        _sweep_replies(ib, replay)
        [swept] = await executor.recent_executions()

        assert swept.execution_id == "x.new"
        assert [p["execution_id"] for p in delivered] == ["x.new"]

    async def test_a_hung_delivery_never_holds_the_sweep(self):
        async def hung(payload):
            await asyncio.sleep(3600)

        executor, ib, _ = await _connected_executor(handler=hung)

        def replay(wrapper):
            wrapper.execDetails(SWEEP_REQ_ID, _contract(), _execution("x.new"))
            wrapper.commissionReport(_report("x.new"))
            wrapper.execDetailsEnd(SWEEP_REQ_ID)

        _sweep_replies(ib, replay)
        with patch(
            "services.execution.ib_executor.DELIVERY_DRAIN_TIMEOUT_SECONDS", 0.01
        ):
            [swept] = await asyncio.wait_for(executor.recent_executions(), 1)

        assert swept.execution_id == "x.new"
        for task in list(executor._delivery_tasks):
            task.cancel()


# --------------------------------------------------------------------------
# Window (b): a live execDetails inside connectAsync
# --------------------------------------------------------------------------


class TestALiveFillDuringConnectIsNotStamped:
    async def test_a_live_execution_before_the_callbacks_are_rebound(self):
        executor, ib, delivered = await _connected_executor(
            during_connect=lambda wrapper: wrapper.execDetails(
                -1, _contract(), _execution("x.during-connect")
            )
        )

        ib.wrapper.commissionReport(_report("x.during-connect"))
        await _settle()

        [payload] = delivered
        assert "recovery_source" not in payload

    async def test_a_live_fill_after_connect_still_registers_through_the_trade(self):
        executor, ib, delivered = await _connected_executor()

        _live(ib.wrapper, "x.live")
        await _settle()

        [payload] = delivered
        assert "recovery_source" not in payload


# --------------------------------------------------------------------------
# Window (c): a restored, partly filled order's earlier executions
# --------------------------------------------------------------------------


class TestBookedExecutionsOfARestoredOrderAreNotReplayed:
    async def test_a_booked_execution_is_not_handed_over_again(self):
        executor, ib, delivered = await _connected_executor(
            during_connect=_startup_sync("x.booked", "x.missed")
        )
        executor.mark_executions_booked({"x.booked"})

        def replay(wrapper):
            for exec_id in ("x.booked", "x.missed"):
                wrapper.execDetails(SWEEP_REQ_ID, _contract(), _execution(exec_id))
                wrapper.commissionReport(_report(exec_id))
            wrapper.execDetailsEnd(SWEEP_REQ_ID)

        _sweep_replies(ib, replay)
        await executor.recent_executions()

        # Only the genuinely missed one reaches the handler — still stamped.
        assert [(p["execution_id"], p["recovery_source"]) for p in delivered] == [
            ("x.missed", RECOVERY_SOURCE_SWEEP)
        ]

    def test_the_order_manager_forwards_the_seed(self):
        executor = MagicMock()
        manager = OrderManager(
            executor=executor, redis_client=AsyncMock(), db_session=MagicMock()
        )

        manager.mark_executions_booked({"x.booked"})

        executor.mark_executions_booked.assert_called_once_with({"x.booked"})


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _runner(session=None, *, order_manager=None, executions=None):
    config = MagicMock(spec=AppConfig)
    config.execution = ExecutionConfig()
    config.ib = IBConfig()
    config.mode = "paper"
    config.risk = MagicMock()
    if order_manager is None:
        order_manager = AsyncMock()
        order_manager.open_orders = {}
        order_manager.recent_broker_executions = AsyncMock(
            return_value=list(executions or [])
        )
        order_manager.broker_connection_generation = AsyncMock(return_value=1)
    redis = AsyncMock()
    redis.drain_pending = AsyncMock(return_value=[])
    runner = ExecutionServiceRunner(
        config=config,
        redis_client=redis,
        order_manager=order_manager,
        order_ledger=OrderLedger(session) if session is not None else None,
    )
    return runner, redis


class _RestartedManager:
    """What setup() needs of the order manager across a restart."""

    def __init__(self) -> None:
        self.open_orders: dict = {}
        self.events: list[str] = []
        self.seeded: set[str] | None = None

    async def restore_broker_tracking(self) -> None:
        self.events.append("restore")

    def mark_executions_booked(self, execution_ids) -> None:
        self.seeded = set(execution_ids)
        self.events.append("seed")


class TestARestartSeedsWhatIsAlreadyBooked:
    async def _book_partial_fill(self, session):
        """Execution books 13 of XLC order 219 before the restart."""
        _seed_xlc_submitted(session)
        runner, redis = _runner(session, executions=[_swept(
            execution_id="x.booked", quantity=13.0, cumulative_quantity=13.0,
        )])
        await runner.maybe_run_execution_sweep(0.0)
        [payload] = [c.args[1] for c in redis.publish.await_args_list
                     if c.args[0] == "stream:fills"]
        assert FillProjector(session).apply(FillMessage.from_stream_dict(payload))

    async def test_the_ledger_lists_an_orders_booked_executions(self, session):
        await self._book_partial_fill(session)

        ledger = OrderLedger(session)
        assert ledger.booked_execution_ids(["219"]) == {"x.booked"}
        assert ledger.booked_execution_ids(["220"]) == set()
        assert ledger.booked_execution_ids([]) == set()

    async def test_setup_seeds_before_it_rebinds_the_restored_orders(self, session):
        await self._book_partial_fill(session)
        manager = _RestartedManager()
        runner, _ = _runner(session, order_manager=manager)

        await runner.setup()

        assert manager.seeded == {"x.booked"}
        # Before the rebind: commission reports that trail connect's own
        # execution sync fire on the rebound trade too.
        assert manager.events == ["seed", "restore"]
        assert not session.in_transaction()

    async def test_a_failed_seed_never_blocks_startup(self, session):
        await self._book_partial_fill(session)
        manager = _RestartedManager()
        runner, _ = _runner(session, order_manager=manager)
        runner._order_ledger.booked_execution_ids = MagicMock(
            side_effect=RuntimeError("db hiccup")
        )

        await runner.setup()

        assert manager.events == ["restore"]


# --------------------------------------------------------------------------
# Item 3: a replay while the first delivery is in flight
# --------------------------------------------------------------------------


class TestAReplayDuringTheFirstDelivery:
    async def _executor_with_gated_handler(self, *, fail_first):
        gate = asyncio.Event()
        calls = []

        async def handler(payload):
            calls.append(payload)
            await gate.wait()
            if fail_first and len(calls) == 1:
                raise RuntimeError("transient Redis failure")

        executor, ib, _ = await _connected_executor(handler=handler)
        executor._logger = MagicMock()
        return executor, ib, gate, calls

    async def test_is_held_and_dropped_when_the_first_succeeds(self):
        executor, ib, gate, calls = await self._executor_with_gated_handler(
            fail_first=False
        )
        _live(ib.wrapper, "x.live")
        await _settle()
        ib.wrapper.commissionReport(_report("x.live"))  # replay, first in flight
        await _settle()

        gate.set()
        await _settle()

        assert len(calls) == 1

    async def test_becomes_the_retry_when_the_first_fails(self):
        executor, ib, gate, calls = await self._executor_with_gated_handler(
            fail_first=True
        )
        _live(ib.wrapper, "x.live")
        await _settle()
        ib.wrapper.commissionReport(_report("x.live"))
        await _settle()
        assert len(calls) == 1

        gate.set()
        await _settle()

        assert len(calls) == 2
        assert calls[1]["execution_id"] == "x.live"
        assert "recovery_source" not in calls[1]


# --------------------------------------------------------------------------
# Item 1: one page per sweep pass / reconnect window
# --------------------------------------------------------------------------


def _fill_info(exec_id, *, ticker="AMAT", recovered=True):
    info = {
        "execution_id": exec_id, "account_id": ACCOUNT,
        "timestamp": FILLED_AT, "order_id": "240", "con_id": 270639,
        "ticker": ticker, "exchange": "NASDAQ", "currency": "USD",
        "side": "buy", "quantity": 5.0, "cumulative_quantity": 5.0,
        "fill_price": 539.10, "commission": 1.0, "commission_currency": "USD",
        "commission_trading": 1.0, "commission_fx_base_per_trading": None,
        "order_done": False,
    }
    if recovered:
        info["recovery_source"] = RECOVERY_SOURCE_SWEEP
    return info


def _alerts(redis):
    return [
        AlertMessage.from_stream_dict(c.args[1])
        for c in redis.publish.await_args_list
        if c.args[0] == "stream:alerts"
    ]


def _later():
    return asyncio.get_running_loop().time() + RECOVERY_PAGE_SETTLE_SECONDS + 1


class TestLivePathRecoveriesAreBatched:
    async def test_several_recoveries_page_once_listing_every_fill(self):
        runner, redis = _runner()

        await runner.handle_ib_fill(_fill_info("x.1", ticker="AMAT"))
        await runner.handle_ib_fill(_fill_info("x.2", ticker="NVDA"))
        assert _alerts(redis) == []

        assert await runner.maybe_page_recovered_fills(_later()) is True

        [alert] = _alerts(redis)
        assert alert.event_type == "execution_sweep_recovered"
        assert alert.priority == "medium"
        assert "AMAT" in alert.message and "NVDA" in alert.message
        assert "probably" in alert.message.lower()
        assert alert.context["recovered"] == "2"
        assert alert.context["execution_ids"] == "x.1,x.2"

    async def test_each_fill_is_still_published_at_once(self):
        runner, redis = _runner()

        await runner.handle_ib_fill(_fill_info("x.1"))

        [payload] = [c.args[1] for c in redis.publish.await_args_list
                     if c.args[0] == "stream:fills"]
        assert FillMessage.from_stream_dict(payload).recovery_source == (
            RECOVERY_SOURCE_SWEEP
        )

    async def test_the_page_waits_for_the_burst_to_settle(self):
        runner, redis = _runner()
        await runner.handle_ib_fill(_fill_info("x.1"))

        now = asyncio.get_running_loop().time()
        assert await runner.maybe_page_recovered_fills(now) is False
        assert _alerts(redis) == []

    async def test_a_second_burst_pages_again(self):
        runner, redis = _runner()
        await runner.handle_ib_fill(_fill_info("x.1"))
        await runner.maybe_page_recovered_fills(_later())

        await runner.handle_ib_fill(_fill_info("x.2"))
        await runner.maybe_page_recovered_fills(_later())

        assert [a.context["execution_ids"] for a in _alerts(redis)] == ["x.1", "x.2"]

    async def test_a_live_fill_is_neither_paged_nor_held(self):
        runner, redis = _runner()

        await runner.handle_ib_fill(_fill_info("x.1", recovered=False))

        assert await runner.maybe_page_recovered_fills(_later()) is False
        assert _alerts(redis) == []

    async def test_a_running_sweep_pages_them_instead(self):
        runner, redis = _runner()
        await runner.handle_ib_fill(_fill_info("x.1"))
        runner._execution_sweep_task = asyncio.create_task(asyncio.sleep(3600))
        try:
            assert await runner.maybe_page_recovered_fills(_later()) is False
        finally:
            runner._execution_sweep_task.cancel()
        assert _alerts(redis) == []

    async def test_shutdown_pages_what_is_still_held(self):
        runner, redis = _runner()
        await runner.handle_ib_fill(_fill_info("x.1"))

        await runner.shutdown()

        assert [a.event_type for a in _alerts(redis)] == ["execution_sweep_recovered"]

    async def test_a_failed_page_keeps_nothing_held_and_never_raises(self):
        runner, redis = _runner()

        async def publish(stream, payload):
            if stream == "stream:alerts":
                raise ConnectionError("redis down")
            return "id"

        redis.publish = AsyncMock(side_effect=publish)
        await runner.handle_ib_fill(_fill_info("x.1"))

        assert await runner.maybe_page_recovered_fills(_later()) is True
        assert await runner.maybe_page_recovered_fills(_later()) is False

    async def test_the_loops_own_sweep_task_never_starves_the_page(self):
        """The loop starts a sweep task every iteration; checking for a
        running pass after starting one would never page."""
        runner, redis = _runner()
        runner.setup = AsyncMock()
        runner.shutdown = AsyncMock()
        await runner.handle_ib_fill(_fill_info("x.1"))
        runner._last_recovery_queued_at -= RECOVERY_PAGE_SETTLE_SECONDS + 1
        kill_reads = 0

        async def read_group(stream, *args, **kwargs):
            nonlocal kill_reads
            await asyncio.sleep(0)
            if stream == "stream:kill":
                kill_reads += 1
                if kill_reads == 3:
                    runner._running = False
            return []

        redis.read_group = read_group
        with patch("services.execution.runner.write_heartbeat"):
            await asyncio.wait_for(runner.run(), timeout=5)

        assert [a.event_type for a in _alerts(redis)] == ["execution_sweep_recovered"]

    async def test_the_main_loop_flushes_them(self):
        runner, redis = _runner()
        runner.setup = AsyncMock()
        runner.shutdown = AsyncMock()
        runner.maybe_run_execution_sweep = AsyncMock(return_value=False)
        calls = []

        async def flush(now):
            calls.append(now)
            runner._running = False
            return False

        async def read_group(*args, **kwargs):
            await asyncio.sleep(0)
            return []

        redis.read_group = read_group
        runner.maybe_page_recovered_fills = flush
        with patch("services.execution.runner.write_heartbeat"):
            await asyncio.wait_for(runner.run(), timeout=5)

        assert len(calls) == 1


class TestTheSweepPassPagesOnceForBothPaths:
    async def test_live_path_and_sweep_recoveries_share_one_page(self, session):
        _seed_xlc_submitted(session)
        runner, redis = _runner(session, executions=[_swept()])
        # Booked by the live path during this pass's reqExecutions reply.
        await runner.handle_ib_fill(_fill_info("x.live-path", ticker="AMAT"))

        await runner.maybe_run_execution_sweep(0.0)

        [alert] = _alerts(redis)
        assert alert.event_type == "execution_sweep_recovered"
        assert alert.priority == "medium"
        assert XLC_REC in alert.message and "AMAT" in alert.message
        assert "probably" in alert.message.lower()
        assert alert.context["recovered"] == "2"
        # Nothing left for the loop to page a second time.
        assert await runner.maybe_page_recovered_fills(_later()) is False

    async def test_a_failed_pass_leaves_them_for_the_loop(self, session):
        runner, redis = _runner(session)
        runner._order_manager.recent_broker_executions = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )
        await runner.handle_ib_fill(_fill_info("x.1"))

        await runner.maybe_run_execution_sweep(0.0)
        assert _alerts(redis) == []

        assert await runner.maybe_page_recovered_fills(_later()) is True
        assert [a.event_type for a in _alerts(redis)] == ["execution_sweep_recovered"]
