from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from services.notifications.dispatcher import NotificationDispatcher
from shared.config import AppConfig
from shared.logging import get_logger
from shared.redis_client import RedisStreamClient, StreamMessage
from shared.schemas.messages import AlertMessage

ALERTS_STREAM = "stream:alerts"
CONSUMER_GROUP = "notifications_service"
CONSUMER_NAME = "notifications_worker_1"

logger = get_logger("notifications_service")


class NotificationsServiceRunner:
    """Orchestrates the Notifications Service.

    Subscribes to ``stream:alerts`` via a Redis consumer group for
    at-least-once delivery.  Each alert message is routed through the
    :class:`NotificationDispatcher` which fans out to Slack, Email, and
    SMS channels based on the alert priority.
    """

    def __init__(
        self,
        config: AppConfig,
        redis_client: RedisStreamClient,
        dispatcher: NotificationDispatcher,
    ) -> None:
        self._config = config
        self._redis = redis_client
        self._dispatcher = dispatcher
        self._logger = logger
        self._running = False

    async def setup(self) -> None:
        """Create consumer group for the alerts stream."""
        await self._redis.create_consumer_group(ALERTS_STREAM, CONSUMER_GROUP)
        self._logger.info("Notifications service consumer group created")

    async def process_message(self, message: StreamMessage) -> None:
        """Deserialise an alert and dispatch it to the appropriate channels.

        On success the message is acknowledged.  On failure it is forwarded
        to the dead-letter queue so it can be retried or inspected later.

        Args:
            message: The raw stream message from Redis.
        """
        try:
            alert = AlertMessage.from_stream_dict(message.data)
            await self._dispatcher.dispatch(alert)
            await self._redis.ack(ALERTS_STREAM, CONSUMER_GROUP, message.message_id)
            self._logger.info(
                "Alert dispatched",
                event_type=alert.event_type,
                priority=alert.priority,
                message_id=message.message_id,
            )
        except Exception as exc:
            self._logger.exception(
                "Error processing alert message",
                message_id=message.message_id,
            )
            await self._redis.send_to_dead_letter(
                ALERTS_STREAM, message, str(exc)
            )

    async def health_check(self) -> dict[str, Any]:
        """Return health status for the notifications service.

        Returns:
            A dict with ``status`` and ``service`` keys.
        """
        return {
            "status": "ok",
            "service": "notifications_service",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def run(self) -> None:
        """Main event loop: read alerts and dispatch notifications.

        Runs until ``self._running`` is set to ``False`` or an interrupt
        is received.
        """
        await self.setup()
        self._running = True

        self._logger.info("Notifications service started")

        try:
            while self._running:
                messages = await self._redis.read_group(
                    ALERTS_STREAM,
                    CONSUMER_GROUP,
                    CONSUMER_NAME,
                    count=10,
                    block_ms=2000,
                )

                for msg in messages:
                    await self.process_message(msg)
        except (KeyboardInterrupt, Exception):
            self._logger.info("Notifications service interrupted")
        finally:
            self._running = False
            self._logger.info("Notifications service stopped")
