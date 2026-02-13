from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from shared.config import AppConfig
from shared.logging import get_logger
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
    ) -> None:
        self._config = config
        self._redis = redis_client
        self._order_manager = order_manager
        self._logger = get_logger("execution_service")
        self._running = False

        # Positions tracked locally (in production loaded from DB)
        self._positions: dict[str, int] = {}

        # Determine IB port based on mode
        if config.mode == "live":
            self.ib_port = config.ib.live_port
        else:
            self.ib_port = config.ib.paper_port

    async def setup(self) -> None:
        """Create consumer groups for subscribed streams."""
        await self._redis.create_consumer_group(
            APPROVED_ORDERS_STREAM, CONSUMER_GROUP
        )
        await self._redis.create_consumer_group(KILLS_STREAM, CONSUMER_GROUP)
        self._logger.info("Execution service consumer groups created")

    async def process_approved_order(
        self, order: ApprovedOrderMessage
    ) -> None:
        """Process a single approved order.

        For buy orders: submit a limit entry.
        For sell orders: submit a market exit.
        Then publish a fill message to ``stream:fills``.

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

        # Publish fill message
        fill = FillMessage(
            ticker=order.ticker,
            timestamp=datetime.now(timezone.utc),
            side=order.action,
            quantity=order.quantity,
            fill_price=order.limit_price or 0.0,
            commission=0.0,
            recommendation_id=order.recommendation_id,
            order_id=order_id,
        )

        await self._redis.publish(FILLS_STREAM, fill.to_stream_dict())

        self._logger.info(
            "Fill published",
            order_id=order_id,
            ticker=order.ticker,
            action=order.action,
        )

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
