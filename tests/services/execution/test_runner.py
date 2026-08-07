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
        action="BUY",
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
