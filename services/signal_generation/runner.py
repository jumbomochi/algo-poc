from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from shared.config import AppConfig
from shared.logging import get_logger
from shared.market_calendar import MarketCalendar
from shared.redis_client import RedisStreamClient
from shared.schemas.messages import SignalMessage

from services.signal_generation.base import Signal, SignalResult
from services.signal_generation.technical import (
    SupportProximitySignal,
    SupportStrengthSignal,
    SupportTrendSignal,
)
from services.signal_generation.fundamental import (
    ValuationSignal,
    QualitySignal,
    GrowthSignal,
)
from services.signal_generation.event import (
    EarningsSurpriseSignal,
    NewsSentimentSignal,
    InsiderActivitySignal,
)
from services.signal_generation.staleness import StalenessChecker

logger = get_logger("signal_generation_runner")

SIGNALS_STREAM = "stream:signals"
CONSUMER_GROUP = "signal_generation"
CONSUMER_NAME = "signal_worker_1"


class SignalGenerationRunner:
    """Orchestrates signal computation across technical, fundamental, and event
    signal groups.

    Consumes data dicts, runs the appropriate signal group, checks for
    staleness, and publishes SignalMessages to the Redis signals stream.
    """

    def __init__(
        self,
        config: AppConfig,
        redis_client: RedisStreamClient,
        db_session: Any,
        calendar: MarketCalendar | Any | None = None,
    ):
        self._config = config
        self._redis = redis_client
        self._db = db_session
        self._calendar = calendar or MarketCalendar()

        staleness_cfg = config.signals.staleness_thresholds
        self._staleness = StalenessChecker(
            calendar=self._calendar,
            grace_hours=staleness_cfg.market_data_grace_hours,
            fundamentals_days=staleness_cfg.fundamentals_days,
            events_hours=staleness_cfg.events_hours,
        )

        self._technical_signals: list[Signal] = [
            SupportProximitySignal(),
            SupportStrengthSignal(),
            SupportTrendSignal(),
        ]
        self._fundamental_signals: list[Signal] = [
            ValuationSignal(),
            QualitySignal(),
            GrowthSignal(),
        ]
        self._event_signals: list[Signal] = [
            EarningsSurpriseSignal(),
            NewsSentimentSignal(),
            InsiderActivitySignal(),
        ]

    def _stream_handlers(self) -> dict[str, Any]:
        return {
            "stream:market_data": self.process_market_data,
            "stream:fundamentals": self.process_fundamentals,
            "stream:events": self.process_events,
        }

    async def setup(self) -> None:
        """Create consumer groups and replay delivered-but-unacked inputs so a
        restart does not silently drop signal-input messages in flight."""
        for stream, handler in self._stream_handlers().items():
            await self._redis.create_consumer_group(stream, CONSUMER_GROUP)
            pending = await self._redis.drain_pending(
                stream, CONSUMER_GROUP, CONSUMER_NAME
            )
            for msg in pending:
                await self._handle(stream, handler, msg)

    async def consume_once(self, *, count: int = 10, block_ms: int = 1000) -> None:
        """Read one batch from each input stream and process it."""
        for stream, handler in self._stream_handlers().items():
            messages = await self._redis.read_group(
                stream, CONSUMER_GROUP, CONSUMER_NAME, count=count, block_ms=block_ms
            )
            for msg in messages:
                await self._handle(stream, handler, msg)

    async def _handle(self, stream: str, handler: Any, msg: Any) -> None:
        """Process one input, dead-lettering poison messages (DLQ + ack) instead
        of leaving them parked in the PEL."""
        try:
            await handler(msg.data)
            await self._redis.ack(stream, CONSUMER_GROUP, msg.message_id)
        except Exception as exc:
            logger.exception(
                "Poison signal-input message; sending to DLQ",
                stream=stream,
                message_id=msg.message_id,
            )
            try:
                await self._redis.send_to_dead_letter(stream, msg, str(exc))
                await self._redis.ack(stream, CONSUMER_GROUP, msg.message_id)
            except Exception:
                logger.exception(
                    "Failed to dead-letter poison message", stream=stream
                )

    async def process_market_data(self, data: dict[str, Any]) -> list[SignalMessage]:
        """Run all technical signals on market data and publish results.

        Args:
            data: Dict containing ticker, timestamp, and OHLCV arrays.

        Returns:
            List of SignalMessage instances produced.
        """
        ticker = data["ticker"]
        timestamp = data["timestamp"]
        now = datetime.now(timezone.utc)

        # Check staleness
        data_ts = _parse_timestamp(timestamp)
        is_stale = self._staleness.is_stale("market_data", data_ts, now)
        if is_stale:
            logger.warning(
                "stale_market_data",
                ticker=ticker,
                data_timestamp=str(timestamp),
            )

        return await self._run_signals(
            signals=self._technical_signals,
            data=data,
            ticker=ticker,
            timestamp=timestamp,
            is_stale=is_stale,
        )

    async def process_fundamentals(self, data: dict[str, Any]) -> list[SignalMessage]:
        """Run all fundamental signals and publish results.

        Args:
            data: Dict containing ticker, timestamp, and fundamental metrics.

        Returns:
            List of SignalMessage instances produced.
        """
        ticker = data["ticker"]
        timestamp = data["timestamp"]
        now = datetime.now(timezone.utc)

        data_ts = _parse_timestamp(timestamp)
        is_stale = self._staleness.is_stale("fundamentals", data_ts, now)
        if is_stale:
            logger.warning(
                "stale_fundamentals",
                ticker=ticker,
                data_timestamp=str(timestamp),
            )

        return await self._run_signals(
            signals=self._fundamental_signals,
            data=data,
            ticker=ticker,
            timestamp=timestamp,
            is_stale=is_stale,
        )

    async def process_events(self, data: dict[str, Any]) -> list[SignalMessage]:
        """Run all event signals and publish results.

        Args:
            data: Dict containing ticker, timestamp, and event data.

        Returns:
            List of SignalMessage instances produced.
        """
        ticker = data["ticker"]
        timestamp = data["timestamp"]
        now = datetime.now(timezone.utc)

        data_ts = _parse_timestamp(timestamp)
        is_stale = self._staleness.is_stale("events", data_ts, now)
        if is_stale:
            logger.warning(
                "stale_events",
                ticker=ticker,
                data_timestamp=str(timestamp),
            )

        return await self._run_signals(
            signals=self._event_signals,
            data=data,
            ticker=ticker,
            timestamp=timestamp,
            is_stale=is_stale,
        )

    async def _run_signals(
        self,
        signals: list[Signal],
        data: dict[str, Any],
        ticker: str,
        timestamp: str | datetime,
        is_stale: bool,
    ) -> list[SignalMessage]:
        """Compute a list of signals and publish each to Redis.

        If the data is stale, confidence is zeroed out to flag downstream
        consumers.
        """
        computed_at = datetime.now(timezone.utc)
        ts = _parse_timestamp(timestamp)
        results: list[SignalMessage] = []

        for signal in signals:
            try:
                result: SignalResult = signal.compute(data)
            except Exception:
                logger.exception(
                    "signal_compute_error",
                    signal=signal.name,
                    ticker=ticker,
                )
                continue

            confidence = 0.0 if is_stale else result.confidence

            msg = SignalMessage(
                ticker=ticker,
                timestamp=ts,
                signal_name=signal.name,
                signal_value=result.value,
                confidence=confidence,
                computed_at=computed_at,
            )
            results.append(msg)

            try:
                await self._redis.publish(
                    SIGNALS_STREAM,
                    msg.to_stream_dict(),
                )
            except Exception:
                logger.exception(
                    "signal_publish_error",
                    signal=signal.name,
                    ticker=ticker,
                )

        return results


def _parse_timestamp(ts: str | datetime) -> datetime:
    """Parse a timestamp string or return the datetime as-is."""
    if isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts)


if __name__ == "__main__":
    import asyncio

    from shared.config import load_config

    config = load_config("config/default.yaml")

    async def main() -> None:
        import redis.asyncio as aioredis

        from shared.redis_client import RedisStreamClient

        redis_conn = aioredis.from_url(config.redis.url)
        redis_client = RedisStreamClient(redis_conn)
        runner = SignalGenerationRunner(
            config=config, redis_client=redis_client, db_session=None
        )

        await runner.setup()  # create groups + replay pending inputs

        logger.info("Signal generation service started", mode=config.mode)

        while True:
            await runner.consume_once(count=10, block_ms=1000)

    asyncio.run(main())
