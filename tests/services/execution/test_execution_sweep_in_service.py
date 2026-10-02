"""KAN-95: execution recovers the fills its callback missed, on its own.

The 04:15 sweep in ``run_paper.py`` logged "IB returned 0 execution(s)" on
every night it ran (2026-09-23 → 10-02), including the morning after execution
had recorded seven fills live; it never recovered a fill. So when XLC order 219
filled into a dead socket on 2026-09-25 nothing booked it, and every buy was
blocked for five sessions.

The sweep now runs inside the execution service, on the session that placed
the orders (client 1, so the Master API client ID no longer matters), hourly
and right after any reconnect — always inside IB's execution window.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from services.execution.execution_sweep import RECOVERY_SOURCE_SWEEP, SweptExecution
from services.execution.ib_executor import IBExecutor
from services.execution.order_manager import OrderManager
from services.execution.runner import ExecutionServiceRunner
from services.portfolio_accounting.projector import FillProjector
from shared.config import AppConfig, ExecutionConfig, IBConfig
from shared.models import (
    Base,
    ExecutionFill,
    OrderStatus,
    PortfolioConfig,
    Position,
)
from shared.order_ledger import OrderLedger
from shared.schemas.messages import FillMessage

ACCOUNT = "DUN551088"
XLC_REC = "sleeve-2026-09-25-DUN551088-paper-sector_rotation-XLC-buy"
FILLED_AT = datetime(2026, 9, 25, 13, 30, 26, tzinfo=timezone.utc)


def _ib_fill(*, exec_id="0001f4e8.xlc.01", order_id=219, side="BOT",
             commission_currency="USD"):
    return SimpleNamespace(
        execution=SimpleNamespace(
            execId=exec_id, acctNumber=ACCOUNT, orderId=order_id, side=side,
            shares=26.0, cumQty=26.0, price=113.35, time=FILLED_AT,
        ),
        contract=SimpleNamespace(
            conId=322317077, symbol="XLC", exchange="ARCA", currency="USD"
        ),
        # A report that has arrived carries its execId (ib_insync's
        # CommissionReport); recent_executions skips ones that have not.
        commissionReport=SimpleNamespace(
            execId=exec_id, commission=1.000078, currency=commission_currency
        ),
    )


def _swept(**overrides) -> SweptExecution:
    base = dict(
        execution_id="0001f4e8.xlc.01", account_id=ACCOUNT, ib_order_id="219",
        con_id=322317077, ticker="XLC", exchange="ARCA", currency="USD",
        side="buy", quantity=26.0, cumulative_quantity=26.0, price=113.35,
        commission=1.000078, commission_currency="USD", executed_at=FILLED_AT,
    )
    base.update(overrides)
    return SweptExecution(**base)


# --------------------------------------------------------------------------
# Task 1 — the executor reads its own executions
# --------------------------------------------------------------------------


class TestTheExecutorReadsItsOwnExecutions:
    async def test_ib_fills_are_normalized_into_swept_executions(self):
        executor = IBExecutor("h", 7497, 1)
        executor._ib = MagicMock()
        executor._ib.isConnected.return_value = True
        executor._ib.reqExecutionsAsync = AsyncMock(return_value=[_ib_fill()])
        executor._ib.accountValues.return_value = []
        executor._ib.wrapper.fills = {}

        [swept] = await executor.recent_executions()

        assert swept.execution_id == "0001f4e8.xlc.01"
        assert swept.ib_order_id == "219"
        assert swept.side == "buy"
        assert swept.quantity == 26.0
        assert swept.commission_currency == "USD"
        assert swept.commission_fx_base_per_trading is None

    async def test_a_non_usd_commission_carries_the_exchange_rate(self):
        executor = IBExecutor("h", 7497, 1)
        executor._ib = MagicMock()
        executor._ib.isConnected.return_value = True
        executor._ib.reqExecutionsAsync = AsyncMock(
            return_value=[_ib_fill(commission_currency="SGD")]
        )
        executor._ib.accountValues.return_value = [
            SimpleNamespace(tag="ExchangeRate", currency="USD", value="1.2775")
        ]
        executor._ib.wrapper.fills = {}

        [swept] = await executor.recent_executions()

        assert swept.commission_fx_base_per_trading == pytest.approx(1.2775)

    async def test_a_hung_request_is_bounded(self):
        executor = IBExecutor("h", 7497, 1)
        executor._ib = MagicMock()
        executor._ib.isConnected.return_value = True

        async def hang(*args, **kwargs):
            await asyncio.sleep(3600)

        executor._ib.reqExecutionsAsync = hang
        with patch("services.execution.ib_executor.REQ_EXECUTIONS_TIMEOUT_SECONDS", 0.01):
            with pytest.raises(asyncio.TimeoutError):
                await executor.recent_executions()

    async def test_every_connect_bumps_the_connection_generation(self):
        executor = IBExecutor("h", 7497, 1)
        fake_ib = MagicMock()
        fake_ib.connectAsync = AsyncMock()
        fake_ib.managedAccounts.return_value = [ACCOUNT]
        fake_ib.openTrades.return_value = []

        assert executor.connection_generation == 0
        with patch("ib_insync.IB", return_value=fake_ib):
            await executor.connect(expect_paper=True)
            await executor.connect(expect_paper=True)

        assert executor.connection_generation == 2

    async def test_the_order_manager_forwards_both(self):
        executor = AsyncMock()
        executor.recent_executions = AsyncMock(return_value=[_swept()])
        executor.connection_generation = 3
        manager = OrderManager(
            executor=executor, redis_client=AsyncMock(), db_session=MagicMock()
        )

        assert await manager.recent_broker_executions() == [_swept()]
        assert await manager.broker_connection_generation() == 3


# --------------------------------------------------------------------------
# Task 2 — the runner sweep step
# --------------------------------------------------------------------------


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_xlc_submitted(session: Session) -> OrderLedger:
    session.add(PortfolioConfig(
        portfolio="sector_rotation", capital=10_000, cash=10_000,
        created_at=FILLED_AT, updated_at=FILLED_AT,
    ))
    session.commit()
    ledger = OrderLedger(session)
    ledger.create_intent(SimpleNamespace(
        recommendation_id=XLC_REC, account_id=ACCOUNT, mode="paper",
        portfolio="sector_rotation", con_id=322317077, symbol="XLC",
        exchange="ARCA", currency="USD", action="BUY", quantity=26.9848,
        limit_price=113.99, order_type="LMT",
    ))
    ledger.transition(XLC_REC, OrderStatus.APPROVED)
    ledger.record_submission(XLC_REC, "219")
    session.commit()
    return ledger


def _runner(session, *, executions=None, error=None, generation=1, ledger=True,
            **execution_overrides):
    config = MagicMock(spec=AppConfig)
    config.execution = ExecutionConfig(**execution_overrides)
    config.ib = IBConfig()
    config.mode = "paper"
    config.risk = MagicMock()
    order_manager = AsyncMock()
    order_manager.open_orders = {}
    if error is not None:
        order_manager.recent_broker_executions = AsyncMock(side_effect=error)
    else:
        order_manager.recent_broker_executions = AsyncMock(
            return_value=list(executions or [])
        )
    order_manager.broker_connection_generation = AsyncMock(return_value=generation)
    redis = AsyncMock()
    runner = ExecutionServiceRunner(
        config=config,
        redis_client=redis,
        order_manager=order_manager,
        order_ledger=OrderLedger(session) if ledger else None,
    )
    return runner, order_manager, redis


def _published_fills(redis) -> list[FillMessage]:
    return [
        FillMessage.from_stream_dict(c.args[1])
        for c in redis.publish.await_args_list
        if c.args[0] == "stream:fills"
    ]


class TestTheRunnerRecoversMissedFills:
    async def test_a_missed_fill_is_booked_through_the_projector(self, session):
        _seed_xlc_submitted(session)
        runner, _, redis = _runner(session, executions=[_swept()])

        assert await runner.maybe_run_execution_sweep(0.0) is True

        [fill] = _published_fills(redis)
        assert fill.recommendation_id == XLC_REC
        assert fill.portfolio == "sector_rotation"
        assert fill.recovery_source == RECOVERY_SOURCE_SWEEP
        assert FillProjector(session).apply(fill) is True
        assert OrderLedger(session).get(XLC_REC).status == OrderStatus.FILLED.value
        position = session.scalar(select(Position).where(Position.ticker == "XLC"))
        assert position.portfolio == "sector_rotation"
        assert position.quantity == 26.0
        row = session.scalar(select(ExecutionFill))
        assert row.recovery_source == RECOVERY_SOURCE_SWEEP

    async def test_a_second_pass_books_nothing_new(self, session):
        _seed_xlc_submitted(session)
        runner, _, redis = _runner(
            session, executions=[_swept()], execution_sweep_interval_minutes=1
        )
        await runner.maybe_run_execution_sweep(0.0)
        FillProjector(session).apply(_published_fills(redis)[0])

        await runner.maybe_run_execution_sweep(60.0)

        assert len(_published_fills(redis)) == 1
        assert len(session.scalars(select(ExecutionFill)).all()) == 1

    async def test_an_execution_the_callback_already_handled_is_skipped(self, session):
        """The live callback published it but the projector has not applied it
        yet — the sweep must not publish it a second time."""
        _seed_xlc_submitted(session)
        runner, _, redis = _runner(session, executions=[_swept()])
        runner._handled_executions.add((ACCOUNT, "0001f4e8.xlc.01"))

        await runner.maybe_run_execution_sweep(0.0)

        assert _published_fills(redis) == []

    async def test_a_recovered_fill_is_marked_handled_so_a_late_callback_is_ignored(
        self, session
    ):
        _seed_xlc_submitted(session)
        runner, _, _ = _runner(session, executions=[_swept()])

        await runner.maybe_run_execution_sweep(0.0)

        assert (ACCOUNT, "0001f4e8.xlc.01") in runner._handled_executions

    async def test_an_untracked_execution_is_not_booked(self, session):
        _seed_xlc_submitted(session)
        runner, _, redis = _runner(
            session, executions=[_swept(ib_order_id="999", execution_id="x")]
        )

        await runner.maybe_run_execution_sweep(0.0)

        assert _published_fills(redis) == []

    async def test_an_ib_failure_never_stops_the_loop(self, session):
        _seed_xlc_submitted(session)
        runner, _, redis = _runner(session, error=asyncio.TimeoutError())

        assert await runner.maybe_run_execution_sweep(0.0) is True
        assert _published_fills(redis) == []
        assert not session.in_transaction()


class TestWhenTheSweepRuns:
    async def test_it_waits_out_its_interval(self, session):
        runner, order_manager, _ = _runner(
            session, execution_sweep_interval_minutes=60
        )

        assert await runner.maybe_run_execution_sweep(0.0) is True
        assert await runner.maybe_run_execution_sweep(1800.0) is False
        assert await runner.maybe_run_execution_sweep(3600.0) is True
        assert order_manager.recent_broker_executions.await_count == 2

    async def test_a_reconnect_triggers_a_sweep_before_the_interval(self, session):
        runner, order_manager, _ = _runner(
            session, execution_sweep_interval_minutes=60, generation=1
        )
        await runner.maybe_run_execution_sweep(0.0)

        order_manager.broker_connection_generation = AsyncMock(return_value=2)
        assert await runner.maybe_run_execution_sweep(10.0) is True
        assert order_manager.recent_broker_executions.await_count == 2

        assert await runner.maybe_run_execution_sweep(20.0) is False

    async def test_without_a_ledger_it_never_touches_the_broker(self, session):
        runner, order_manager, _ = _runner(session, ledger=False)

        assert await runner.maybe_run_execution_sweep(0.0) is False
        order_manager.recent_broker_executions.assert_not_awaited()

    def test_a_zero_interval_is_refused(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ExecutionConfig(execution_sweep_interval_minutes=0)

    def test_the_default_interval_keeps_the_pre_midnight_gap_short(self):
        """IB serves executions since midnight Gateway time (Asia/Singapore):
        a missed fill in the last interval before 00:00 SGT is unreadable
        after it, so the interval bounds that loss."""
        assert ExecutionConfig().execution_sweep_interval_minutes == 15

    async def test_the_main_loop_runs_the_sweep(self, session):
        runner, _, redis = _runner(session)
        runner.setup = AsyncMock()
        runner.shutdown = AsyncMock()
        calls = []

        async def sweep(now):
            calls.append(now)
            runner._running = False
            return True

        async def read_group(*args, **kwargs):
            await asyncio.sleep(0)
            return []

        redis.read_group = read_group
        runner.maybe_run_execution_sweep = sweep
        with patch("services.execution.runner.write_heartbeat"):
            await asyncio.wait_for(runner.run(), timeout=5)

        assert len(calls) == 1


# --------------------------------------------------------------------------
# Task 3 — the 04:15 sweep is gone
# --------------------------------------------------------------------------


class TestTheNightlySweepIsRetired:
    def test_run_paper_no_longer_reads_executions(self):
        from pathlib import Path

        import scripts.run_paper as run_paper

        source = Path(run_paper.__file__).read_text()
        assert "reqExecutions" not in source
        assert not hasattr(run_paper, "sweep_executions_best_effort")
        assert not hasattr(run_paper, "alert_sweep_blindness_best_effort")


# --------------------------------------------------------------------------
# Review follow-up — commission integrity, alerts, 1101
# --------------------------------------------------------------------------


def _ib_contract_and_execution(exec_id="0001f4e8.xlc.01"):
    from ib_insync import Contract, Execution

    contract = Contract(conId=322317077, symbol="XLC", exchange="ARCA",
                        currency="USD", secType="STK")
    execution = Execution(
        execId=exec_id, time=FILLED_AT, acctNumber=ACCOUNT, exchange="ARCA",
        side="BOT", shares=26.0, price=113.35, permId=753926985, clientId=1,
        orderId=219, cumQty=26.0, avgPrice=113.35,
    )
    return contract, execution


def _commission_report(exec_id="0001f4e8.xlc.01", commission=1.000078,
                       currency="USD"):
    from ib_insync import CommissionReport

    return CommissionReport(execId=exec_id, commission=commission,
                            currency=currency)


class TestCommissionIntegrity:
    """ib_insync's reqExecutions returns a *fresh* Fill with an empty
    CommissionReport for an execution its wrapper already stored; the report
    only ever updates the stored Fill. Reading the returned one booked every
    recovered fill at commission 0 into the immutable execution_fills row."""

    def _executor_on_a_real_wrapper(self, replay):
        from ib_insync import IB

        executor = IBExecutor("h", 7497, 1)
        executor._ib = IB()
        executor._ensure_connected = AsyncMock()
        wrapper = executor._ib.wrapper

        async def req_executions(_filter):
            future = wrapper.startReq(77)
            replay(wrapper)
            return await future

        executor._ib.reqExecutionsAsync = req_executions
        return executor, wrapper

    async def test_a_known_execution_keeps_its_real_commission(self):
        contract, execution = _ib_contract_and_execution()

        def replay(wrapper):
            wrapper.execDetails(77, contract, execution)
            wrapper.execDetailsEnd(77)

        executor, wrapper = self._executor_on_a_real_wrapper(replay)
        # Seen live earlier (connect's startup sync does the same), with its
        # commission report applied to the stored Fill.
        wrapper.execDetails(-1, contract, execution)
        wrapper.commissionReport(_commission_report())

        [swept] = await executor.recent_executions()

        assert swept.commission == pytest.approx(1.000078)
        assert swept.commission_currency == "USD"

    async def test_a_fill_whose_commission_has_not_arrived_is_left_for_next_pass(self):
        contract, execution = _ib_contract_and_execution()

        def replay(wrapper):
            wrapper.execDetails(77, contract, execution)  # first sight
            wrapper.execDetailsEnd(77)  # ends before its commissionReport

        executor, _ = self._executor_on_a_real_wrapper(replay)

        assert await executor.recent_executions() == []

    async def test_a_data_lost_reconnect_bumps_the_generation(self):
        executor = IBExecutor("h", 7497, 1)
        executor._ib = MagicMock()
        executor._ib.reqOpenOrdersAsync = AsyncMock(return_value=[])
        before = executor.connection_generation

        await executor._resubscribe_after_data_loss(1101)

        assert executor.connection_generation == before + 1


class TestTheSweepPagesWhatItFinds:
    def _alerts(self, redis):
        from shared.schemas.messages import AlertMessage

        return [
            AlertMessage.from_stream_dict(c.args[1])
            for c in redis.publish.await_args_list
            if c.args[0] == "stream:alerts"
        ]

    async def test_a_recovered_fill_is_paged(self, session):
        _seed_xlc_submitted(session)
        runner, _, redis = _runner(session, executions=[_swept()])

        await runner.maybe_run_execution_sweep(0.0)

        [alert] = self._alerts(redis)
        assert alert.event_type == "execution_sweep_recovered"
        assert alert.priority == "medium"
        assert XLC_REC in alert.message

    async def test_an_untracked_execution_pages_once(self, session):
        _seed_xlc_submitted(session)
        runner, _, redis = _runner(
            session, execution_sweep_interval_minutes=1,
            executions=[_swept(ib_order_id="999", execution_id="manual-1")],
        )

        await runner.maybe_run_execution_sweep(0.0)
        await runner.maybe_run_execution_sweep(60.0)

        alerts = self._alerts(redis)
        assert [a.event_type for a in alerts] == ["execution_sweep_untracked"]
        assert alerts[0].priority == "high"
        assert "999" in alerts[0].message

    async def test_three_failed_passes_in_a_row_page_once(self, session):
        runner, _, redis = _runner(
            session, execution_sweep_interval_minutes=1,
            error=asyncio.TimeoutError(),
        )

        for now in (0.0, 60.0):
            await runner.maybe_run_execution_sweep(now)
        assert self._alerts(redis) == []

        for now in (120.0, 180.0):
            await runner.maybe_run_execution_sweep(now)

        alerts = self._alerts(redis)
        assert [a.event_type for a in alerts] == ["execution_sweep_failed"]
        assert alerts[0].priority == "high"

    async def test_a_redis_failure_while_paging_never_escapes(self, session):
        _seed_xlc_submitted(session)
        runner, _, redis = _runner(session, executions=[_swept()])

        async def publish(stream, payload):
            if stream == "stream:alerts":
                raise ConnectionError("redis down")
            return "id"

        redis.publish = AsyncMock(side_effect=publish)

        assert await runner.maybe_run_execution_sweep(0.0) is True
