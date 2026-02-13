from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from shared.logging import get_logger
from shared.redis_client import RedisStreamClient
from shared.schemas.messages import FundamentalMessage
from services.data_ingestion.ib_client import IBClientProtocol

logger = get_logger("fundamentals_pipeline")


class FundamentalsPipeline:
    def __init__(
        self,
        ib_client: IBClientProtocol,
        redis_client: RedisStreamClient,
        db_session: Any,
    ):
        self._ib = ib_client
        self._redis = redis_client
        self._db = db_session

    async def ingest(self, ticker: str) -> None:
        """Fetch fundamentals from IB and publish to Redis stream.

        1. Fetch fundamentals from IB
        2. Create FundamentalMessage with point-in-time fields
        3. Publish to stream:fundamentals
        """
        logger.info("fetching_fundamentals", ticker=ticker)

        fundamentals = await self._ib.get_fundamentals(ticker)

        now = datetime.now(timezone.utc)
        msg = FundamentalMessage(
            ticker=ticker,
            timestamp=now,
            metric_type="snapshot",
            data=fundamentals,
            effective_at=now,
            ingested_at=now,
            source_revision="ib-snapshot",
        )

        await self._redis.publish("stream:fundamentals", msg.to_stream_dict())

        logger.info("published_fundamentals", ticker=ticker)
