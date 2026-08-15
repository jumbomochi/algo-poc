from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

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
from shared.schemas.messages import (
    AlertMessage,
    ApprovedOrderMessage,
    FillMessage,
    KillMessage,
)

BROKER_TIME = datetime(2026, 7, 24, 8, 30, tzinfo=timezone.utc)


def make_approved_order(
    ticker: str = "AAPL",
    action: str = "buy",
    quantity: int = 50,
    order_type: str = "limit",
    limit_price: float | None = 150.0,
    recommendation_id: str = "rec-001",
) -> ApprovedOrderMessage:
    return ApprovedOrderMessage(
        ticker=ticker,
        timestamp=datetime.now(timezone.utc),
        action=action,
        quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
        recommendation_id=recommendation_id,
    )


@pytest.fixture()
def mock_config():
    config = MagicMock(spec=AppConfig)
    config.execution = ExecutionConfig()
    config.ib = IBConfig()
    config.mode = "paper"
    config.risk = MagicMock()
    config.risk.min_viable_fill_pct = 40.0
    return config


@pytest.fixture()
def mock_redis():
    redis = AsyncMock()
    redis.publish = AsyncMock(return_value="msg-id-001")
    redis.create_consumer_group = AsyncMock()
    redis.read_group = AsyncMock(return_value=[])
    redis.ack = AsyncMock()
    return redis


@pytest.fixture()
def mock_order_manager():
    mgr = AsyncMock()
    mgr.submit_entry = AsyncMock(return_value="order-001")
    mgr.submit_exit = AsyncMock(return_value="order-002")
    mgr.open_orders = {}
    mgr.cancel_all_orders = AsyncMock()
    mgr.list_open_broker_orders = AsyncMock(return_value=[])
    mgr.cancel_broker_order = AsyncMock(return_value=True)
    return mgr


@pytest.fixture()
def runner(mock_config, mock_redis, mock_order_manager):
    r = ExecutionServiceRunner(
        config=mock_config,
        redis_client=mock_redis,
        order_manager=mock_order_manager,
    )
    return r


@pytest.fixture()
def ledger_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def seed_approved_intent(
    ledger: OrderLedger,
    recommendation_id: str = "rec-1",
    *,
    ib_order_id: str | None = None,
    filled_quantity: float = 0,
    symbol: str = "AAPL",
    con_id: int = 265598,
    portfolio: str = "momentum",
    action: str = "BUY",
):
    proposal = SimpleNamespace(
        recommendation_id=recommendation_id,
        account_id="DUN551088",
        mode="paper",
        portfolio=portfolio,
        con_id=con_id,
        symbol=symbol,
        exchange="SMART",
        currency="USD",
        action=action,
        quantity=50,
        limit_price=150.0,
        order_type="LMT",
    )
    intent = ledger.create_intent(proposal)
    ledger.transition(recommendation_id, OrderStatus.APPROVED)
    if ib_order_id is not None:
        ledger.record_submission(recommendation_id, ib_order_id)
    intent.filled_quantity = filled_quantity
    ledger.session.commit()
    return intent


def make_durable_fill_info(
    *,
    execution_id: str,
    order_id: str,
    ticker: str = "AAPL",
    con_id: int = 265598,
    quantity: float = 5.0,
) -> dict[str, object]:
    return {
        "execution_id": execution_id,
        "account_id": "DUN551088",
        "timestamp": BROKER_TIME,
        "order_id": order_id,
        "con_id": con_id,
        "ticker": ticker,
        "exchange": "SMART",
        "currency": "USD",
        "side": "buy",
        "quantity": quantity,
        "cumulative_quantity": quantity,
        "fill_price": 100.0,
        "commission": 0.0,
        "commission_currency": "USD",
        "commission_trading": 0.0,
        "commission_fx_base_per_trading": None,
        "order_done": False,
    }


def project_durable_fill(
    session: Session,
    fill_info: dict[str, object],
    *,
    recommendation_id: str,
    portfolio: str = "momentum",
) -> None:
    assert FillProjector(session).apply(FillMessage(
        ticker=str(fill_info["ticker"]),
        timestamp=fill_info["timestamp"],
        side=str(fill_info["side"]),
        quantity=float(fill_info["quantity"]),
        cumulative_quantity=float(fill_info["cumulative_quantity"]),
        fill_price=float(fill_info["fill_price"]),
        commission=float(fill_info["commission"]),
        commission_currency=str(fill_info["commission_currency"]),
        commission_trading=float(fill_info["commission_trading"]),
        commission_fx_base_per_trading=None,
        recommendation_id=recommendation_id,
        order_id=str(fill_info["order_id"]),
        execution_id=str(fill_info["execution_id"]),
        account_id=str(fill_info["account_id"]),
        portfolio=portfolio,
        con_id=int(fill_info["con_id"]),
        exchange=str(fill_info["exchange"]),
        currency=str(fill_info["currency"]),
    )) is True


def kill_message() -> KillMessage:
    return KillMessage(
        timestamp=BROKER_TIME,
        triggered_by="test",
        reason="reconcile managed positions",
    )


def seed_portfolio_config(
    session: Session, portfolio: str = "momentum"
) -> None:
    session.add(PortfolioConfig(
        portfolio=portfolio,
        capital=10_000,
        cash=10_000,
        created_at=BROKER_TIME,
        updated_at=BROKER_TIME,
    ))
    session.commit()


@pytest.fixture()
def durable_runner(
    mock_config, mock_redis, mock_order_manager, ledger_session
):
    ledger = OrderLedger(ledger_session)
    runner = ExecutionServiceRunner(
        config=mock_config,
        redis_client=mock_redis,
        order_manager=mock_order_manager,
        order_ledger=ledger,
    )
    return runner, ledger


class TestAbsentOrderRecovery:
    @pytest.mark.asyncio
    async def test_absent_order_expires_partially_filled_intent(
        self, durable_runner, ledger_session
    ):
        """An order gone from IB after a session boundary terminalizes a
        partially-filled intent to EXPIRED while preserving the filled shares."""
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="16", filled_quantity=6)
        ledger.transition("rec-1", OrderStatus.PARTIALLY_FILLED)
        ledger_session.commit()

        await runner.handle_ib_order_status({
            "order_id": "16",
            "status": "Expired",
            "reason": "order absent from IB after session boundary",
            "order_absent_at_ib": True,
        })

        intent = ledger.get("rec-1")
        assert intent.status == OrderStatus.EXPIRED.value
        assert intent.filled_quantity == pytest.approx(6)
        assert intent.terminal_at is not None

    @pytest.mark.asyncio
    async def test_absent_order_expires_unfilled_submitted_intent(
        self, durable_runner, ledger_session
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="17", filled_quantity=0)
        ledger_session.commit()

        await runner.handle_ib_order_status({
            "order_id": "17",
            "status": "Expired",
            "order_absent_at_ib": True,
        })

        assert ledger.get("rec-1").status == OrderStatus.EXPIRED.value


class TestDurableExecutionIdentity:
    @pytest.mark.asyncio
    async def test_submission_persists_order_id_before_return(
        self, durable_runner, ledger_session
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger)
        commits = 0
        original_commit = ledger_session.commit

        def counting_commit():
            nonlocal commits
            commits += 1
            original_commit()

        ledger_session.commit = counting_commit

        await runner.process_approved_order(
            make_approved_order(recommendation_id="rec-1")
        )

        intent = ledger.get("rec-1")
        assert intent.status == OrderStatus.SUBMITTED.value
        assert intent.ib_order_id == "order-001"
        assert commits == 1

    @pytest.mark.asyncio
    async def test_submission_does_not_await_broker_with_db_transaction_open(
        self, durable_runner, ledger_session
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger)

        async def submit_entry(**kwargs):
            assert not ledger_session.in_transaction()
            return "order-001"

        runner._order_manager.submit_entry.side_effect = submit_entry

        await runner.process_approved_order(
            make_approved_order(recommendation_id="rec-1")
        )

    def test_restore_pending_orders_reloads_persisted_attribution(
        self, durable_runner
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")

        runner.restore_pending_orders()

        assert runner._pending_orders["9"].portfolio == "momentum"
        assert runner._pending_orders["9"].recommendation_id == "rec-1"

    @pytest.mark.asyncio
    async def test_skip_persists_submission_failed(self, durable_runner):
        from services.execution.ib_executor import OrderSkippedError

        runner, ledger = durable_runner
        seed_approved_intent(ledger)
        runner._order_manager.submit_entry.side_effect = OrderSkippedError("too small")

        await runner.process_approved_order(
            make_approved_order(recommendation_id="rec-1")
        )

        intent = ledger.get("rec-1")
        assert intent.status == OrderStatus.SUBMISSION_FAILED.value
        assert intent.reason == "too small"

    @pytest.mark.asyncio
    async def test_submission_exception_persists_failure_and_returns_ack_safe(
        self, durable_runner
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger)
        runner._order_manager.submit_entry.side_effect = RuntimeError("IB down")

        await runner.process_approved_order(
            make_approved_order(recommendation_id="rec-1")
        )

        assert ledger.get("rec-1").status == OrderStatus.SUBMISSION_FAILED.value

    @pytest.mark.asyncio
    async def test_terminal_replay_never_calls_broker(self, durable_runner):
        runner, ledger = durable_runner
        seed_approved_intent(ledger)
        ledger.transition(
            "rec-1", OrderStatus.SUBMISSION_FAILED, reason="IB down"
        )
        ledger.session.commit()

        await runner.process_approved_order(
            make_approved_order(recommendation_id="rec-1")
        )

        runner._order_manager.submit_entry.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("broker_status", ["Cancelled", "ApiCancelled"])
    async def test_broker_cancellation_releases_reservation(
        self, durable_runner, broker_status
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")
        runner.restore_pending_orders()

        await runner.handle_ib_order_status({
            "order_id": "9",
            "status": broker_status,
            "reason": "cancelled at IB",
        })

        assert ledger.get("rec-1").status == OrderStatus.CANCELLED.value
        assert ledger.active_reservations("momentum") == 0

    @pytest.mark.asyncio
    async def test_late_projection_after_startup_is_liquidated_on_kill(
        self, durable_runner, ledger_session
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")
        seed_portfolio_config(ledger_session)
        runner.restore_pending_orders()
        assert runner._positions == {}

        fill_info = make_durable_fill_info(
            execution_id="late-projection", order_id="9", quantity=4
        )
        project_durable_fill(
            ledger_session, fill_info, recommendation_id="rec-1"
        )

        await runner.handle_ib_fill(fill_info)
        await runner.process_kill(kill_message())

        runner._order_manager.submit_exit.assert_awaited_once()
        assert runner._order_manager.submit_exit.await_args.kwargs[
            "ticker"
        ] == "AAPL"
        assert runner._order_manager.submit_exit.await_args.kwargs[
            "quantity"
        ] == pytest.approx(4)

    @pytest.mark.asyncio
    async def test_ambiguous_publish_projected_before_raise_is_liquidated(
        self, durable_runner, ledger_session
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")
        seed_portfolio_config(ledger_session)
        runner.restore_pending_orders()
        fill_info = make_durable_fill_info(
            execution_id="ambiguous-publish", order_id="9", quantity=3
        )

        async def project_then_raise(stream, payload):
            assert stream == "stream:fills"
            assert FillProjector(ledger_session).apply(
                FillMessage.from_stream_dict(payload)
            ) is True
            raise RuntimeError("Redis acknowledgement lost")

        runner._redis.publish.side_effect = project_then_raise
        with pytest.raises(RuntimeError, match="acknowledgement lost"):
            await runner.handle_ib_fill(fill_info)

        runner._redis.publish.side_effect = None
        runner._redis.publish.return_value = "message-id"
        await runner.handle_ib_fill(fill_info)
        await runner.process_kill(kill_message())

        runner._order_manager.submit_exit.assert_awaited_once()
        assert runner._order_manager.submit_exit.await_args.kwargs[
            "quantity"
        ] == pytest.approx(3)

    @pytest.mark.asyncio
    async def test_unprojected_local_fill_survives_durable_reload(
        self, durable_runner, ledger_session
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, "rec-aapl", ib_order_id="9")
        seed_approved_intent(
            ledger,
            "rec-msft",
            ib_order_id="10",
            symbol="MSFT",
            con_id=272093,
        )
        seed_portfolio_config(ledger_session)
        runner.restore_pending_orders()
        aapl_fill = make_durable_fill_info(
            execution_id="projected-aapl", order_id="9", quantity=3
        )
        project_durable_fill(
            ledger_session, aapl_fill, recommendation_id="rec-aapl"
        )
        msft_fill = make_durable_fill_info(
            execution_id="pending-msft",
            order_id="10",
            ticker="MSFT",
            con_id=272093,
            quantity=2,
        )

        await runner.handle_ib_fill(msft_fill)
        await runner.handle_ib_fill(aapl_fill)
        await runner.process_kill(kill_message())

        exits = {
            call.kwargs["ticker"]: call.kwargs["quantity"]
            for call in runner._order_manager.submit_exit.await_args_list
        }
        assert exits == {
            "AAPL": pytest.approx(3),
            "MSFT": pytest.approx(2),
        }

    @pytest.mark.asyncio
    async def test_projected_and_pending_fills_same_ticker_are_combined(
        self, durable_runner, ledger_session
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, "rec-projected", ib_order_id="9")
        seed_approved_intent(ledger, "rec-pending", ib_order_id="10")
        seed_portfolio_config(ledger_session)
        runner.restore_pending_orders()
        projected_fill = make_durable_fill_info(
            execution_id="projected-same-ticker", order_id="9", quantity=3
        )
        project_durable_fill(
            ledger_session,
            projected_fill,
            recommendation_id="rec-projected",
        )

        await runner.handle_ib_fill(make_durable_fill_info(
            execution_id="pending-same-ticker", order_id="10", quantity=2
        ))
        await runner.process_kill(kill_message())

        runner._order_manager.submit_exit.assert_awaited_once()
        assert runner._order_manager.submit_exit.await_args.kwargs[
            "quantity"
        ] == pytest.approx(5)

    @pytest.mark.asyncio
    async def test_projected_duplicate_does_not_over_liquidate_or_add_unowned(
        self, durable_runner, ledger_session
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")
        seed_portfolio_config(ledger_session)
        runner.restore_pending_orders()
        fill_info = make_durable_fill_info(
            execution_id="projected-after-local", order_id="9", quantity=2
        )

        await runner.handle_ib_fill(fill_info)
        _, payload = runner._redis.publish.await_args.args
        assert FillProjector(ledger_session).apply(
            FillMessage.from_stream_dict(payload)
        ) is True
        ledger_session.add(Position(
            account_id="OTHER",
            ticker="TSLA",
            portfolio="manual",
            con_id=76792991,
            exchange="SMART",
            currency="USD",
            quantity=99,
            avg_entry_price=200,
            current_price=200,
            peak_price=200,
            highest_price_since_entry=200,
            opened_at=BROKER_TIME,
            status="open",
        ))
        ledger_session.commit()

        await runner.handle_ib_fill(fill_info)
        await runner.handle_ib_fill(fill_info)
        await runner.process_kill(kill_message())

        runner._order_manager.submit_exit.assert_awaited_once()
        assert runner._order_manager.submit_exit.await_args.kwargs[
            "ticker"
        ] == "AAPL"
        assert runner._order_manager.submit_exit.await_args.kwargs[
            "quantity"
        ] == pytest.approx(2)

    @pytest.mark.asyncio
    async def test_reconciliation_reads_projection_and_positions_atomically(
        self, durable_runner, ledger_session
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")
        seed_portfolio_config(ledger_session)
        runner.restore_pending_orders()
        await runner.handle_ib_fill(make_durable_fill_info(
            execution_id="snapshot-pending", order_id="9", quantity=2
        ))
        selects: list[str] = []

        def record_select(_conn, _cursor, statement, *_args):
            if statement.lstrip().upper().startswith("SELECT"):
                selects.append(statement)

        engine = ledger_session.get_bind()
        event.listen(engine, "before_cursor_execute", record_select)
        try:
            await runner.process_kill(kill_message())
        finally:
            event.remove(engine, "before_cursor_execute", record_select)

        # The reconciliation must be a single atomic read (projection + positions
        # in one UNION query). The per-position exit-dedup reads that follow are a
        # separate, legitimate concern and are not counted here.
        reconciliation_selects = [s for s in selects if "row_kind" in s]
        assert len(reconciliation_selects) == 1

    @pytest.mark.asyncio
    async def test_inactive_before_fill_is_submission_failed(
        self, durable_runner
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")
        runner.restore_pending_orders()

        await runner.handle_ib_order_status({
            "order_id": "9", "status": "Inactive", "reason": "rejected"
        })

        assert ledger.get("rec-1").status == OrderStatus.SUBMISSION_FAILED.value

    @pytest.mark.asyncio
    async def test_inactive_after_partial_fill_is_cancelled(
        self, durable_runner
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9", filled_quantity=5)
        ledger.get("rec-1").status = OrderStatus.PARTIALLY_FILLED.value
        ledger.session.commit()
        runner.restore_pending_orders()

        await runner.handle_ib_order_status({
            "order_id": "9", "status": "Inactive", "reason": "rejected remainder"
        })

        assert ledger.get("rec-1").status == OrderStatus.CANCELLED.value

    @pytest.mark.asyncio
    async def test_inactive_race_uses_broker_filled_quantity(
        self, durable_runner
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9", filled_quantity=0)
        runner.restore_pending_orders()

        await runner.handle_ib_order_status({
            "order_id": "9", "status": "Inactive", "reason": "remainder",
            "filled_quantity": 5,
        })

        assert ledger.get("rec-1").status == OrderStatus.CANCELLED.value

    @pytest.mark.asyncio
    async def test_completed_inactive_without_reason_never_stays_active(
        self, durable_runner
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9", filled_quantity=0)
        runner.restore_pending_orders()

        await runner.handle_ib_order_status({
            "order_id": "9", "status": "Inactive", "reason": "",
            "filled_quantity": 0, "completed_order_confirmed": True,
        })

        intent = ledger.get("rec-1")
        assert intent.status == OrderStatus.SUBMISSION_FAILED.value
        assert intent.reason == "IB completed order is Inactive"

    @pytest.mark.asyncio
    async def test_expiry_requires_completed_order_confirmation(
        self, durable_runner
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")
        runner.restore_pending_orders()

        await runner.handle_ib_order_status({
            "order_id": "9", "status": "Expired",
            "completed_order_confirmed": False,
        })
        assert ledger.get("rec-1").status == OrderStatus.SUBMITTED.value

        await runner.handle_ib_order_status({
            "order_id": "9", "status": "Expired",
            "completed_order_confirmed": True,
        })
        assert ledger.get("rec-1").status == OrderStatus.EXPIRED.value

    @pytest.mark.asyncio
    async def test_live_filled_status_waits_for_fill_projector(
        self, durable_runner
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")
        runner.restore_pending_orders()

        await runner.handle_ib_order_status({
            "order_id": "9", "status": "Filled",
            "filled_quantity": 50, "completed_order_confirmed": False,
        })

        assert ledger.get("rec-1").status == OrderStatus.SUBMITTED.value

    @pytest.mark.asyncio
    async def test_fill_uses_persisted_attribution_and_execution_identity(
        self, durable_runner
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")
        runner.restore_pending_orders()

        await runner.handle_ib_fill({
            "execution_id": "exec-1",
            "account_id": "DUN551088",
            "timestamp": BROKER_TIME,
            "order_id": "9",
            "con_id": 265598,
            "ticker": "AAPL",
            "exchange": "SMART",
            "currency": "USD",
            "side": "buy",
            "quantity": 5.0,
            "cumulative_quantity": 5.0,
            "fill_price": 149.5,
            "commission": 0.25,
            "commission_currency": "SGD",
            "commission_trading": 0.2,
            "commission_fx_base_per_trading": 1.25,
            "order_done": False,
        })

        _, payload = runner._redis.publish.call_args.args
        assert payload["recommendation_id"] == "rec-1"
        assert payload["portfolio"] == "momentum"
        assert payload["execution_id"] == "exec-1"
        assert payload["account_id"] == "DUN551088"
        assert payload["cumulative_quantity"] == "5.0"
        assert payload["commission"] == "0.25"
        assert payload["commission_currency"] == "SGD"
        assert payload["commission_trading"] == "0.2"
        assert payload["commission_fx_base_per_trading"] == "1.25"
        assert payload["timestamp"] == "2026-07-24T08:30:00Z"

    @pytest.mark.asyncio
    async def test_duplicate_fill_handler_is_ignored_before_publish_and_position(
        self, durable_runner
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")
        runner.restore_pending_orders()
        fill_info = {
            "execution_id": "exec-duplicate",
            "account_id": "DUN551088",
            "timestamp": BROKER_TIME,
            "order_id": "9",
            "con_id": 265598,
            "ticker": "AAPL",
            "exchange": "SMART",
            "currency": "USD",
            "side": "buy",
            "quantity": 5.0,
            "cumulative_quantity": 5.0,
            "fill_price": 149.5,
            "commission": 0.25,
            "commission_currency": "USD",
            "commission_trading": 0.25,
            "commission_fx_base_per_trading": None,
            "order_done": False,
        }

        await runner.handle_ib_fill(fill_info)
        await runner.handle_ib_fill(fill_info)

        runner._redis.publish.assert_awaited_once()
        assert runner._positions == {"AAPL": pytest.approx(5.0)}

    @pytest.mark.asyncio
    async def test_restart_uses_durable_execution_identity_before_side_effects(
        self, durable_runner
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")
        ledger.session.add(ExecutionFill(
            account_id="DUN551088",
            execution_id="exec-stored",
            ib_order_id="9",
            recommendation_id="rec-1",
            portfolio="momentum",
            con_id=265598,
            symbol="AAPL",
            exchange="SMART",
            currency="USD",
            side="BUY",
            quantity=5.0,
            price=149.5,
            commission=0.25,
            commission_currency="USD",
            commission_trading=0.25,
            commission_fx_base_per_trading=None,
            cumulative_quantity=5.0,
            executed_at=BROKER_TIME,
            projection_applied=True,
        ))
        ledger.session.commit()

        await runner.handle_ib_fill({
            "execution_id": "exec-stored",
            "account_id": "DUN551088",
            "timestamp": BROKER_TIME,
            "order_id": "9",
            "con_id": 265598,
            "ticker": "AAPL",
            "exchange": "SMART",
            "currency": "USD",
            "side": "buy",
            "quantity": 5.0,
            "cumulative_quantity": 5.0,
            "fill_price": 149.5,
            "commission": 0.25,
            "commission_currency": "USD",
            "commission_trading": 0.25,
            "commission_fx_base_per_trading": None,
            "order_done": False,
        })

        runner._redis.publish.assert_not_awaited()
        assert runner._positions == {}

    @pytest.mark.asyncio
    async def test_duplicate_commission_retries_after_transient_publish_failure(
        self, durable_runner, ledger_session
    ):
        from services.execution.ib_executor import IBExecutor

        class Event:
            def __init__(self):
                self.callbacks = []

            def __iadd__(self, callback):
                self.callbacks.append(callback)
                return self

            def emit(self, *args):
                for callback in self.callbacks:
                    callback(*args)

        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")
        ledger_session.add(PortfolioConfig(
            portfolio="momentum",
            capital=10_000,
            cash=10_000,
            created_at=BROKER_TIME,
            updated_at=BROKER_TIME,
        ))
        ledger_session.commit()
        runner.restore_pending_orders()

        successful_publications = []
        publish_attempts = 0

        async def publish_with_transient_failure(stream, payload):
            nonlocal publish_attempts
            publish_attempts += 1
            if publish_attempts == 1:
                raise RuntimeError("transient Redis failure")
            successful_publications.append((stream, payload))
            return "message-id"

        runner._redis.publish.side_effect = publish_with_transient_failure
        handler_attempts = 0
        handler_errors = []

        async def handle_with_error_capture(payload):
            nonlocal handler_attempts
            handler_attempts += 1
            try:
                await runner.handle_ib_fill(payload)
            except RuntimeError as exc:
                handler_errors.append(str(exc))

        executor = IBExecutor("h", 7497, 1)
        executor._ib = MagicMock()
        executor._ib.accountValues.return_value = []
        executor.set_fill_handler(handle_with_error_capture)
        trade = MagicMock()
        trade.fillEvent = Event()
        trade.commissionReportEvent = Event()
        trade.statusEvent = Event()
        trade.isDone.return_value = False
        executor._register_trade("9", trade, ticker="AAPL", side="buy")
        fill = SimpleNamespace(
            execution=SimpleNamespace(
                execId="exec-retry",
                acctNumber="DUN551088",
                shares=2,
                cumQty=2,
                price=149.5,
                time=BROKER_TIME,
            ),
            contract=SimpleNamespace(
                conId=265598,
                exchange="SMART",
                currency="USD",
            ),
            commissionReport=SimpleNamespace(commission=0.0, currency=""),
        )
        report = SimpleNamespace(commission=0.25, currency="USD")

        trade.commissionReportEvent.emit(trade, fill, report)
        trade.commissionReportEvent.emit(trade, fill, report)
        await asyncio.sleep(0)

        assert handler_attempts == 2
        assert handler_errors == ["transient Redis failure"]
        assert publish_attempts == 2
        assert len(successful_publications) == 1
        assert runner._positions == {"AAPL": pytest.approx(2.0)}

        stream, payload = successful_publications[0]
        assert stream == "stream:fills"
        message = FillMessage.from_stream_dict(payload)
        assert message.timestamp == BROKER_TIME
        projector = FillProjector(ledger_session)
        assert projector.apply(message) is True
        assert projector.apply(message) is False
        assert ledger_session.scalar(select(Position)).quantity == pytest.approx(2)
        assert ledger_session.scalar(select(PortfolioConfig.cash)) == pytest.approx(
            9_700.75
        )

    @pytest.mark.asyncio
    async def test_late_fill_uses_terminal_intent_attribution(
        self, durable_runner
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")
        runner.restore_pending_orders()
        await runner.handle_ib_order_status({
            "order_id": "9", "status": "Cancelled", "reason": "cancelled"
        })

        await runner.handle_ib_fill({
            "execution_id": "late-1", "account_id": "DUN551088",
            "timestamp": BROKER_TIME,
            "order_id": "9", "con_id": 265598, "ticker": "AAPL",
            "exchange": "SMART", "currency": "USD", "side": "buy",
            "quantity": 1.0, "cumulative_quantity": 1.0,
            "fill_price": 149.5, "commission": 0.1, "order_done": True,
        })

        _, payload = runner._redis.publish.call_args.args
        assert payload["recommendation_id"] == "rec-1"
        assert payload["portfolio"] == "momentum"

    @pytest.mark.asyncio
    async def test_cancellation_before_commission_still_projects_fill_once(
        self, durable_runner, ledger_session
    ):
        from services.execution.ib_executor import IBExecutor

        class Event:
            def __init__(self):
                self.callbacks = []

            def __iadd__(self, callback):
                self.callbacks.append(callback)
                return self

            def emit(self, *args):
                for callback in self.callbacks:
                    callback(*args)

        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")
        ledger_session.add(PortfolioConfig(
            portfolio="momentum",
            capital=10_000,
            cash=10_000,
            created_at=BROKER_TIME,
            updated_at=BROKER_TIME,
        ))
        ledger_session.commit()
        runner.restore_pending_orders()

        executor = IBExecutor("h", 7497, 1)
        executor._ib = MagicMock()
        executor._ib.accountValues.return_value = []
        executor.set_fill_handler(runner.handle_ib_fill)
        executor.set_order_status_handler(runner.handle_ib_order_status)
        trade = MagicMock()
        trade.fillEvent = Event()
        trade.commissionReportEvent = Event()
        trade.statusEvent = Event()
        trade.isDone.return_value = True
        trade.orderStatus.status = "Cancelled"
        trade.orderStatus.whyHeld = "cancelled at IB"
        trade.orderStatus.filled = 0
        trade.log = []
        executor._register_trade("9", trade, ticker="AAPL", side="buy")

        trade.statusEvent.emit(trade)
        await asyncio.sleep(0)
        intent = ledger.get("rec-1")
        assert intent.status == OrderStatus.CANCELLED.value
        assert intent.reason == "cancelled at IB"
        ledger_session.rollback()

        fill = SimpleNamespace(
            execution=SimpleNamespace(
                execId="late-exec-1",
                acctNumber="DUN551088",
                shares=2,
                cumQty=2,
                price=149.5,
                time=BROKER_TIME,
            ),
            contract=SimpleNamespace(
                conId=265598,
                exchange="SMART",
                currency="USD",
            ),
            commissionReport=SimpleNamespace(commission=0.0, currency=""),
        )
        report = SimpleNamespace(commission=0.25, currency="USD")
        trade.fillEvent.emit(trade, fill)
        trade.commissionReportEvent.emit(trade, fill, report)
        trade.commissionReportEvent.emit(trade, fill, report)
        await asyncio.sleep(0)

        runner._redis.publish.assert_awaited_once()
        _, payload = runner._redis.publish.await_args.args
        message = FillMessage.from_stream_dict(payload)
        assert message.timestamp == BROKER_TIME
        assert runner._positions == {"AAPL": pytest.approx(2.0)}

        projector = FillProjector(ledger_session)
        assert projector.apply(message) is True
        assert projector.apply(message) is False
        intent = ledger.get("rec-1")
        assert intent.status == OrderStatus.CANCELLED.value
        assert intent.reason == "cancelled at IB"
        assert intent.filled_quantity == pytest.approx(2)
        assert ledger_session.scalar(select(PortfolioConfig.cash)) == pytest.approx(
            9_700.75
        )
        assert ledger_session.scalar(select(Position)).quantity == pytest.approx(2)

    @pytest.mark.asyncio
    async def test_completed_fill_recovery_releases_reservation_without_duplicate(
        self, mock_config, mock_redis, ledger_session
    ):
        from services.execution.order_manager import OrderManager

        ledger = OrderLedger(ledger_session)
        seed_approved_intent(ledger)
        executor = AsyncMock()
        executor.find_order_by_ref.return_value = "77"
        manager = OrderManager(executor, mock_redis, ledger_session)
        runner = ExecutionServiceRunner(
            mock_config, mock_redis, manager, order_ledger=ledger
        )

        async def reconcile_completed(recommendation_id, order_id):
            await runner.handle_ib_order_status({
                "order_id": order_id, "status": "Filled",
                "filled_quantity": 50, "completed_order_confirmed": True,
            })
            return False

        executor.restore_order_by_ref.side_effect = reconcile_completed

        await runner.process_approved_order(
            make_approved_order(recommendation_id="rec-1")
        )

        executor.submit_limit_order.assert_not_awaited()
        assert ledger.get("rec-1").status == OrderStatus.FILLED.value
        assert ledger.active_reservations("momentum") == 0


class TestKillExitCrossesToExecution:
    """KAN-4: a risk-side kill exit must survive the whole hop to execution.

    Risk publishes the exit; execution's record_submission does
    APPROVED->SUBMITTED. If risk left the intent at PROPOSED, that transition
    is illegal and raises *after* the IB order is already live.
    """

    @pytest.mark.asyncio
    async def test_kill_exit_submits_and_attributes_fill(
        self, mock_config, mock_redis, mock_order_manager, ledger_session
    ):
        from unittest.mock import MagicMock as _MagicMock

        from services.risk_management.runner import RiskServiceRunner
        from shared.config import CurrencyConfig, RiskConfig
        from shared.models import Position
        from shared.schemas.messages import ApprovedOrderMessage as _Approved

        ledger_session.add(Position(
            account_id="DUN551088",
            ticker="AAPL",
            portfolio="momentum",
            con_id=265598,
            exchange="SMART",
            currency="USD",
            quantity=10.0,
            avg_entry_price=100,
            current_price=100,
            peak_price=100,
            highest_price_since_entry=100,
            opened_at=BROKER_TIME,
            status="open",
        ))
        ledger_session.commit()

        risk_config = _MagicMock(spec=AppConfig)
        risk_config.mode = "paper"
        risk_config.risk = RiskConfig()
        risk_config.currency = CurrencyConfig()
        risk_redis = AsyncMock()
        risk_redis.publish = AsyncMock(return_value="msg-1")
        risk_redis.stream_length = AsyncMock(return_value=0)
        ledger = OrderLedger(ledger_session)
        risk_runner = RiskServiceRunner(
            config=risk_config,
            redis_client=risk_redis,
            db_session=ledger_session,
            order_ledger=ledger,
        )

        await risk_runner.process_kill(KillMessage(
            timestamp=BROKER_TIME,
            triggered_by="test",
            reason="kill exit crosses to execution",
        ))
        ledger_session.rollback()

        (exit_order,) = [
            _Approved.from_stream_dict(c.args[1])
            for c in risk_redis.publish.call_args_list
            if c.args[0] == "stream:approved_orders"
        ]
        exit_id = exit_order.recommendation_id

        execution_runner = ExecutionServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            order_manager=mock_order_manager,
            order_ledger=ledger,
        )
        await execution_runner.process_approved_order(exit_order)  # must not raise

        intent = ledger.get(exit_id)
        assert intent.status == OrderStatus.SUBMITTED.value
        assert intent.ib_order_id == "order-002"
        ledger_session.rollback()

        await execution_runner.handle_ib_fill(make_durable_fill_info(
            execution_id="exec-kill-1",
            order_id="order-002",
            quantity=10.0,
        ) | {"side": "sell", "order_done": True})

        fills = [
            FillMessage.from_stream_dict(c.args[1])
            for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:fills"
        ]
        assert [f.recommendation_id for f in fills] == [exit_id]


class TestApprovedOrderProcessing:
    @pytest.mark.asyncio
    async def test_process_buy_approved_order(
        self, runner, mock_redis, mock_order_manager
    ):
        """Buy approved order should submit limit entry via OrderManager."""
        order = make_approved_order(
            ticker="AAPL", action="buy", order_type="limit", limit_price=150.0
        )

        await runner.process_approved_order(order)

        mock_order_manager.submit_entry.assert_called_once_with(
            ticker="AAPL",
            quantity=50,
            limit_price=150.0,
            recommendation_id="rec-001",
        )

    @pytest.mark.asyncio
    async def test_process_sell_approved_order(
        self, runner, mock_redis, mock_order_manager
    ):
        """Sell approved order should submit market exit via OrderManager."""
        order = make_approved_order(
            ticker="AAPL",
            action="sell",
            order_type="market",
            limit_price=None,
            recommendation_id="rec-002",
        )

        await runner.process_approved_order(order)

        mock_order_manager.submit_exit.assert_called_once_with(
            ticker="AAPL",
            quantity=50,
            recommendation_id="rec-002",
        )

    @pytest.mark.asyncio
    async def test_buy_order_does_not_publish_fill_at_submission(
        self, runner, mock_redis, mock_order_manager
    ):
        """Submission is not a fill: nothing goes to stream:fills yet."""
        order = make_approved_order(
            ticker="AAPL", action="buy", limit_price=150.0
        )

        await runner.process_approved_order(order)

        mock_redis.publish.assert_not_called()
        # The order is tracked so a later IB fill can be attributed.
        assert order in runner._pending_orders.values()

    @pytest.mark.asyncio
    async def test_ib_fill_publishes_fill_with_execution_data(
        self, runner, mock_redis, mock_order_manager
    ):
        """A real IB fill event publishes actual execution price/quantity."""
        order = make_approved_order(ticker="AAPL", action="buy", limit_price=150.0)
        await runner.process_approved_order(order)
        (order_id,) = runner._pending_orders.keys()

        await runner.handle_ib_fill({
            "timestamp": BROKER_TIME,
            "order_id": order_id,
            "ticker": "AAPL",
            "side": "buy",
            "quantity": 100.0,
            "fill_price": 149.87,
            "commission": 0.5,
            "order_done": True,
        })

        mock_redis.publish.assert_called_once()
        stream, payload = mock_redis.publish.call_args[0]
        assert stream == "stream:fills"
        assert payload["fill_price"] == "149.87"
        assert payload["recommendation_id"] == order.recommendation_id
        # Completed order is no longer pending.
        assert order_id not in runner._pending_orders

    @pytest.mark.asyncio
    async def test_partial_fill_keeps_order_pending(
        self, runner, mock_redis, mock_order_manager
    ):
        """A partial fill publishes but keeps the order tracked."""
        order = make_approved_order(ticker="AAPL", action="buy", limit_price=150.0)
        await runner.process_approved_order(order)
        (order_id,) = runner._pending_orders.keys()

        await runner.handle_ib_fill({
            "timestamp": BROKER_TIME,
            "order_id": order_id,
            "ticker": "AAPL",
            "side": "buy",
            "quantity": 40.0,
            "fill_price": 149.90,
            "commission": 0.2,
            "order_done": False,
        })

        mock_redis.publish.assert_called_once()
        assert order_id in runner._pending_orders

    @pytest.mark.asyncio
    async def test_sell_order_does_not_publish_fill_at_submission(
        self, runner, mock_redis, mock_order_manager
    ):
        """Market exits also wait for the real IB fill."""
        order = make_approved_order(
            ticker="AAPL",
            action="sell",
            order_type="market",
            limit_price=None,
        )

        await runner.process_approved_order(order)

        mock_redis.publish.assert_not_called()
        assert order in runner._pending_orders.values()


class TestHaltGate:
    """KAN-12: execution reads the durable halt latch before submitting.

    The gate blocks exposure-INCREASING orders only — a halt is exactly when
    the risk service publishes liquidation sells, so blocking those would
    block the system's own emergency flatten.
    """

    @staticmethod
    def _halt(session, *, mode: str = "paper") -> None:
        from shared.halt_state import HaltStateRepository

        HaltStateRepository(session).record_halt(
            mode=mode,
            source="kill",
            reason="test halt",
            triggered_by="test",
            now=BROKER_TIME,
        )
        session.commit()

    @pytest.mark.asyncio
    async def test_halted_buy_is_durably_rejected(
        self, durable_runner, ledger_session, mock_order_manager
    ):
        """Design test #23: halted buy → SUBMISSION_FAILED reason='halted'."""
        runner, ledger = durable_runner
        seed_approved_intent(ledger, recommendation_id="rec-1")
        self._halt(ledger_session)

        await runner.process_approved_order(
            make_approved_order(recommendation_id="rec-1", action="buy")
        )

        mock_order_manager.submit_entry.assert_not_awaited()
        intent = ledger.get("rec-1")
        assert intent.status == OrderStatus.SUBMISSION_FAILED.value
        assert intent.reason == "halted"

    @pytest.mark.asyncio
    async def test_halted_buy_is_acked_and_never_dead_lettered(
        self, durable_runner, ledger_session, mock_redis, mock_order_manager
    ):
        """Design test #9: durably rejected AND acked — absent from the DLQ,
        so it is not redelivered via the PEL."""
        runner, ledger = durable_runner
        seed_approved_intent(ledger, recommendation_id="rec-1")
        self._halt(ledger_session)
        mock_redis.send_to_dead_letter = AsyncMock()
        order = make_approved_order(recommendation_id="rec-1", action="buy")
        mock_redis.read_group = AsyncMock(
            return_value=[
                SimpleNamespace(message_id="5-0", data=order.to_stream_dict())
            ]
        )

        await runner._consume_and_process(
            "stream:approved_orders",
            ApprovedOrderMessage.from_stream_dict,
            runner.process_approved_order,
            count=1,
            block_ms=0,
        )

        mock_order_manager.submit_entry.assert_not_awaited()
        mock_redis.send_to_dead_letter.assert_not_awaited()
        mock_redis.ack.assert_awaited_once_with(
            "stream:approved_orders", "execution_service", "5-0"
        )

    @pytest.mark.asyncio
    async def test_halted_risk_reducing_sell_still_submits(
        self, durable_runner, ledger_session, mock_order_manager
    ):
        """Design test #23: a ledger-backed risk-reducing sell submits during a
        halt — the emergency flatten must not be blocked by the gate."""
        runner, ledger = durable_runner
        seed_approved_intent(
            ledger, recommendation_id="exit-1", action="SELL"
        )
        self._halt(ledger_session)

        await runner.process_approved_order(
            make_approved_order(
                recommendation_id="exit-1",
                action="sell",
                order_type="market",
                limit_price=None,
            )
        )

        mock_order_manager.submit_exit.assert_awaited_once_with(
            ticker="AAPL", quantity=50, recommendation_id="exit-1"
        )
        assert ledger.get("exit-1").status == OrderStatus.SUBMITTED.value

    @pytest.mark.asyncio
    async def test_unhalted_buy_submits_unchanged(
        self, durable_runner, mock_order_manager
    ):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, recommendation_id="rec-1")

        await runner.process_approved_order(
            make_approved_order(recommendation_id="rec-1", action="buy")
        )

        mock_order_manager.submit_entry.assert_awaited_once()
        assert ledger.get("rec-1").status == OrderStatus.SUBMITTED.value

    @pytest.mark.asyncio
    async def test_pel_replay_on_restart_is_gated(
        self, durable_runner, ledger_session, mock_redis, mock_order_manager
    ):
        """Design test #10: restart with halted PEL entries → all buys
        rejected, none submitted. The replay path is gated, not just the loop."""
        runner, ledger = durable_runner
        seed_approved_intent(ledger, recommendation_id="rec-1")
        seed_approved_intent(
            ledger, recommendation_id="rec-2", symbol="MSFT", con_id=272093
        )
        self._halt(ledger_session)
        replayed = [
            SimpleNamespace(
                message_id=f"{i}-0",
                data=make_approved_order(
                    recommendation_id=rec, action="buy"
                ).to_stream_dict(),
            )
            for i, rec in enumerate(("rec-1", "rec-2"), start=1)
        ]
        mock_redis.drain_pending = AsyncMock(
            side_effect=[replayed, []]
        )
        mock_redis.send_to_dead_letter = AsyncMock()

        await runner.setup()

        mock_order_manager.submit_entry.assert_not_awaited()
        mock_redis.send_to_dead_letter.assert_not_awaited()
        for rec in ("rec-1", "rec-2"):
            assert ledger.get(rec).status == OrderStatus.SUBMISSION_FAILED.value

    @pytest.mark.asyncio
    async def test_cleared_halt_does_not_resurrect_rejected_orders(
        self, durable_runner, ledger_session, mock_order_manager
    ):
        """Design test #12: no zombie resubmission after the halt clears."""
        from shared.halt_state import HaltStateRepository

        runner, ledger = durable_runner
        seed_approved_intent(ledger, recommendation_id="rec-1")
        self._halt(ledger_session)
        order = make_approved_order(recommendation_id="rec-1", action="buy")
        await runner.process_approved_order(order)

        HaltStateRepository(ledger_session).clear_halt(
            mode="paper", cleared_by="operator", now=BROKER_TIME
        )
        ledger_session.commit()

        await runner.process_approved_order(order)

        mock_order_manager.submit_entry.assert_not_awaited()
        assert ledger.get("rec-1").status == OrderStatus.SUBMISSION_FAILED.value

    @pytest.mark.asyncio
    async def test_gate_is_inert_without_a_ledger(
        self, runner, mock_order_manager
    ):
        """AC7: no ledger injected → no session to read, gate stays out of the
        way rather than failing closed on a system that has no halt table."""
        assert runner._halt_store is None

        await runner.process_approved_order(
            make_approved_order(action="buy")
        )

        mock_order_manager.submit_entry.assert_awaited_once()


class TestHaltLookupFailure:
    """KAN-12 / design test #24: an unreadable halt latch is its own state —
    neither halted nor clear. Retain, retry with backoff, page separately."""

    @pytest.fixture()
    def failing_halt_runner(self, durable_runner, monkeypatch):
        runner, ledger = durable_runner
        calls: list[str] = []

        def boom(*, mode: str):
            calls.append(mode)
            raise RuntimeError("connection refused")

        monkeypatch.setattr(runner._halt_store, "load_active_halt", boom)
        seed_approved_intent(ledger, recommendation_id="rec-1")
        return runner, ledger, calls

    @pytest.mark.asyncio
    async def test_message_is_retained_not_acked_and_not_dead_lettered(
        self, failing_halt_runner, mock_redis, mock_order_manager
    ):
        runner, _ledger, _calls = failing_halt_runner
        mock_redis.send_to_dead_letter = AsyncMock()
        order = make_approved_order(recommendation_id="rec-1", action="buy")
        mock_redis.read_group = AsyncMock(
            return_value=[
                SimpleNamespace(message_id="7-0", data=order.to_stream_dict())
            ]
        )

        with patch("asyncio.sleep", new=AsyncMock()):
            await runner._consume_and_process(
                "stream:approved_orders",
                ApprovedOrderMessage.from_stream_dict,
                runner.process_approved_order,
                count=1,
                block_ms=0,
            )

        mock_order_manager.submit_entry.assert_not_awaited()
        mock_redis.ack.assert_not_awaited()
        mock_redis.send_to_dead_letter.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lookup_is_retried_with_backoff(self, failing_halt_runner):
        from services.execution.runner import (
            HALT_LOOKUP_RETRY_BACKOFF_SECONDS,
            HaltStateUnavailable,
        )

        runner, _ledger, calls = failing_halt_runner
        sleep = AsyncMock()

        with patch("asyncio.sleep", new=sleep):
            with pytest.raises(HaltStateUnavailable):
                await runner.process_approved_order(
                    make_approved_order(recommendation_id="rec-1", action="buy")
                )

        assert len(calls) == len(HALT_LOOKUP_RETRY_BACKOFF_SECONDS) + 1
        assert [c.args[0] for c in sleep.await_args_list] == list(
            HALT_LOOKUP_RETRY_BACKOFF_SECONDS
        )

    @pytest.mark.asyncio
    async def test_unable_to_determine_halt_state_is_paged(
        self, failing_halt_runner, mock_redis
    ):
        runner, _ledger, _calls = failing_halt_runner
        order = make_approved_order(recommendation_id="rec-1", action="buy")
        mock_redis.read_group = AsyncMock(
            return_value=[
                SimpleNamespace(message_id="7-0", data=order.to_stream_dict())
            ]
        )

        with patch("asyncio.sleep", new=AsyncMock()):
            await runner._consume_and_process(
                "stream:approved_orders",
                ApprovedOrderMessage.from_stream_dict,
                runner.process_approved_order,
                count=1,
                block_ms=0,
            )

        alerts = [
            AlertMessage.from_stream_dict(c.args[1])
            for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:alerts"
        ]
        assert [a.event_type for a in alerts] == ["halt_state_unavailable"]
        assert alerts[0].priority == "critical"
        assert "unable to determine halt state" in alerts[0].message.lower()

    @pytest.mark.asyncio
    async def test_pel_replay_retains_on_lookup_failure(
        self, failing_halt_runner, mock_redis
    ):
        runner, _ledger, _calls = failing_halt_runner
        mock_redis.send_to_dead_letter = AsyncMock()
        mock_redis.drain_pending = AsyncMock(side_effect=[
            [SimpleNamespace(
                message_id="8-0",
                data=make_approved_order(
                    recommendation_id="rec-1", action="buy"
                ).to_stream_dict(),
            )],
            [],
        ])

        with patch("asyncio.sleep", new=AsyncMock()):
            await runner.setup()

        mock_redis.ack.assert_not_awaited()
        mock_redis.send_to_dead_letter.assert_not_awaited()


class TestKillHandling:
    @pytest.mark.asyncio
    async def test_kill_event_cancels_all_and_sells(
        self, runner, mock_redis, mock_order_manager
    ):
        """Kill event should cancel all open orders and market-sell all positions."""
        mock_order_manager.open_orders = {
            "order-001": {
                "ticker": "AAPL",
                "quantity": 50,
                "recommendation_id": "rec-001",
            },
        }
        runner._positions = {"AAPL": 100, "MSFT": 75}

        kill_msg = KillMessage(
            timestamp=datetime.now(timezone.utc),
            triggered_by="admin",
            reason="emergency",
        )

        await runner.process_kill(kill_msg)

        mock_order_manager.cancel_all_orders.assert_called_once()
        # Should have submitted market exits for all positions
        assert mock_order_manager.submit_exit.call_count == 2

    @pytest.mark.asyncio
    async def test_kill_uses_deterministic_exit_id(
        self, runner, mock_order_manager
    ):
        """Kill exits must carry a deterministic per-event id so a replay is a
        no-op instead of re-selling (review 1.3)."""
        runner._positions = {"AAPL": 10}
        msg = KillMessage(
            timestamp=datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc),
            triggered_by="admin",
            reason="e",
        )
        await runner.process_kill(msg)

        epoch = int(msg.timestamp.timestamp())
        assert mock_order_manager.submit_exit.await_args.kwargs[
            "recommendation_id"
        ] == f"liq-paper-AAPL-{epoch}"

    @pytest.mark.asyncio
    async def test_kill_per_ticker_failure_continues_and_alerts(
        self, runner, mock_redis, mock_order_manager
    ):
        """One ticker failing (e.g. IB disconnect) must not abort the rest, and
        the critical alert must always fire (review 1.5)."""
        runner._positions = {"AAPL": 10, "MSFT": 5}

        async def flaky(*, ticker, quantity, recommendation_id):
            if ticker == "AAPL":
                raise RuntimeError("IB disconnected")
            return "order-x"

        mock_order_manager.submit_exit.side_effect = flaky

        await runner.process_kill(kill_message())

        # Both tickers attempted despite AAPL failing.
        assert mock_order_manager.submit_exit.await_count == 2
        alerts = [
            c for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:alerts"
        ]
        assert len(alerts) >= 1

    @pytest.mark.asyncio
    async def test_kill_skips_ticker_already_liquidated_via_ledger(
        self, durable_runner
    ):
        """If the authoritative (risk) path already submitted the exit for this
        kill, execution's defense-in-depth net must skip it — no double-sell."""
        runner, ledger = durable_runner
        msg = kill_message()
        epoch = int(msg.timestamp.timestamp())
        exit_id = f"liq-paper-AAPL-{epoch}"
        # A real open AAPL position exists (so reconcile keeps it) ...
        ledger.session.add(
            Position(
                account_id="DUN551088",
                ticker="AAPL",
                portfolio="momentum",
                con_id=265598,
                exchange="SMART",
                currency="USD",
                quantity=10,
                avg_entry_price=100,
                current_price=100,
                peak_price=100,
                highest_price_since_entry=100,
                opened_at=BROKER_TIME,
                status="open",
            )
        )
        ledger.session.commit()
        # ... but risk already created + submitted this deterministic exit.
        seed_approved_intent(ledger, recommendation_id=exit_id, ib_order_id="77")

        await runner.process_kill(msg)

        runner._order_manager.submit_exit.assert_not_awaited()


class TestUnfilledSweepDriver:
    """A periodic driver must actually run the unfilled-order sweep (the logic
    had no production caller before)."""

    @pytest.mark.asyncio
    async def test_maybe_run_unfilled_sweep_respects_interval(self, runner):
        runner._market_calendar = MagicMock()
        runner._order_manager.sweep_unfilled_orders = AsyncMock(return_value=0)
        interval = runner._reprice_interval_seconds

        assert await runner.maybe_run_unfilled_sweep(now=1000.0) is True
        assert await runner.maybe_run_unfilled_sweep(now=1000.0 + interval - 1) is False
        assert await runner.maybe_run_unfilled_sweep(now=1000.0 + interval + 1) is True
        assert runner._order_manager.sweep_unfilled_orders.await_count == 2

    @pytest.mark.asyncio
    async def test_sweep_skipped_without_market_calendar(self, runner):
        runner._market_calendar = None
        runner._order_manager.sweep_unfilled_orders = AsyncMock()
        assert await runner.maybe_run_unfilled_sweep(now=1000.0) is False
        runner._order_manager.sweep_unfilled_orders.assert_not_awaited()


class TestPostHaltSweep:
    """KAN-13: a post-halt reconcile sweep on its own timer.

    KAN-12's pre-submit check narrows the window between "halt lands" and
    "order reaches IB" but cannot close it. In that window the order is live
    at the broker and may have no ib_order_id in the ledger at all, so only an
    account-wide orderRef scan can find it.
    """

    @staticmethod
    def _halt(session, *, mode: str = "paper") -> None:
        from shared.halt_state import HaltStateRepository

        HaltStateRepository(session).record_halt(
            mode=mode,
            source="kill",
            reason="test halt",
            triggered_by="test",
            now=BROKER_TIME,
        )
        session.commit()

    @staticmethod
    def _broker_order(
        order_id: str = "77",
        order_ref: str = "rec-1",
        action: str = "BUY",
        ticker: str = "AAPL",
        quantity: float = 50.0,
    ):
        from services.execution.ib_executor import OpenBrokerOrder

        return OpenBrokerOrder(
            order_id=order_id,
            order_ref=order_ref,
            action=action,
            ticker=ticker,
            quantity=quantity,
            account_id="DUN551088",
        )

    @pytest.mark.asyncio
    async def test_raced_order_is_cancelled_by_the_sweep(
        self, durable_runner, ledger_session, mock_order_manager
    ):
        """Design test #11: the buy slipped past the pre-submit check and
        reached IB; the sweep cancels it."""
        runner, ledger = durable_runner
        seed_approved_intent(ledger, recommendation_id="rec-1", ib_order_id="77")
        self._halt(ledger_session)
        mock_order_manager.list_open_broker_orders = AsyncMock(
            return_value=[self._broker_order()]
        )

        assert await runner.maybe_run_halt_sweep(now=1000.0) is True

        mock_order_manager.cancel_broker_order.assert_awaited_once_with("77")

    @pytest.mark.asyncio
    async def test_sweep_fires_without_a_market_calendar(
        self, durable_runner, ledger_session, mock_order_manager
    ):
        """Design test #13: independence from the calendar-gated unfilled
        sweep, which silently no-ops when _market_calendar is unset. This is
        the test that distinguishes the story from a shortcut."""
        runner, ledger = durable_runner
        runner._market_calendar = None
        seed_approved_intent(ledger, recommendation_id="rec-1", ib_order_id="77")
        self._halt(ledger_session)
        mock_order_manager.list_open_broker_orders = AsyncMock(
            return_value=[self._broker_order()]
        )

        assert await runner.maybe_run_halt_sweep(now=1000.0) is True
        mock_order_manager.cancel_broker_order.assert_awaited_once_with("77")

    @pytest.mark.asyncio
    async def test_orderref_only_order_is_discovered_and_cancelled(
        self, durable_runner, ledger_session, mock_order_manager
    ):
        """Design test #25: the crash-window order — live at the broker, no
        ib_order_id in the ledger, so invisible to any ledger-keyed lookup.
        The sweep finds it by orderRef, stamps the broker id so the coming
        Cancelled callback is attributable, and cancels."""
        runner, ledger = durable_runner
        seed_approved_intent(ledger, recommendation_id="rec-1")
        assert ledger.get("rec-1").ib_order_id is None
        ledger.session.rollback()
        self._halt(ledger_session)
        mock_order_manager.list_open_broker_orders = AsyncMock(
            return_value=[self._broker_order(order_id="99", order_ref="rec-1")]
        )

        await runner.maybe_run_halt_sweep(now=1000.0)

        mock_order_manager.cancel_broker_order.assert_awaited_once_with("99")
        intent = ledger.get("rec-1")
        assert intent.ib_order_id == "99"
        assert intent.status == OrderStatus.SUBMITTED.value
        ledger.session.rollback()

    @pytest.mark.asyncio
    async def test_sells_are_never_cancelled_during_a_halt(
        self, durable_runner, ledger_session, mock_order_manager, mock_redis
    ):
        """Part of design test #23: cancelling the emergency flatten during a
        halt is the one catastrophic outcome, so sells are exempt — the
        ledgered stop/exit and the kill liquidation that has no intent alike."""
        runner, ledger = durable_runner
        seed_approved_intent(
            ledger, recommendation_id="exit-1", action="SELL", ib_order_id="55"
        )
        self._halt(ledger_session)
        mock_order_manager.list_open_broker_orders = AsyncMock(
            return_value=[
                self._broker_order(
                    order_id="55", order_ref="exit-1", action="SELL"
                ),
                self._broker_order(
                    order_id="56",
                    order_ref="liq-paper-AAPL-1754568000",
                    action="SELL",
                ),
            ]
        )

        await runner.maybe_run_halt_sweep(now=1000.0)

        mock_order_manager.cancel_broker_order.assert_not_awaited()
        assert [
            AlertMessage.from_stream_dict(c.args[1]).event_type
            for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:alerts"
        ] == []

    @pytest.mark.asyncio
    async def test_unknown_orderref_is_alerted_not_cancelled(
        self, durable_runner, ledger_session, mock_order_manager, mock_redis
    ):
        """AC5: the ref may belong to another client id or a manual TWS
        order. Cancelling someone else's order silently is worse than
        reporting it."""
        runner, _ledger = durable_runner
        self._halt(ledger_session)
        mock_order_manager.list_open_broker_orders = AsyncMock(
            return_value=[
                self._broker_order(order_id="1234", order_ref="manual-tws")
            ]
        )

        await runner.maybe_run_halt_sweep(now=1000.0)

        mock_order_manager.cancel_broker_order.assert_not_awaited()
        alerts = [
            AlertMessage.from_stream_dict(c.args[1])
            for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:alerts"
        ]
        assert [a.event_type for a in alerts] == ["halt_sweep_unknown_order"]
        assert alerts[0].context["order_id"] == "1234"

    @pytest.mark.asyncio
    async def test_sweep_is_a_no_op_when_not_halted(
        self, durable_runner, mock_order_manager
    ):
        """AC6: loop iterations with no halt produce no broker calls."""
        runner, ledger = durable_runner
        seed_approved_intent(ledger, recommendation_id="rec-1", ib_order_id="77")

        for tick in range(3):
            assert await runner.maybe_run_halt_sweep(now=1000.0 + tick) is False

        mock_order_manager.list_open_broker_orders.assert_not_awaited()
        mock_order_manager.cancel_broker_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sweep_repeats_on_its_own_interval_while_halted(
        self, durable_runner, ledger_session, mock_order_manager
    ):
        """Fires immediately when the halt is first seen, then periodically —
        an order placed after the first pass is still caught."""
        from services.execution.runner import HALT_SWEEP_INTERVAL_SECONDS

        runner, ledger = durable_runner
        seed_approved_intent(ledger, recommendation_id="rec-1", ib_order_id="77")
        self._halt(ledger_session)
        interval = HALT_SWEEP_INTERVAL_SECONDS
        mock_order_manager.list_open_broker_orders = AsyncMock(return_value=[])

        assert await runner.maybe_run_halt_sweep(now=1000.0) is True
        assert await runner.maybe_run_halt_sweep(now=1000.0 + interval - 1) is False
        assert await runner.maybe_run_halt_sweep(now=1000.0 + interval + 1) is True
        assert mock_order_manager.list_open_broker_orders.await_count == 2

    @pytest.mark.asyncio
    async def test_repeated_sweeps_do_not_re_page_for_the_same_order(
        self, durable_runner, ledger_session, mock_order_manager, mock_redis
    ):
        """A cancel is asynchronous at IB, so the same order can still be open
        on the next pass. Retry the cancel, but page only once."""
        from services.execution.runner import HALT_SWEEP_INTERVAL_SECONDS

        runner, ledger = durable_runner
        seed_approved_intent(ledger, recommendation_id="rec-1", ib_order_id="77")
        self._halt(ledger_session)
        mock_order_manager.list_open_broker_orders = AsyncMock(
            return_value=[self._broker_order()]
        )

        await runner.maybe_run_halt_sweep(now=1000.0)
        await runner.maybe_run_halt_sweep(
            now=1000.0 + HALT_SWEEP_INTERVAL_SECONDS + 1
        )

        assert mock_order_manager.cancel_broker_order.await_count == 2
        alerts = [
            AlertMessage.from_stream_dict(c.args[1])
            for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:alerts"
        ]
        assert [a.event_type for a in alerts] == ["halt_sweep_order_cancelled"]

    @pytest.mark.asyncio
    async def test_sweep_is_inert_without_a_ledger(
        self, runner, mock_order_manager
    ):
        """No ledger injected means no halt latch to read — same inertness as
        the KAN-12 gate, rather than failing closed on a system that has no
        halt table."""
        assert runner._halt_store is None

        assert await runner.maybe_run_halt_sweep(now=1000.0) is False
        mock_order_manager.list_open_broker_orders.assert_not_awaited()


class TestOrderStatusGuards:
    """handle_ib_order_status must tolerate late/duplicate broker statuses on an
    already-terminal intent and must never leak an open transaction (review)."""

    @pytest.mark.asyncio
    async def test_late_status_for_terminal_intent_is_ignored(self, durable_runner):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")  # -> SUBMITTED
        ledger.transition("rec-1", OrderStatus.FILLED)
        ledger.session.commit()

        # A late Cancelled arrives after the fill already terminalized it.
        await runner.handle_ib_order_status(
            {"order_id": "9", "status": "Cancelled"}
        )

        assert ledger.get("rec-1").status == OrderStatus.FILLED.value
        ledger.session.rollback()

    @pytest.mark.asyncio
    async def test_transition_failure_is_caught_and_alerts(self, durable_runner):
        runner, ledger = durable_runner
        seed_approved_intent(ledger, ib_order_id="9")  # SUBMITTED
        ledger.session.commit()

        from shared.order_ledger import InvalidOrderTransition

        with patch.object(
            runner._order_ledger,
            "transition",
            side_effect=InvalidOrderTransition("boom"),
        ):
            # Must not propagate into the fire-and-forget IB callback task.
            await runner.handle_ib_order_status(
                {"order_id": "9", "status": "Cancelled"}
            )

        alerts = [
            c for c in runner._redis.publish.call_args_list
            if c.args[0] == "stream:alerts"
        ]
        assert len(alerts) >= 1
        # Session is usable afterwards (not left mid-transaction).
        assert ledger.get("rec-1") is not None
        ledger.session.rollback()


class TestExecutionPoisonHandling:
    """A poison message in the execution loop must DLQ + ack + alert (3.3)."""

    @pytest.mark.asyncio
    async def test_poison_message_dead_lettered_acked_alerted(
        self, runner, mock_redis
    ):
        from types import SimpleNamespace

        mock_redis.read_group = AsyncMock(
            return_value=[SimpleNamespace(message_id="1-0", data={"x": "y"})]
        )
        mock_redis.send_to_dead_letter = AsyncMock()

        async def boom(_):
            raise ValueError("unparseable")

        await runner._consume_and_process(
            "stream:approved_orders", lambda d: d, boom, count=1, block_ms=0
        )

        mock_redis.send_to_dead_letter.assert_awaited_once()
        mock_redis.ack.assert_awaited_once()
        alerts = [
            c for c in mock_redis.publish.call_args_list
            if c.args[0] == "stream:alerts"
        ]
        assert len(alerts) >= 1


class TestGracefulShutdown:
    @pytest.mark.asyncio
    async def test_graceful_shutdown_cleans_up(
        self, runner, mock_redis, mock_order_manager
    ):
        """Graceful shutdown should cancel all open orders."""
        mock_order_manager.open_orders = {
            "order-001": {
                "ticker": "AAPL",
                "quantity": 50,
                "recommendation_id": "rec-001",
            },
        }

        await runner.shutdown()

        mock_order_manager.cancel_all_orders.assert_called_once()

    @pytest.mark.asyncio
    async def test_setup_creates_consumer_groups(
        self, runner, mock_redis
    ):
        """Setup should create consumer groups for subscribed streams."""
        await runner.setup()

        assert mock_redis.create_consumer_group.call_count >= 2


class TestPaperMode:
    def test_paper_mode_uses_paper_port(self, mock_config):
        """Paper mode should use the paper port."""
        mock_config.mode = "paper"
        runner = ExecutionServiceRunner(
            config=mock_config,
            redis_client=AsyncMock(),
            order_manager=AsyncMock(),
        )
        assert runner.ib_port == mock_config.ib.paper_port

    def test_live_mode_uses_live_port(self, mock_config):
        """Live mode should use the live port."""
        mock_config.mode = "live"
        runner = ExecutionServiceRunner(
            config=mock_config,
            redis_client=AsyncMock(),
            order_manager=AsyncMock(),
        )
        assert runner.ib_port == mock_config.ib.live_port


class TestPaperAccountGuard:
    """connect(expect_paper=True) must refuse a live-account Gateway session."""

    @pytest.mark.asyncio
    async def test_live_account_on_paper_port_refused(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from services.execution.ib_executor import (
            IBExecutor,
            WrongAccountTypeError,
        )

        executor = IBExecutor(host="h", port=7497, client_id=1)
        fake_ib = MagicMock()
        fake_ib.connectAsync = AsyncMock()
        fake_ib.managedAccounts.return_value = ["U17723819"]  # LIVE prefix

        with patch("ib_insync.IB", return_value=fake_ib):
            with pytest.raises(WrongAccountTypeError, match="LIVE"):
                await executor.connect(expect_paper=True)

        fake_ib.disconnect.assert_called_once()
        assert executor._ib is None

    @pytest.mark.asyncio
    async def test_paper_account_accepted(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from services.execution.ib_executor import IBExecutor

        executor = IBExecutor(host="h", port=7497, client_id=1)
        fake_ib = MagicMock()
        fake_ib.connectAsync = AsyncMock()
        fake_ib.managedAccounts.return_value = ["DUN551088"]  # paper prefix

        with patch("ib_insync.IB", return_value=fake_ib):
            await executor.connect(expect_paper=True)

        assert executor._ib is fake_ib

    @pytest.mark.asyncio
    async def test_paper_account_on_live_port_refused(self):
        """The mirror guard: live mode must refuse a paper (DU) Gateway session
        so a mis-login never trades the wrong book."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from services.execution.ib_executor import (
            IBExecutor,
            WrongAccountTypeError,
        )

        executor = IBExecutor(host="h", port=7496, client_id=1)
        fake_ib = MagicMock()
        fake_ib.connectAsync = AsyncMock()
        fake_ib.managedAccounts.return_value = ["DUN551088"]  # PAPER prefix

        with patch("ib_insync.IB", return_value=fake_ib):
            with pytest.raises(WrongAccountTypeError, match="PAPER"):
                await executor.connect(expect_paper=False)

        fake_ib.disconnect.assert_called_once()
        assert executor._ib is None

    @pytest.mark.asyncio
    async def test_live_account_accepted_in_live_mode(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from services.execution.ib_executor import IBExecutor

        executor = IBExecutor(host="h", port=7496, client_id=1)
        fake_ib = MagicMock()
        fake_ib.connectAsync = AsyncMock()
        fake_ib.managedAccounts.return_value = ["U17723819"]  # live prefix

        with patch("ib_insync.IB", return_value=fake_ib):
            await executor.connect(expect_paper=False)

        assert executor._ib is fake_ib


class TestExactAccountPin:
    """A DU prefix is not an identity. With ``account_id`` configured the
    session must serve exactly that account — a second paper account, or a
    Gateway repointed at a different one, must refuse to trade."""

    @staticmethod
    def _fake_ib(accounts):
        from unittest.mock import AsyncMock, MagicMock

        fake_ib = MagicMock()
        fake_ib.connectAsync = AsyncMock()
        fake_ib.managedAccounts.return_value = accounts
        fake_ib.isConnected.return_value = True
        return fake_ib

    def _executor(self, account_id="DUN551088"):
        from services.execution.ib_executor import IBExecutor

        return IBExecutor(
            host="h", port=7497, client_id=1, account_id=account_id
        )

    @pytest.mark.asyncio
    async def test_other_paper_account_refused_despite_matching_prefix(self):
        """The test that fails without the pin: DU9999999 passes the prefix
        guard but is not the configured book."""
        from unittest.mock import patch

        from services.execution.ib_executor import WrongAccountTypeError

        executor = self._executor()
        fake_ib = self._fake_ib(["DU9999999"])

        with patch("ib_insync.IB", return_value=fake_ib):
            with pytest.raises(WrongAccountTypeError) as excinfo:
                await executor.connect(expect_paper=True)

        # The message must name both ids so the operator can see the swap.
        assert "DUN551088" in str(excinfo.value)
        assert "DU9999999" in str(excinfo.value)
        fake_ib.disconnect.assert_called_once()
        assert executor._ib is None

    @pytest.mark.asyncio
    async def test_configured_account_connects(self):
        from unittest.mock import patch

        executor = self._executor()
        fake_ib = self._fake_ib(["DUN551088"])

        with patch("ib_insync.IB", return_value=fake_ib):
            await executor.connect(expect_paper=True)

        assert executor._ib is fake_ib

    @pytest.mark.asyncio
    async def test_ambiguous_multi_account_session_refused(self):
        """Configured account present but not alone: an ambiguous session is
        how orders reach the wrong book, so reject it — the same
        exactly-one-account rule IBAccountReader.snapshot() enforces."""
        from unittest.mock import patch

        from services.execution.ib_executor import WrongAccountTypeError

        executor = self._executor()
        fake_ib = self._fake_ib(["DUN551088", "DU9999999"])

        with patch("ib_insync.IB", return_value=fake_ib):
            with pytest.raises(WrongAccountTypeError) as excinfo:
                await executor.connect(expect_paper=True)

        assert "DUN551088" in str(excinfo.value)
        assert "DU9999999" in str(excinfo.value)
        fake_ib.disconnect.assert_called_once()
        assert executor._ib is None

    @pytest.mark.asyncio
    async def test_unset_account_id_keeps_todays_behaviour(self):
        """Regression: with no pin configured, any DU account still connects."""
        from unittest.mock import patch

        executor = self._executor(account_id=None)
        fake_ib = self._fake_ib(["DU9999999", "DU1111111"])

        with patch("ib_insync.IB", return_value=fake_ib):
            await executor.connect(expect_paper=True)

        assert executor._ib is fake_ib

    @pytest.mark.asyncio
    async def test_pin_survives_reconnect(self):
        """The auto-reconnect path re-runs connect(); a Gateway that comes back
        serving a different account must not be trusted."""
        from unittest.mock import patch

        from services.execution.ib_executor import WrongAccountTypeError

        executor = self._executor()
        with patch("ib_insync.IB", return_value=self._fake_ib(["DUN551088"])):
            await executor.connect(expect_paper=True)

        executor._ib.isConnected.return_value = False  # session dropped
        with patch("ib_insync.IB", return_value=self._fake_ib(["DU9999999"])):
            with pytest.raises(WrongAccountTypeError, match="DU9999999"):
                await executor.submit_limit_order("CSCO", 17.0, 121.31)

    async def _place(self, account_id):
        from unittest.mock import MagicMock, patch

        executor = self._executor(account_id=account_id)
        fake_ib = self._fake_ib(["DUN551088"])
        trade = MagicMock()
        trade.order.orderId = 7
        fake_ib.placeOrder.return_value = trade

        with patch("ib_insync.IB", return_value=fake_ib):
            await executor.connect(expect_paper=True)
            await executor.submit_limit_order(
                "AAPL", 5, 150.0, recommendation_id="rec-1"
            )
            await executor.submit_market_order(
                "AAPL", 5, recommendation_id="rec-2"
            )

        return [call.args[1] for call in fake_ib.placeOrder.call_args_list]

    @pytest.mark.asyncio
    async def test_orders_are_stamped_with_the_configured_account(self):
        limit_order, market_order = await self._place("DUN551088")

        assert limit_order.account == "DUN551088"
        assert market_order.account == "DUN551088"

    @pytest.mark.asyncio
    async def test_orders_are_unstamped_when_no_account_configured(self):
        limit_order, market_order = await self._place(None)

        # ib_insync's default: the Gateway picks the session's account.
        assert limit_order.account == ""
        assert market_order.account == ""


class TestWholeShareFallback:
    """Accounts without fractional API support round down; sub-1-share skips."""

    def _executor(self, allow_fractional=False):
        from services.execution.ib_executor import IBExecutor

        return IBExecutor(host="h", port=7497, client_id=1,
                          allow_fractional=allow_fractional)

    def test_fractional_rounds_down(self):
        ex = self._executor()
        assert ex._effective_quantity("PM", 8.3243) == 8.0

    def test_whole_quantity_untouched(self):
        ex = self._executor()
        assert ex._effective_quantity("PM", 8.0) == 8.0

    def test_sub_one_share_raises_skip(self):
        from services.execution.ib_executor import OrderSkippedError

        ex = self._executor()
        with pytest.raises(OrderSkippedError, match="rounds to zero"):
            ex._effective_quantity("ISRG", 0.4)

    def test_fractional_allowed_passthrough(self):
        ex = self._executor(allow_fractional=True)
        assert ex._effective_quantity("PM", 8.3243) == 8.3243

    @pytest.mark.asyncio
    async def test_runner_treats_skip_as_nonfailure(self, runner, mock_redis, mock_order_manager):
        """A skipped order acks cleanly: no pending entry, no exception."""
        from services.execution.ib_executor import OrderSkippedError

        mock_order_manager.submit_entry.side_effect = OrderSkippedError("too small")
        order = make_approved_order(ticker="ISRG", action="buy", limit_price=432.83)

        await runner.process_approved_order(order)  # must not raise

        assert order not in runner._pending_orders.values()
        mock_redis.publish.assert_not_called()


class TestAutoReconnect:
    """A dropped Gateway session must reconnect on demand, not fail orders.

    Regression: overnight 2026-07-10/11 the Gateway dropped the socket
    ("Peer closed connection") and the sole Saturday order (CSCO) failed
    with NotConnectedError because the executor never reconnected.
    """

    async def _connected_paper_executor(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from services.execution.ib_executor import IBExecutor

        executor = IBExecutor(host="h", port=7497, client_id=1)
        fake_ib = MagicMock()
        fake_ib.connectAsync = AsyncMock()
        fake_ib.managedAccounts.return_value = ["DUN551088"]
        fake_ib.isConnected.return_value = True
        with patch("ib_insync.IB", return_value=fake_ib):
            await executor.connect(expect_paper=True)
        return executor, fake_ib

    @pytest.mark.asyncio
    async def test_submit_reconnects_after_drop(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        executor, fake_ib = await self._connected_paper_executor()
        fake_ib.isConnected.return_value = False  # Gateway dropped us

        fresh_ib = MagicMock()
        fresh_ib.connectAsync = AsyncMock()
        fresh_ib.managedAccounts.return_value = ["DUN551088"]
        fresh_ib.isConnected.return_value = True
        trade = MagicMock()
        trade.order.orderId = 7
        fresh_ib.placeOrder.return_value = trade

        with patch("ib_insync.IB", return_value=fresh_ib):
            order_id = await executor.submit_limit_order("CSCO", 17.0, 121.31)

        assert order_id == "7"
        fresh_ib.connectAsync.assert_awaited_once()
        fresh_ib.placeOrder.assert_called_once()

    @pytest.mark.asyncio
    async def test_reconnect_failure_raises_not_connected(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from services.execution.ib_executor import NotConnectedError

        executor, fake_ib = await self._connected_paper_executor()
        fake_ib.isConnected.return_value = False

        dead_ib = MagicMock()
        dead_ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("down"))

        with patch("ib_insync.IB", return_value=dead_ib):
            with pytest.raises(NotConnectedError, match="reconnect failed"):
                await executor.submit_limit_order("CSCO", 17.0, 121.31)

    @pytest.mark.asyncio
    async def test_reconnect_reapplies_paper_guard(self):
        """A reconnect must re-check the account type — the 2026-07-04
        live-account-on-paper-port scenario applies to reconnects too."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from services.execution.ib_executor import WrongAccountTypeError

        executor, fake_ib = await self._connected_paper_executor()
        fake_ib.isConnected.return_value = False

        live_ib = MagicMock()
        live_ib.connectAsync = AsyncMock()
        live_ib.managedAccounts.return_value = ["U17723819"]  # LIVE
        live_ib.isConnected.return_value = True

        with patch("ib_insync.IB", return_value=live_ib):
            with pytest.raises(WrongAccountTypeError, match="LIVE"):
                await executor.submit_limit_order("CSCO", 17.0, 121.31)

        live_ib.placeOrder.assert_not_called()


class TestRecurringExitsCrossToExecution:
    """KAN-7 AC 1: the P0 end to end.

    Stop-loss and passive-trim sells used to publish a uuid4 recommendation_id
    with no backing intent. Execution's first act on an approved order is a
    ledger lookup, so every one of them raised OrderIntentNotFound and landed in
    stream:approved_orders:dlq — silently, every scan, for as long as the breach
    lasted. Driving the real consume path here is what makes the no-DLQ half of
    the assertion mean anything.
    """

    @staticmethod
    def _risk_runner(ledger, ledger_session):
        from unittest.mock import MagicMock as _MagicMock

        from services.risk_management.runner import RiskServiceRunner
        from shared.config import CurrencyConfig, RiskConfig

        config = _MagicMock(spec=AppConfig)
        config.mode = "paper"
        config.risk = RiskConfig()
        config.currency = CurrencyConfig()
        redis = AsyncMock()
        redis.publish = AsyncMock(return_value="msg-1")
        redis.stream_length = AsyncMock(return_value=0)
        runner = RiskServiceRunner(
            config=config,
            redis_client=redis,
            db_session=ledger_session,
            order_ledger=ledger,
        )
        return runner, redis

    @staticmethod
    def _hold(ledger_session, *, quantity: float = 10.0) -> None:
        ledger_session.add(Position(
            account_id="DUN551088",
            ticker="AAPL",
            portfolio="momentum",
            con_id=265598,
            exchange="SMART",
            currency="USD",
            quantity=quantity,
            avg_entry_price=100,
            current_price=100,
            peak_price=100,
            highest_price_since_entry=100,
            opened_at=BROKER_TIME,
            status="open",
        ))
        ledger_session.commit()

    @staticmethod
    def _published(risk_redis) -> list[ApprovedOrderMessage]:
        return [
            ApprovedOrderMessage.from_stream_dict(c.args[1])
            for c in risk_redis.publish.call_args_list
            if c.args[0] == "stream:approved_orders"
        ]

    async def _consume(
        self, execution_runner, mock_redis, order: ApprovedOrderMessage
    ) -> None:
        """Feed the exit through the real consumer, DLQ path armed."""
        mock_redis.read_group = AsyncMock(return_value=[
            SimpleNamespace(message_id="1-1", data=order.to_stream_dict())
        ])
        mock_redis.send_to_dead_letter = AsyncMock()
        await execution_runner._consume_and_process(
            "stream:approved_orders",
            ApprovedOrderMessage.from_stream_dict,
            execution_runner.process_approved_order,
            count=10,
            block_ms=0,
        )

    @pytest.mark.asyncio
    async def test_stop_loss_exit_submits_to_ib_and_never_dead_letters(
        self, mock_config, mock_redis, mock_order_manager, ledger_session
    ):
        self._hold(ledger_session)
        ledger = OrderLedger(ledger_session)
        risk_runner, risk_redis = self._risk_runner(ledger, ledger_session)
        risk_runner._portfolio.positions = {
            "AAPL": {"quantity": 10, "highest_price_since_entry": 100.0}
        }
        risk_runner._current_prices = {"AAPL": 84.0}

        await risk_runner.run_stop_loss_check()
        ledger_session.rollback()

        (exit_order,) = self._published(risk_redis)
        execution_runner = ExecutionServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            order_manager=mock_order_manager,
            order_ledger=ledger,
        )
        await self._consume(execution_runner, mock_redis, exit_order)

        mock_order_manager.submit_exit.assert_awaited_once_with(
            ticker="AAPL",
            quantity=10.0,
            recommendation_id=exit_order.recommendation_id,
        )
        mock_redis.send_to_dead_letter.assert_not_awaited()
        assert ledger.get(exit_order.recommendation_id).status == (
            OrderStatus.SUBMITTED.value
        )
        ledger_session.rollback()

    @pytest.mark.asyncio
    async def test_passive_trim_exit_submits_the_partial_quantity(
        self, mock_config, mock_redis, mock_order_manager, ledger_session
    ):
        """A trim is a partial sell: the intent, the published order and the IB
        submission must all carry the same partial quantity, or the ledger and
        the broker disagree about what is outstanding."""
        self._hold(ledger_session, quantity=40.0)
        ledger = OrderLedger(ledger_session)
        risk_runner, risk_redis = self._risk_runner(ledger, ledger_session)
        risk_runner._portfolio.nav = 10_000
        risk_runner._current_prices = {"AAPL": 100.0}

        await risk_runner._trim_position_to_target(SimpleNamespace(
            ticker="AAPL",
            target_pct=10.0,
            current_pct=40.0,
            message="hard ceiling breach",
        ))
        ledger_session.rollback()

        (exit_order,) = self._published(risk_redis)
        assert exit_order.quantity == pytest.approx(30.0)
        execution_runner = ExecutionServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            order_manager=mock_order_manager,
            order_ledger=ledger,
        )
        await self._consume(execution_runner, mock_redis, exit_order)

        mock_order_manager.submit_exit.assert_awaited_once_with(
            ticker="AAPL",
            quantity=30.0,
            recommendation_id=exit_order.recommendation_id,
        )
        mock_redis.send_to_dead_letter.assert_not_awaited()
        ledger_session.rollback()
