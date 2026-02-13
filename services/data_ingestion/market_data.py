from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any

from shared.redis_client import RedisStreamClient
from shared.schemas.messages import MarketDataMessage
from services.data_ingestion.ib_client import IBClientProtocol


class MarketDataPipeline:
    def __init__(
        self,
        ib_client: IBClientProtocol,
        redis_client: RedisStreamClient,
        db_session: Any,
        rate_limit_per_sec: int = 45,
    ):
        self._ib = ib_client
        self._redis = redis_client
        self._db = db_session
        self._semaphore = asyncio.Semaphore(rate_limit_per_sec)

    async def fetch_daily_bars(self, ticker: str, start: date, end: date) -> list[dict[str, Any]]:
        async with self._semaphore:
            bars = await self._ib.get_daily_bars(ticker, start, end)
            return [
                {
                    "ticker": ticker,
                    "date": b["date"],
                    "open": b["open"],
                    "high": b["high"],
                    "low": b["low"],
                    "close": b["close"],
                    "volume": b["volume"],
                }
                for b in bars
            ]

    async def ingest(self, ticker: str, start: date, end: date) -> None:
        bars = await self.fetch_daily_bars(ticker, start, end)
        for bar in bars:
            msg = MarketDataMessage(
                ticker=bar["ticker"],
                timestamp=datetime.now(timezone.utc),
                open=bar["open"],
                high=bar["high"],
                low=bar["low"],
                close=bar["close"],
                volume=bar["volume"],
            )
            await self._redis.publish("stream:market_data", msg.to_stream_dict())
