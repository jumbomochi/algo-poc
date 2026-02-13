from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from shared.logging import get_logger
from shared.redis_client import RedisStreamClient
from shared.schemas.messages import EventMessage

logger = get_logger("events_pipeline")


class EventsSourceProtocol(Protocol):
    """Protocol for event data sources (news APIs, etc.)."""

    async def get_events(self, ticker: str) -> list[dict[str, Any]]: ...


class EventsPipeline:
    def __init__(
        self,
        events_source: EventsSourceProtocol,
        redis_client: RedisStreamClient,
        db_session: Any,
    ):
        self._source = events_source
        self._redis = redis_client
        self._db = db_session

    async def ingest(self, ticker: str) -> None:
        """Fetch events from source and publish to Redis stream.

        1. Fetch events from source
        2. Create EventMessage with point-in-time fields for each event
        3. Publish to stream:events
        """
        logger.info("fetching_events", ticker=ticker)

        events = await self._source.get_events(ticker)

        if not events:
            logger.info("no_events_found", ticker=ticker)
            return

        now = datetime.now(timezone.utc)

        for event in events:
            msg = EventMessage(
                ticker=ticker,
                timestamp=now,
                event_type=event.get("event_type", "unknown"),
                data=event,
                sentiment_score=event.get("sentiment_score"),
                effective_at=now,
                ingested_at=now,
                source_revision=event.get("source_revision", "external-api"),
            )

            await self._redis.publish("stream:events", msg.to_stream_dict())

        logger.info("published_events", ticker=ticker, count=len(events))
