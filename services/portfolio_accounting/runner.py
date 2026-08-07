from __future__ import annotations

import asyncio

from pydantic import ValidationError

from services.portfolio_accounting.projector import FillProjectionError, FillProjector
from shared.heartbeat import write_heartbeat
from shared.logging import get_logger
from shared.redis_client import RedisStreamClient, StreamMessage
from shared.schemas.messages import FillMessage


FILLS_STREAM = "stream:fills"
CONSUMER_GROUP = "portfolio_accounting"
CONSUMER_NAME = "portfolio_accounting_worker_1"

logger = get_logger("portfolio_accounting")


class PortfolioAccountingRunner:
    """Drain and project fills before acknowledging their Redis delivery."""

    def __init__(
        self, redis_client: RedisStreamClient, projector: FillProjector
    ) -> None:
        self._redis = redis_client
        self._projector = projector
        self._running = False

    async def setup(self) -> None:
        await self._redis.create_consumer_group(FILLS_STREAM, CONSUMER_GROUP)
        pending = await self._redis.drain_pending(
            FILLS_STREAM, CONSUMER_GROUP, CONSUMER_NAME
        )
        for message in pending:
            await self.process_message(message)

    async def process_message(self, message: StreamMessage) -> None:
        try:
            fill = FillMessage.from_stream_dict(message.data)
            self._projector.apply(fill)
        except (ValidationError, FillProjectionError) as exc:
            logger.exception(
                "Fill projection failed; sending to DLQ",
                message_id=message.message_id,
            )
            await self._redis.send_to_dead_letter(FILLS_STREAM, message, str(exc))
            await self._redis.ack(
                FILLS_STREAM, CONSUMER_GROUP, message.message_id
            )
            return

        await self._redis.ack(FILLS_STREAM, CONSUMER_GROUP, message.message_id)

    async def run(self) -> None:
        await self.setup()
        self._running = True
        while self._running:
            messages = await self._redis.read_group(
                FILLS_STREAM,
                CONSUMER_GROUP,
                CONSUMER_NAME,
                count=10,
                block_ms=2000,
            )
            for message in messages:
                await self.process_message(message)
            # T6: heartbeat for the container healthcheck — see docker-compose.yml.
            write_heartbeat()


async def main() -> None:
    import redis.asyncio as aioredis
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from shared.config import load_config
    from shared.observability import setup_metrics

    config = load_config("config/default.yaml")
    setup_metrics("portfolio-accounting", port=config.observability.prometheus_port)
    redis_connection = aioredis.from_url(config.redis.url)
    engine = create_engine(config.database.url)
    with Session(engine) as session:
        runner = PortfolioAccountingRunner(
            RedisStreamClient(redis_connection), FillProjector(session)
        )
        try:
            await runner.run()
        finally:
            await redis_connection.aclose()


if __name__ == "__main__":
    asyncio.run(main())
