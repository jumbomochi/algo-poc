from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from shared.config import AppConfig
from shared.logging import get_logger
from shared.models import OrderStatus
from shared.order_ledger import OrderLedger, TERMINAL_STATUSES
from shared.schemas.messages import (
    AlertMessage,
    ApprovedOrderMessage,
    FillMessage,
    KillMessage,
)

APPROVED_ORDERS_STREAM = "stream:approved_orders"
KILLS_STREAM = "stream:kill"
FILLS_STREAM = "stream:fills"
ALERTS_STREAM = "stream:alerts"

CONSUMER_GROUP = "execution_service"
CONSUMER_NAME = "execution_worker_1"


@dataclass(frozen=True)
class PendingOrderAttribution:
    recommendation_id: str
    portfolio: str | None


class ExecutionServiceRunner:
    """Orchestrates the Execution Service.

    Subscribes to ``stream:approved_orders`` and ``stream:kill``.
    Submits orders via :class:`OrderManager`, publishes fills to
    ``stream:fills``, and handles kill events with full liquidation.

    Paper mode toggle via ``config.mode`` — selects the appropriate
    IB port (paper vs live).
    """

    def __init__(
        self,
        config: AppConfig,
        redis_client: Any,
        order_manager: Any,
        order_ledger: OrderLedger | None = None,
    ) -> None:
        self._config = config
        self._redis = redis_client
        self._order_manager = order_manager
        self._order_ledger = order_ledger
        self._logger = get_logger("execution_service")
        self._running = False

        # Positions tracked locally (in production loaded from DB)
        self._positions: dict[str, int] = {}

        # order_id -> ApprovedOrderMessage, so IB fills can be attributed
        # back to the originating recommendation.
        self._pending_orders: dict[
            str, ApprovedOrderMessage | PendingOrderAttribution
        ] = {}

        # Determine IB port based on mode
        if config.mode == "live":
            self.ib_port = config.ib.live_port
        else:
            self.ib_port = config.ib.paper_port

    async def setup(self) -> None:
        """Create consumer groups and replay pending messages.

        Messages delivered but not acked before a crash sit in the pending
        entries list and are never re-delivered by the normal ``">"`` read —
        without this replay, an approved order in flight during a restart is
        silently lost.
        """
        self.restore_pending_orders()
        restore_broker = getattr(
            type(self._order_manager), "restore_broker_tracking", None
        )
        if restore_broker is not None:
            await restore_broker(self._order_manager)

        await self._redis.create_consumer_group(
            APPROVED_ORDERS_STREAM, CONSUMER_GROUP
        )
        await self._redis.create_consumer_group(KILLS_STREAM, CONSUMER_GROUP)

        pending_orders = await self._redis.drain_pending(
            APPROVED_ORDERS_STREAM, CONSUMER_GROUP, CONSUMER_NAME
        )
        for msg in pending_orders:
            try:
                order = ApprovedOrderMessage.from_stream_dict(msg.data)
                await self.process_approved_order(order)
                await self._redis.ack(
                    APPROVED_ORDERS_STREAM, CONSUMER_GROUP, msg.message_id
                )
            except Exception as exc:
                self._logger.exception(
                    "Error replaying pending order; sending to DLQ",
                    message_id=msg.message_id,
                )
                await self._redis.send_to_dead_letter(
                    APPROVED_ORDERS_STREAM, msg, str(exc)
                )
                await self._redis.ack(
                    APPROVED_ORDERS_STREAM, CONSUMER_GROUP, msg.message_id
                )

        pending_kills = await self._redis.drain_pending(
            KILLS_STREAM, CONSUMER_GROUP, CONSUMER_NAME
        )
        for msg in pending_kills:
            try:
                kill_msg = KillMessage.from_stream_dict(msg.data)
                await self.process_kill(kill_msg)
                await self._redis.ack(KILLS_STREAM, CONSUMER_GROUP, msg.message_id)
            except Exception as exc:
                self._logger.exception(
                    "Error replaying pending kill; sending to DLQ",
                    message_id=msg.message_id,
                )
                await self._redis.send_to_dead_letter(KILLS_STREAM, msg, str(exc))
                await self._redis.ack(KILLS_STREAM, CONSUMER_GROUP, msg.message_id)

        if pending_orders or pending_kills:
            self._logger.warning(
                "Replayed pending messages from a prior crash",
                orders=len(pending_orders),
                kills=len(pending_kills),
            )
        self._logger.info("Execution service consumer groups created")

    def restore_pending_orders(self) -> None:
        """Rebuild execution attribution and idempotency from PostgreSQL."""
        if self._order_ledger is None:
            return
        for intent in self._order_ledger.load_pending_orders():
            if intent.ib_order_id is None:
                continue
            order_id = str(intent.ib_order_id)
            self._pending_orders[order_id] = PendingOrderAttribution(
                recommendation_id=intent.recommendation_id,
                portfolio=intent.portfolio,
            )
            restore = getattr(
                type(self._order_manager), "restore_submission", None
            )
            if restore is not None:
                restore(
                    self._order_manager,
                    intent.recommendation_id,
                    order_id,
                    ticker=intent.symbol,
                    quantity=intent.requested_quantity,
                    limit_price=intent.limit_price,
                )
        self._order_ledger.session.rollback()

    def _commit_ledger(self) -> None:
        if self._order_ledger is not None:
            self._order_ledger.session.commit()

    def _intent_for_order(
        self, order_id: str, *, account_id: str | None = None
    ):
        if self._order_ledger is None:
            return None
        pending = self._pending_orders.get(order_id)
        if pending is not None:
            return self._order_ledger.get(pending.recommendation_id)
        intent = self._order_ledger.get_by_ib_order_id(
            order_id, account_id=account_id
        )
        if intent is not None:
            return intent
        for intent in self._order_ledger.load_pending_orders():
            if str(intent.ib_order_id) == order_id:
                self._pending_orders[order_id] = PendingOrderAttribution(
                    recommendation_id=intent.recommendation_id,
                    portfolio=intent.portfolio,
                )
                return intent
        return None

    async def process_approved_order(
        self, order: ApprovedOrderMessage
    ) -> None:
        """Process a single approved order.

        For buy orders: submit a limit entry.
        For sell orders: submit a market exit.

        A ``FillMessage`` is NOT published here — submission is not a fill.
        Fills are published by :meth:`handle_ib_fill` when IB reports actual
        executions (including partials); anything else silently corrupts
        position tracking on rejected, repriced, or partially-filled orders.

        Args:
            order: The approved order message to process.
        """
        self._logger.info(
            "Processing approved order",
            ticker=order.ticker,
            action=order.action,
            quantity=order.quantity,
            recommendation_id=order.recommendation_id,
        )

        order_id: str

        from services.execution.ib_executor import OrderSkippedError

        if self._order_ledger is not None:
            intent = self._order_ledger.get(order.recommendation_id)
            if OrderStatus(intent.status) in TERMINAL_STATUSES:
                self._order_ledger.session.rollback()
                return
            if (
                intent.status
                in {
                    OrderStatus.SUBMITTED.value,
                    OrderStatus.PARTIALLY_FILLED.value,
                }
                and intent.ib_order_id is not None
            ):
                self._pending_orders[str(intent.ib_order_id)] = (
                    PendingOrderAttribution(
                        recommendation_id=intent.recommendation_id,
                        portfolio=intent.portfolio,
                    )
                )
                self._order_ledger.session.rollback()
                return
            # `get()` starts a read transaction. Broker submission is an
            # await point and IB callbacks use this same service-owned
            # session, so end the read before yielding control.
            self._order_ledger.session.rollback()

        try:
            if order.action == "buy":
                order_id = await self._order_manager.submit_entry(
                    ticker=order.ticker,
                    quantity=order.quantity,
                    limit_price=order.limit_price,
                    recommendation_id=order.recommendation_id,
                )
            else:
                order_id = await self._order_manager.submit_exit(
                    ticker=order.ticker,
                    quantity=order.quantity,
                    recommendation_id=order.recommendation_id,
                )
        except OrderSkippedError as exc:
            # Not a failure: the order cannot be sized on this account
            # (e.g. sub-1-share on a no-fractional account). Ack and move on.
            self._logger.warning(
                "Order skipped",
                ticker=order.ticker,
                action=order.action,
                quantity=order.quantity,
                reason=str(exc),
            )
            if self._order_ledger is not None:
                self._order_ledger.transition(
                    order.recommendation_id,
                    OrderStatus.SUBMISSION_FAILED,
                    reason=str(exc),
                )
                self._commit_ledger()
            return
        except Exception as exc:
            if self._order_ledger is not None:
                self._order_ledger.transition(
                    order.recommendation_id,
                    OrderStatus.SUBMISSION_FAILED,
                    reason=str(exc),
                )
                self._commit_ledger()
                return
            raise

        broker_active = True
        if self._order_ledger is not None:
            try:
                intent = self._order_ledger.record_submission(
                    order.recommendation_id, order_id
                )
                portfolio = intent.portfolio
                self._commit_ledger()
            except Exception:
                self._order_ledger.session.rollback()
                raise
            reconcile = getattr(
                type(self._order_manager), "reconcile_submission", None
            )
            if reconcile is not None:
                broker_active = await reconcile(
                    self._order_manager,
                    order.recommendation_id,
                    order_id,
                )
        else:
            intent = order
            portfolio = order.portfolio

        # Remember the order so fills can be attributed back to the
        # recommendation that caused them.
        if self._order_ledger is None:
            self._pending_orders[order_id] = order
        elif broker_active:
            self._pending_orders[order_id] = PendingOrderAttribution(
                recommendation_id=order.recommendation_id,
                portfolio=portfolio,
            )

        self._logger.info(
            "Order submitted, awaiting fill",
            order_id=order_id,
            ticker=order.ticker,
            action=order.action,
        )

    async def handle_ib_fill(self, fill_info: dict[str, Any]) -> None:
        """Publish a ``FillMessage`` for a real IB execution.

        Registered as the executor's fill handler; invoked once per IB fill
        (partial fills produce one call each) with actual execution price,
        quantity, and commission.

        Args:
            fill_info: Payload from :class:`IBExecutor` — order_id, ticker,
                side, quantity, fill_price, commission, order_done.
        """
        order_id = fill_info["order_id"]
        pending = self._pending_orders.get(order_id)
        intent = self._intent_for_order(
            order_id, account_id=fill_info.get("account_id")
        )
        attribution = intent or pending

        fill = FillMessage(
            ticker=fill_info["ticker"],
            timestamp=datetime.now(timezone.utc),
            side=fill_info["side"],
            quantity=fill_info["quantity"],
            cumulative_quantity=fill_info.get("cumulative_quantity"),
            fill_price=fill_info["fill_price"],
            commission=fill_info.get("commission", 0.0),
            recommendation_id=(
                attribution.recommendation_id if attribution else "unknown"
            ),
            order_id=order_id,
            execution_id=fill_info.get("execution_id"),
            account_id=fill_info.get("account_id"),
            portfolio=getattr(attribution, "portfolio", None),
            con_id=fill_info.get("con_id"),
            exchange=fill_info.get("exchange"),
            currency=fill_info.get("currency"),
        )
        if self._order_ledger is not None:
            # Fill publication awaits Redis. End the read-only SQLAlchemy
            # transaction first so status callbacks never share an active
            # transaction while this coroutine is suspended.
            self._order_ledger.session.rollback()
        await self._redis.publish(FILLS_STREAM, fill.to_stream_dict())

        self._logger.info(
            "Fill published",
            order_id=order_id,
            ticker=fill_info["ticker"],
            side=fill_info["side"],
            quantity=fill_info["quantity"],
            fill_price=fill_info["fill_price"],
            order_done=fill_info.get("order_done", False),
        )

        # Keep local position view current so a kill liquidates accurately.
        ticker = fill_info["ticker"]
        delta = fill_info["quantity"]
        if fill_info["side"] == "buy":
            self._positions[ticker] = self._positions.get(ticker, 0) + delta
        else:
            remaining = self._positions.get(ticker, 0) - delta
            if remaining > 0:
                self._positions[ticker] = remaining
            else:
                self._positions.pop(ticker, None)

        # Once IB reports the order complete, it is no longer pending or open.
        if fill_info.get("order_done"):
            self._pending_orders.pop(order_id, None)
            self._order_manager.open_orders.pop(order_id, None)

    async def handle_ib_order_status(
        self, status_info: dict[str, Any]
    ) -> None:
        """Persist terminal broker statuses before returning to IB callbacks."""
        order_id = str(status_info["order_id"])
        intent = self._intent_for_order(order_id)
        if intent is None:
            self._logger.warning(
                "IB status received for unattributed order",
                order_id=order_id,
                status=status_info.get("status"),
            )
            if self._order_ledger is not None:
                self._order_ledger.session.rollback()
            return

        broker_status = str(status_info.get("status", ""))
        reason = status_info.get("reason") or None
        if (
            broker_status == "Inactive"
            and status_info.get("completed_order_confirmed") is True
            and reason is None
        ):
            reason = "IB completed order is Inactive"
        target: OrderStatus | None = None
        if broker_status in {"Cancelled", "ApiCancelled"}:
            target = OrderStatus.CANCELLED
        elif broker_status == "Inactive" and reason:
            target = (
                OrderStatus.CANCELLED
                if (
                    intent.filled_quantity > 0
                    or float(status_info.get("filled_quantity", 0.0) or 0.0) > 0
                )
                else OrderStatus.SUBMISSION_FAILED
            )
        elif (
            broker_status == "Filled"
            and status_info.get("completed_order_confirmed") is True
        ):
            target = OrderStatus.FILLED
        elif (
            broker_status == "Expired"
            and intent.filled_quantity == 0
            and status_info.get("completed_order_confirmed") is True
        ):
            target = OrderStatus.EXPIRED

        if target is None:
            self._order_ledger.session.rollback()
            return
        self._order_ledger.transition(
            intent.recommendation_id, target, reason=reason
        )
        self._commit_ledger()
        self._pending_orders.pop(order_id, None)
        self._order_manager.open_orders.pop(order_id, None)

    async def process_kill(self, kill_msg: KillMessage) -> None:
        """Process a kill event: cancel all open orders and liquidate positions.

        Args:
            kill_msg: The kill message with reason and trigger info.
        """
        self._logger.critical(
            "Kill event received — cancelling all orders and liquidating",
            reason=kill_msg.reason,
            triggered_by=kill_msg.triggered_by,
        )

        # Cancel all open orders
        await self._order_manager.cancel_all_orders()

        # Emit market sell orders for all positions
        for ticker, quantity in self._positions.items():
            if quantity <= 0:
                continue

            kill_rec_id = f"kill-{uuid.uuid4()}"
            await self._order_manager.submit_exit(
                ticker=ticker,
                quantity=quantity,
                recommendation_id=kill_rec_id,
            )

            self._logger.info(
                "Kill liquidation order submitted",
                ticker=ticker,
                quantity=quantity,
            )

        # Publish alert
        alert = AlertMessage(
            timestamp=datetime.now(timezone.utc),
            event_type="kill_switch_liquidation",
            priority="critical",
            message=f"Kill switch activated by {kill_msg.triggered_by}: {kill_msg.reason}",
            context={
                "triggered_by": kill_msg.triggered_by,
                "positions_liquidated": len(self._positions),
            },
        )
        await self._redis.publish(ALERTS_STREAM, alert.to_stream_dict())

    async def shutdown(self) -> None:
        """Graceful shutdown: cancel all open orders to avoid orphans."""
        self._logger.info("Execution service shutting down")
        self._running = False
        await self._order_manager.cancel_all_orders()
        self._logger.info("Execution service shutdown complete — no orphaned orders")

    async def run(self) -> None:
        """Main event loop: read from streams and dispatch.

        Runs until ``self._running`` is set to ``False`` or a
        ``KeyboardInterrupt`` / ``asyncio.CancelledError`` is raised.
        """
        await self.setup()
        self._running = True

        self._logger.info(
            "Execution service started",
            mode=self._config.mode,
            ib_port=self.ib_port,
        )

        try:
            while self._running:
                # Read approved orders
                messages = await self._redis.read_group(
                    APPROVED_ORDERS_STREAM,
                    CONSUMER_GROUP,
                    CONSUMER_NAME,
                    count=10,
                    block_ms=2000,
                )

                for msg in messages:
                    try:
                        order = ApprovedOrderMessage.from_stream_dict(msg.data)
                        await self.process_approved_order(order)
                        await self._redis.ack(
                            APPROVED_ORDERS_STREAM,
                            CONSUMER_GROUP,
                            msg.message_id,
                        )
                    except Exception:
                        self._logger.exception(
                            "Error processing approved order",
                            message_id=msg.message_id,
                        )

                # Read kill stream
                kill_messages = await self._redis.read_group(
                    KILLS_STREAM,
                    CONSUMER_GROUP,
                    CONSUMER_NAME,
                    count=1,
                    block_ms=500,
                )

                for msg in kill_messages:
                    try:
                        kill_msg = KillMessage.from_stream_dict(msg.data)
                        await self.process_kill(kill_msg)
                        await self._redis.ack(
                            KILLS_STREAM,
                            CONSUMER_GROUP,
                            msg.message_id,
                        )
                    except Exception:
                        self._logger.exception(
                            "Error processing kill message",
                            message_id=msg.message_id,
                        )

        except (KeyboardInterrupt, Exception):
            self._logger.info("Execution service interrupted")
        finally:
            await self.shutdown()


if __name__ == "__main__":
    import asyncio

    from shared.config import load_config

    config = load_config("config/default.yaml")

    async def main() -> None:
        import redis.asyncio as aioredis

        from services.execution.ib_executor import IBExecutor
        from services.execution.order_manager import OrderManager
        from shared.order_ledger import OrderLedger
        from shared.redis_client import RedisStreamClient

        redis_conn = aioredis.from_url(config.redis.url)
        redis_client = RedisStreamClient(redis_conn)
        executor = IBExecutor(
            host=config.ib.host,
            port=config.ib.paper_port if config.mode != "live" else config.ib.live_port,
            client_id=config.ib.client_id,
            allow_fractional=config.execution.fractional_orders,
        )
        # Connect BEFORE consuming orders. A failed connect exits nonzero so
        # the container restart policy retries; running without IB would
        # consume approved orders while executing nothing. expect_paper
        # refuses a LIVE Gateway session answering on the paper port.
        await executor.connect(expect_paper=(config.mode != "live"))

        # Load real holdings so a kill event liquidates actual positions.
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from shared.position_loader import load_open_positions

        engine = create_engine(config.database.url)
        session = sessionmaker(bind=engine)()
        order_manager = OrderManager(
            executor=executor, redis_client=redis_client, db_session=session
        )
        runner = ExecutionServiceRunner(
            config=config,
            redis_client=redis_client,
            order_manager=order_manager,
            order_ledger=OrderLedger(session),
        )
        try:
            positions = load_open_positions(session)
            runner._positions = {
                ticker: p["quantity"] for ticker, p in positions.items()
            }
            executor.set_fill_handler(runner.handle_ib_fill)
            executor.set_order_status_handler(runner.handle_ib_order_status)
            await runner.run()
        finally:
            session.close()
            await executor.disconnect()

    asyncio.run(main())
