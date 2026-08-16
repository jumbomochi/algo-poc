from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.notifications.dispatcher import NotificationDispatcher
from shared.config import AppConfig
from shared.heartbeat import write_heartbeat
from shared.logging import get_logger
from shared.models.alerts import AlertRecord
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
        db_session: Session | None = None,
    ) -> None:
        self._config = config
        self._redis = redis_client
        self._dispatcher = dispatcher
        self._db = db_session
        self._logger = logger
        self._running = False
        if db_session is None:
            self._logger.warning(
                "No database session configured; alerts will be delivered but "
                "not recorded, and the go-live reliability gate will report "
                "its evidence as unavailable"
            )

    async def setup(self) -> None:
        """Create consumer group and replay pending alerts.

        Alerts delivered but unacked before a crash would otherwise never be
        re-delivered — a lost critical alert is exactly the failure this
        service exists to prevent.
        """
        await self._redis.create_consumer_group(ALERTS_STREAM, CONSUMER_GROUP)
        pending = await self._redis.drain_pending(
            ALERTS_STREAM, CONSUMER_GROUP, CONSUMER_NAME
        )
        for msg in pending:
            # process_message handles its own ack / dead-lettering.
            await self.process_message(msg)
        if pending:
            self._logger.warning(
                "Replayed pending alerts from a prior crash", count=len(pending)
            )
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
            # Recorded before dispatch: an alert no channel could deliver is
            # exactly the one the reliability gate must be able to see.
            self._record(alert, message.message_id)
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
            # Ack after dead-lettering, or the original leaks in the PEL and is
            # re-drained + re-DLQ'd on every restart (finding 3.4).
            await self._redis.ack(
                ALERTS_STREAM, CONSUMER_GROUP, message.message_id
            )

    def _record(self, alert: AlertMessage, message_id: str) -> None:
        """Persist one alert as durable evidence for the go-live gate.

        The database is deliberately NOT on the delivery path: a write failure
        is logged and swallowed so a sick database can never stop a critical
        alert from reaching a human.  Re-delivery of the same stream id is a
        replay of one incident, not a second one, so it is skipped.
        """
        if self._db is None:
            return
        try:
            already = self._db.scalar(
                select(AlertRecord.id).where(AlertRecord.message_id == message_id)
            )
            if already is not None:
                # The SELECT autobegan a transaction. Returning without ending
                # it would leave this long-lived session idle-in-transaction,
                # holding a snapshot open until the next alert arrives.
                self._db.rollback()
                return
            self._db.add(
                AlertRecord(
                    message_id=message_id,
                    event_type=alert.event_type,
                    priority=alert.priority,
                    message=alert.message,
                    context=alert.context,
                    raised_at=alert.timestamp,
                    recorded_at=datetime.now(timezone.utc),
                )
            )
            self._db.commit()
        except Exception:
            # Best-effort: a dead connection makes rollback() raise too, and an
            # exception escaping here would dead-letter the alert *undelivered*
            # — the exact opposite of this method's contract, happening exactly
            # when the database is sick.
            try:
                self._db.rollback()
            except Exception:
                self._logger.exception("Alert-record rollback failed")
            self._logger.exception(
                "Failed to record alert; delivery continues",
                message_id=message_id,
                event_type=alert.event_type,
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
                # T6: heartbeat for the container healthcheck — see docker-compose.yml.
                write_heartbeat()
        except (KeyboardInterrupt, Exception):
            self._logger.info("Notifications service interrupted")
        finally:
            self._running = False
            self._logger.info("Notifications service stopped")


if __name__ == "__main__":
    import asyncio

    from shared.config import load_config

    config = load_config("config/default.yaml")

    async def main() -> None:
        import redis.asyncio as aioredis
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from services.notifications.channels import (
            EmailChannel,
            SlackChannel,
            SMSChannel,
            TelegramChannel,
        )
        from services.notifications.dispatcher import NotificationDispatcher
        from shared.heartbeat import register_heartbeat_collector
        from shared.observability import setup_metrics
        from shared.redis_client import RedisStreamClient

        setup_metrics("notifications", port=config.observability.prometheus_port)
        register_heartbeat_collector()

        redis_conn = aioredis.from_url(config.redis.url)
        redis_client = RedisStreamClient(redis_conn)

        # Only construct channels that are enabled in config; disabled ones are
        # None and the dispatcher skips them.
        telegram = None
        if config.notifications.telegram_enabled:
            telegram = TelegramChannel()
            if not telegram.is_configured:
                logger.warning(
                    "telegram_enabled but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID "
                    "not set; Telegram alerts will fail"
                )
        dispatcher = NotificationDispatcher(
            slack=SlackChannel() if config.notifications.slack_enabled else None,
            email=EmailChannel() if config.notifications.email_enabled else None,
            sms=SMSChannel() if config.notifications.sms_enabled else None,
            telegram=telegram,
        )
        # Alerts are recorded as durable evidence for the go-live reliability
        # gate; the write is off the delivery path, so a DB outage degrades the
        # evidence rather than the alerting.
        engine = create_engine(config.database.url)
        db_session = sessionmaker(bind=engine)()
        runner = NotificationsServiceRunner(
            config=config,
            redis_client=redis_client,
            dispatcher=dispatcher,
            db_session=db_session,
        )
        try:
            await runner.run()
        finally:
            db_session.close()

    asyncio.run(main())
