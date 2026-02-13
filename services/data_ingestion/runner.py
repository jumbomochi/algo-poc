from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from shared.config import AppConfig
from shared.logging import get_logger
from shared.market_calendar import MarketCalendar
from shared.redis_client import RedisStreamClient
from services.data_ingestion.ib_client import IBClientProtocol
from services.data_ingestion.market_data import MarketDataPipeline
from services.data_ingestion.fundamentals import FundamentalsPipeline
from services.data_ingestion.events import EventsPipeline, EventsSourceProtocol

logger = get_logger("data_ingestion_runner")


class DataIngestionRunner:
    """Service entrypoint that orchestrates all three data ingestion pipelines.

    Coordinates MarketDataPipeline, FundamentalsPipeline, and EventsPipeline
    to ingest data for a list of tickers. Uses MarketCalendar to determine
    whether the market is active. Supports graceful shutdown.
    """

    def __init__(
        self,
        config: AppConfig,
        ib_client: IBClientProtocol,
        redis_client: RedisStreamClient,
        db_session: Any,
        events_source: EventsSourceProtocol | None = None,
    ):
        self._config = config
        self._calendar = MarketCalendar()
        self._running = True

        self._market_data = MarketDataPipeline(
            ib_client=ib_client,
            redis_client=redis_client,
            db_session=db_session,
            rate_limit_per_sec=config.data_ingestion.ib_rate_limit_per_sec,
        )
        self._fundamentals = FundamentalsPipeline(
            ib_client=ib_client,
            redis_client=redis_client,
            db_session=db_session,
        )

        # Use provided events_source or a stub if none given
        if events_source is None:
            events_source = _StubEventsSource()

        self._events = EventsPipeline(
            events_source=events_source,
            redis_client=redis_client,
            db_session=db_session,
        )

    async def run_cycle(self, tickers: list[str]) -> None:
        """Run all three pipelines for each ticker.

        Processes market data, fundamentals, and events for every ticker.
        Errors for individual tickers are logged but do not stop processing
        of remaining tickers.
        """
        if not tickers:
            logger.info("run_cycle_skipped", reason="no_tickers")
            return

        logger.info("run_cycle_start", ticker_count=len(tickers))

        now = datetime.now(timezone.utc)
        today = now.date()
        yesterday = date.fromordinal(today.toordinal() - 1)

        for ticker in tickers:
            if not self._running:
                logger.info("run_cycle_interrupted", reason="shutdown_requested")
                break

            # Market data
            try:
                await self._market_data.ingest(ticker, yesterday, today)
            except Exception:
                logger.exception("market_data_error", ticker=ticker)

            # Fundamentals
            try:
                await self._fundamentals.ingest(ticker)
            except Exception:
                logger.exception("fundamentals_error", ticker=ticker)

            # Events
            try:
                await self._events.ingest(ticker)
            except Exception:
                logger.exception("events_error", ticker=ticker)

        logger.info("run_cycle_complete", ticker_count=len(tickers))

    def is_market_active(self) -> bool:
        """Check if the market is currently open using MarketCalendar."""
        now = datetime.now(timezone.utc)
        return self._calendar.is_market_open(now)

    async def shutdown(self) -> None:
        """Signal a graceful shutdown of the runner."""
        logger.info("shutdown_requested")
        self._running = False


class _StubEventsSource:
    """Default stub events source when no real source is configured."""

    async def get_events(self, ticker: str) -> list[dict[str, Any]]:
        return []
