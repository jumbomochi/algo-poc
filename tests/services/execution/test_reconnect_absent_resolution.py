"""KAN-106: resolve orders IB no longer knows after a reconnect, not only a restart.

2026-10-06: the Gateway was down 23:55 → 06:53 SGT, and ARKW buy order 241
(thematic_momentum, a DAY order) expired unfilled at Monday's close while it
was. Execution reconnected by itself at 06:54:33 and only *warned*:

    Tracked orders absent after reconnect — verify via broker reconciliation
    order_ids=["241"]

so intent 241 stayed SUBMITTED, the 07:03 paper run's reconciliation read
``major`` (order_missing_at_ib 241) and every entry was skipped for the day.
Only ``docker restart`` healed it: the startup restore found 241 in neither
open trades nor completed-order history and expired it (KAN-96).

These tests drive the real executor, order manager and runner through a
disconnect, a reconnect and the loop's background sweep task, against a real
(sqlite) ledger.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scripts.reconcile_paper import reconcile_snapshot
from services.execution.ib_executor import IBExecutor
from services.execution.order_manager import OrderManager
from services.execution.runner import ExecutionServiceRunner
from services.portfolio_accounting.projector import FillProjector
from shared.config import AppConfig, ExecutionConfig, IBConfig
from shared.models import Base, OrderStatus, PortfolioConfig
from shared.order_ledger import ABSENT_AT_IB_REASON, OrderLedger
from shared.schemas.messages import FillMessage

ACCOUNT = "DUN551088"
ARKW_REC = "sleeve-2026-10-05-DUN551088-paper-thematic_momentum-ARKW-buy"
ARKW_CON_ID = 270633028
ARKW_ORDER = "241"
SEEDED_AT = datetime(2026, 10, 5, 8, 21, tzinfo=timezone.utc)
FILLED_AT = datetime(2026, 10, 5, 13, 31, 4, tzinfo=timezone.utc)
SWEEP_INTERVAL_S = 15 * 60


class Event:
    """ib_insync's Event, to the extent the executor binds to it."""

    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def emit(self, *args) -> None:
        for handler in list(self.handlers):
            handler(*args)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_submitted(
    ledger: OrderLedger,
    *,
    rec: str = ARKW_REC,
    ib_order_id: str = ARKW_ORDER,
    symbol: str = "ARKW",
    con_id: int = ARKW_CON_ID,
    quantity: float = 5.0,
) -> None:
    ledger.create_intent(SimpleNamespace(
        recommendation_id=rec, account_id=ACCOUNT, mode="paper",
        portfolio="thematic_momentum", con_id=con_id, symbol=symbol,
        exchange="SMART", currency="USD", action="BUY", quantity=quantity,
        limit_price=180.0, order_type="LMT",
    ))
    ledger.transition(rec, OrderStatus.APPROVED)
    ledger.record_submission(rec, ib_order_id)
    ledger.session.commit()


def _trade(order_id: str, *, rec: str = ARKW_REC, symbol: str = "ARKW"):
    return SimpleNamespace(
        order=SimpleNamespace(orderId=int(order_id), orderRef=rec, action="BUY"),
        contract=SimpleNamespace(symbol=symbol),
        orderStatus=SimpleNamespace(status="Submitted", filled=0.0, whyHeld=""),
        fillEvent=Event(),
        commissionReportEvent=Event(),
        statusEvent=Event(),
        log=[],
        isDone=lambda: True,
    )


def _completed(order_id: str, status: str, *, rec: str = ARKW_REC):
    return SimpleNamespace(
        order=SimpleNamespace(orderId=int(order_id), orderRef=rec, action="BUY"),
        contract=SimpleNamespace(symbol="ARKW"),
        orderStatus=SimpleNamespace(status=status, filled=0.0, whyHeld=""),
        log=[],
    )


def _ib_fill(order_id: str = ARKW_ORDER, exec_id: str = "0001f4e8.arkw.01"):
    return SimpleNamespace(
        execution=SimpleNamespace(
            execId=exec_id, acctNumber=ACCOUNT, orderId=int(order_id),
            side="BOT", shares=5.0, cumQty=5.0, price=179.40, time=FILLED_AT,
        ),
        contract=SimpleNamespace(
            conId=ARKW_CON_ID, symbol="ARKW", exchange="ARCA", currency="USD"
        ),
        commissionReport=SimpleNamespace(
            execId=exec_id, commission=1.0, currency="USD"
        ),
    )


def _fake_ib(*, open_trades=(), completed=(), executions=(), positions=()):
    ib = MagicMock()
    ib.isConnected.return_value = True
    ib.connectAsync = AsyncMock()
    ib.managedAccounts.return_value = [ACCOUNT]
    ib.openTrades.return_value = list(open_trades)
    ib.reqOpenOrdersAsync = AsyncMock(return_value=list(open_trades))
    ib.reqCompletedOrdersAsync = AsyncMock(return_value=list(completed))
    ib.reqExecutionsAsync = AsyncMock(return_value=list(executions))
    ib.reqPositionsAsync = AsyncMock(return_value=list(positions))
    ib.accountValues.return_value = []
    ib.wrapper.fills = {}
    return ib


class Harness:
    """The production wiring of ``runner.main()``, minus Redis and IB."""

    def __init__(self, session: Session, **execution_overrides) -> None:
        self.session = session
        session.add(PortfolioConfig(
            portfolio="thematic_momentum", capital=10_000, cash=10_000,
            created_at=SEEDED_AT, updated_at=SEEDED_AT,
        ))
        session.commit()
        self.ledger = OrderLedger(session)
        self.executor = IBExecutor("h", 7497, 1)
        self.executor._logger = MagicMock()
        self.redis = AsyncMock()
        self.order_manager = OrderManager(
            executor=self.executor, redis_client=self.redis,
            db_session=MagicMock(),
        )
        self.order_manager._logger = MagicMock()
        config = MagicMock(spec=AppConfig)
        config.execution = ExecutionConfig(**execution_overrides)
        config.ib = IBConfig()
        config.mode = "paper"
        config.risk = MagicMock()
        self.runner = ExecutionServiceRunner(
            config=config, redis_client=self.redis,
            order_manager=self.order_manager, order_ledger=self.ledger,
        )
        self.executor.set_fill_handler(self.runner.handle_ib_fill)
        self.executor.set_order_status_handler(self.runner.handle_ib_order_status)
        self.now = 0.0

    def track(self, rec: str = ARKW_REC, order_id: str = ARKW_ORDER,
              symbol: str = "ARKW") -> SimpleNamespace:
        """Order placed by this process before the outage, on the old client."""
        trade = _trade(order_id, rec=rec, symbol=symbol)
        self.executor._register_trade(order_id, trade, symbol, "buy")
        self.order_manager.restore_submission(
            rec, order_id, ticker=symbol, quantity=5.0, limit_price=180.0
        )
        self.runner.restore_pending_orders()
        return trade

    async def run_before_outage(self) -> MagicMock:
        """The loop as it ran on 10-05: connected, one sweep pass done."""
        old_ib = _fake_ib(open_trades=list(self.executor._trades.values()))
        self.executor._ib = old_ib
        self.executor._connection_generation = 1
        await self.loop_pass()
        return old_ib

    async def loop_pass(self, *, advance: float = 1.0) -> None:
        """One main-loop iteration's IB steps, awaiting the background task."""
        self.now += advance
        await self.runner.maybe_check_ib_connection(self.now)
        self.runner._start_execution_sweep(self.now)
        task = self.runner._execution_sweep_task
        if task is not None:
            await task

    async def reconnect_to(self, new_ib: MagicMock) -> None:
        """Gateway back: the liveness check reconnects, then the loop sweeps."""
        self.executor._ib.isConnected.return_value = False
        with patch("ib_insync.IB", return_value=new_ib):
            await self.loop_pass(
                advance=self.runner._ib_liveness_interval_seconds + 1
            )

    def published_fills(self) -> list[FillMessage]:
        return [
            FillMessage.from_stream_dict(c.args[1])
            for c in self.redis.publish.await_args_list
            if c.args[0] == "stream:fills"
        ]

    def intent(self, rec: str = ARKW_REC):
        intent = self.ledger.get(rec)
        self.session.rollback()
        return intent


def _empty_broker():
    return SimpleNamespace(
        account_id=ACCOUNT, mode="paper", positions={}, open_orders={}
    )


# --------------------------------------------------------------------------
# AC1 — absent from open trades AND completed history → EXPIRED, no restart
# --------------------------------------------------------------------------


class TestAnOrderIBNoLongerKnowsExpiresAfterAReconnect:
    async def test_absent_everywhere_expires_with_the_absent_reason(self, session):
        h = Harness(session)
        _seed_submitted(h.ledger)
        h.track()
        await h.run_before_outage()

        # 241 expired at the close while the Gateway was down: the new
        # session's open trades and completed-order history both lack it.
        new_ib = _fake_ib()
        await h.reconnect_to(new_ib)

        intent = h.intent()
        assert intent.status == OrderStatus.EXPIRED.value
        assert intent.reason == ABSENT_AT_IB_REASON
        new_ib.reqCompletedOrdersAsync.assert_awaited()

    async def test_the_outcome_is_logged_per_order(self, session):
        h = Harness(session)
        _seed_submitted(h.ledger)
        h.track()
        await h.run_before_outage()

        await h.reconnect_to(_fake_ib())

        logged = [
            c for c in h.executor._logger.warning.call_args_list
            + h.executor._logger.info.call_args_list
            if c.kwargs.get("outcome") == "expired_absent"
        ]
        assert [c.kwargs.get("order_id") for c in logged] == [ARKW_ORDER]

    async def test_the_reconnect_warning_is_kept(self, session):
        h = Harness(session)
        _seed_submitted(h.ledger)
        h.track()
        await h.run_before_outage()

        await h.reconnect_to(_fake_ib())

        warned = [
            c.kwargs.get("order_ids")
            for c in h.executor._logger.warning.call_args_list
            if "order_ids" in c.kwargs
        ]
        assert [ARKW_ORDER] in warned

    async def test_an_already_terminal_intent_is_not_queried(self, session):
        """Only intents the book still holds working are resolved: an order
        this process filled yesterday costs no completed-order request."""
        h = Harness(session)
        _seed_submitted(h.ledger)
        h.track()
        h.ledger.transition(ARKW_REC, OrderStatus.CANCELLED, reason="test")
        session.commit()
        await h.run_before_outage()

        new_ib = _fake_ib()
        await h.reconnect_to(new_ib)

        new_ib.reqCompletedOrdersAsync.assert_not_awaited()
        assert h.intent().status == OrderStatus.CANCELLED.value

    async def test_no_reconnect_means_no_resolution(self, session):
        """An ordinary interval pass is not a reconnect: an open order the
        loop is merely sweeping past is never put to IB's history."""
        h = Harness(session, execution_sweep_interval_minutes=1)
        _seed_submitted(h.ledger)
        h.track()
        old_ib = await h.run_before_outage()

        await h.loop_pass(advance=120.0)

        old_ib.reqCompletedOrdersAsync.assert_not_awaited()
        assert h.intent().status == OrderStatus.SUBMITTED.value


# --------------------------------------------------------------------------
# AC2 — absent from open trades but in completed history → its true status
# --------------------------------------------------------------------------


class TestCompletedHistoryGivesTheTrueStatus:
    async def test_a_cancelled_order_ends_cancelled_not_expired(self, session):
        h = Harness(session)
        _seed_submitted(h.ledger)
        h.track()
        await h.run_before_outage()

        await h.reconnect_to(
            _fake_ib(completed=[_completed(ARKW_ORDER, "Cancelled")])
        )

        intent = h.intent()
        assert intent.status == OrderStatus.CANCELLED.value
        assert intent.reason != ABSENT_AT_IB_REASON

    async def test_a_completed_expiry_ends_expired_on_ib_word(self, session):
        h = Harness(session)
        _seed_submitted(h.ledger)
        h.track()
        await h.run_before_outage()

        await h.reconnect_to(
            _fake_ib(completed=[_completed(ARKW_ORDER, "Expired")])
        )

        intent = h.intent()
        assert intent.status == OrderStatus.EXPIRED.value
        assert intent.reason != ABSENT_AT_IB_REASON


# --------------------------------------------------------------------------
# AC3 — still open after the reconnect → untouched, callbacks bound once
# --------------------------------------------------------------------------


class TestAStillOpenOrderIsUntouched:
    async def test_open_order_stays_submitted_with_one_binding(self, session):
        h = Harness(session)
        _seed_submitted(h.ledger)
        h.track()
        await h.run_before_outage()

        fresh = _trade(ARKW_ORDER)
        new_ib = _fake_ib(open_trades=[fresh])
        await h.reconnect_to(new_ib)

        assert h.intent().status == OrderStatus.SUBMITTED.value
        new_ib.reqCompletedOrdersAsync.assert_not_awaited()
        assert h.executor._trades[ARKW_ORDER] is fresh
        assert len(fresh.commissionReportEvent.handlers) == 1
        assert len(fresh.statusEvent.handlers) == 1
        assert len(fresh.fillEvent.handlers) == 1

    async def test_a_fill_on_the_rebound_trade_is_delivered_once(self, session):
        h = Harness(session)
        _seed_submitted(h.ledger)
        h.track()
        await h.run_before_outage()
        fresh = _trade(ARKW_ORDER)
        await h.reconnect_to(_fake_ib(open_trades=[fresh]))
        fill = _ib_fill()

        fresh.fillEvent.emit(fresh, fill)
        fresh.commissionReportEvent.emit(fresh, fill, fill.commissionReport)
        for _ in range(5):
            await asyncio.sleep(0)

        assert len(h.published_fills()) == 1


# --------------------------------------------------------------------------
# AC4 — the post-reconnect sweep runs first; a served fill is never expired
# --------------------------------------------------------------------------


class TestTheSweepRunsBeforeTheResolution:
    async def test_executions_are_read_before_completed_history(self, session):
        h = Harness(session)
        _seed_submitted(h.ledger)
        h.track()
        await h.run_before_outage()
        calls: list[str] = []
        new_ib = _fake_ib()

        async def executions(*args, **kwargs):
            calls.append("reqExecutions")
            return []

        async def completed(*args, **kwargs):
            calls.append("reqCompletedOrders")
            return []

        new_ib.reqExecutionsAsync = AsyncMock(side_effect=executions)
        new_ib.reqCompletedOrdersAsync = AsyncMock(side_effect=completed)

        await h.reconnect_to(new_ib)

        assert calls == ["reqExecutions", "reqCompletedOrders"]

    async def test_a_fill_ib_still_serves_is_booked_and_ends_filled(self, session):
        """241 filled while the socket was dead and IB's completed history no
        longer lists it, but reqExecutions still serves the fill. The sweep
        books it; the expiry must not land before the projector applies it,
        or the intent ends EXPIRED holding a fill."""
        h = Harness(session)
        _seed_submitted(h.ledger)
        h.track()
        await h.run_before_outage()

        await h.reconnect_to(_fake_ib(executions=[_ib_fill()]))

        [fill] = h.published_fills()
        assert fill.recommendation_id == ARKW_REC
        assert h.intent().status == OrderStatus.SUBMITTED.value
        # portfolio_accounting applies the published fill.
        assert FillProjector(session).apply(fill) is True

        # The next interval's sweep and resolution leave it alone.
        await h.loop_pass(advance=SWEEP_INTERVAL_S + 1)

        intent = h.intent()
        assert intent.status == OrderStatus.FILLED.value
        assert intent.filled_quantity == pytest.approx(5.0)

    async def test_a_failed_sweep_holds_the_resolution(self, session):
        """Resolving without the post-reconnect sweep would risk exactly the
        pre-emption above, so a failed pass defers it to the next good one."""
        h = Harness(session)
        _seed_submitted(h.ledger)
        h.track()
        await h.run_before_outage()
        new_ib = _fake_ib()
        new_ib.reqExecutionsAsync = AsyncMock(side_effect=asyncio.TimeoutError())

        await h.reconnect_to(new_ib)

        new_ib.reqCompletedOrdersAsync.assert_not_awaited()
        assert h.intent().status == OrderStatus.SUBMITTED.value

        new_ib.reqExecutionsAsync = AsyncMock(return_value=[])
        await h.loop_pass(advance=SWEEP_INTERVAL_S + 1)

        assert h.intent().status == OrderStatus.EXPIRED.value


# --------------------------------------------------------------------------
# AC5 — failures are logged and retried; the loop and kill path never stop
# --------------------------------------------------------------------------


class TestFailuresAreRetriedNeverFatal:
    async def test_a_failure_is_retried_at_the_next_interval(self, session):
        h = Harness(session)
        _seed_submitted(h.ledger)
        h.track()
        await h.run_before_outage()
        new_ib = _fake_ib()
        new_ib.reqCompletedOrdersAsync = AsyncMock(
            side_effect=[RuntimeError("IB hiccup"), []]
        )

        await h.reconnect_to(new_ib)

        task = h.runner._execution_sweep_task
        assert task.done() and task.exception() is None
        assert h.intent().status == OrderStatus.SUBMITTED.value
        [failed] = h.order_manager._logger.exception.call_args_list
        assert failed.kwargs["order_id"] == ARKW_ORDER

        # Not before the next successful sweep: no hammering every loop pass.
        await h.loop_pass(advance=1.0)
        assert new_ib.reqCompletedOrdersAsync.await_count == 1

        await h.loop_pass(advance=SWEEP_INTERVAL_S + 1)
        assert new_ib.reqCompletedOrdersAsync.await_count == 2
        assert h.intent().status == OrderStatus.EXPIRED.value

    async def test_a_failure_is_retried_at_the_next_reconnect(self, session):
        h = Harness(session)
        _seed_submitted(h.ledger)
        h.track()
        await h.run_before_outage()
        flaky = _fake_ib()
        flaky.reqCompletedOrdersAsync = AsyncMock(side_effect=RuntimeError("x"))
        await h.reconnect_to(flaky)
        assert h.intent().status == OrderStatus.SUBMITTED.value

        await h.reconnect_to(_fake_ib())

        assert h.intent().status == OrderStatus.EXPIRED.value

    async def test_one_failing_order_does_not_stop_the_others(self, session):
        h = Harness(session)
        _seed_submitted(h.ledger)
        other_rec = ARKW_REC.replace("ARKW", "ARKK")
        _seed_submitted(
            h.ledger, rec=other_rec, ib_order_id="242", symbol="ARKK",
            con_id=270633029,
        )
        h.track()
        h.track(rec=other_rec, order_id="242", symbol="ARKK")
        await h.run_before_outage()
        new_ib = _fake_ib()
        answers = {"first": True}

        async def completed(*args, **kwargs):
            if answers.pop("first", False):
                raise RuntimeError("IB hiccup")
            return []

        new_ib.reqCompletedOrdersAsync = AsyncMock(side_effect=completed)

        await h.reconnect_to(new_ib)

        statuses = sorted(
            h.intent(rec).status for rec in (ARKW_REC, other_rec)
        )
        assert statuses == [OrderStatus.EXPIRED.value, OrderStatus.SUBMITTED.value]

    async def test_a_hung_resolution_never_holds_a_kill(self, session):
        h = Harness(session)
        runner = h.runner
        runner.setup = AsyncMock()
        runner.shutdown = AsyncMock()
        runner.maybe_check_ib_connection = AsyncMock(return_value=False)
        runner.maybe_run_execution_sweep = AsyncMock(return_value=True)
        started = asyncio.Event()

        async def hung_resolution():
            started.set()
            await asyncio.sleep(3600)

        runner.maybe_resolve_absent_orders = hung_resolution
        kill_reads = []

        async def read_group(stream, *args, **kwargs):
            await asyncio.sleep(0)
            if stream == "stream:kill" and started.is_set():
                kill_reads.append(stream)
                runner._running = False
            return []

        h.redis.read_group = read_group
        with patch("services.execution.runner.write_heartbeat"):
            await asyncio.wait_for(runner.run(), timeout=1)

        assert kill_reads == ["stream:kill"]
        runner._execution_sweep_task.cancel()


# --------------------------------------------------------------------------
# AC6 — the 2026-10-06 replay: reconciliation ok, entries allowed
# --------------------------------------------------------------------------


class TestReplayOf20261006:
    async def test_reconnect_alone_unblocks_entries(self, session):
        h = Harness(session)
        _seed_submitted(h.ledger)
        h.track()
        await h.run_before_outage()

        # 07:03 without the fix: 241 SUBMITTED, IB has no open order.
        before, _ = reconcile_snapshot(session, _empty_broker())
        session.commit()
        assert before.severity == "major"
        assert [d["type"] for d in before.discrepancies] == ["order_missing_at_ib"]

        # 06:54:33 — execution reconnects by itself; no restart.
        await h.reconnect_to(_fake_ib())

        after, _ = reconcile_snapshot(session, _empty_broker())
        assert after.severity == "ok"
        assert after.entries_allowed is True


# --------------------------------------------------------------------------
# The order-manager seam: one resolution path, never raising
# --------------------------------------------------------------------------


class TestTheOrderManagerSeam:
    def _manager(self, restore):
        executor = MagicMock()
        executor.restore_order_by_ref = AsyncMock(side_effect=restore)
        manager = OrderManager(
            executor=executor, redis_client=AsyncMock(), db_session=MagicMock()
        )
        manager._logger = MagicMock()
        for rec, order_id in (("a", "1"), ("b", "2"), ("c", "3"), ("d", "4")):
            manager.restore_submission(
                rec, order_id, ticker="X", quantity=1.0, limit_price=1.0
            )
        return manager, executor

    async def test_each_outcome_is_reported_and_unnamed_orders_are_skipped(self):
        answers = {"1": True, "2": False, "3": None}

        async def restore(rec, order_id):
            if order_id == "4":
                raise RuntimeError("IB hiccup")
            return answers[order_id]

        manager, executor = self._manager(restore)

        outcomes = await manager.resolve_tracked_orders({"a", "b", "c", "d"})

        assert outcomes == {
            "1": "open", "2": "resolved", "3": "unresolved", "4": "failed",
        }

        executor.restore_order_by_ref.reset_mock()
        assert await manager.resolve_tracked_orders({"b"}) == {"2": "resolved"}
        executor.restore_order_by_ref.assert_awaited_once_with("b", "2")
