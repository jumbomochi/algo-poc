from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class CaptureHealth:
    """How much of the capture universe actually landed in ``ohlcv_daily``.

    KAN-58. A capture that quietly writes 400 of 503 names produces a series
    with holes that looks complete years from now, when the missing bars can
    no longer be fetched. The count is what makes a partial session visible on
    the day it happens rather than at the point of use.
    """

    tickers_expected: int
    tickers_written: int

    @property
    def shortfall(self) -> int:
        return self.tickers_expected - self.tickers_written


class DataIngestionRunner:
    """Service entrypoint that orchestrates all three data ingestion pipelines.

    Coordinates MarketDataPipeline, FundamentalsPipeline, and EventsPipeline
    to ingest data for a list of tickers. Uses MarketCalendar to determine
    whether the market is active. Supports graceful shutdown.

    ``capture_universe`` is the wider set whose daily bars are *kept* (KAN-58):
    every name in it is fetched and persisted, but only the trading watchlist
    is published to ``stream:market_data`` and run through fundamentals and
    events. The two sets are deliberately different — capture follows the
    index so a departed name's history is already on disk, while what the
    sleeves trade is a separate decision.
    """

    def __init__(
        self,
        config: AppConfig,
        ib_client: IBClientProtocol,
        redis_client: RedisStreamClient,
        db_session: Any,
        events_source: EventsSourceProtocol | None = None,
        capture_universe: list[str] | None = None,
    ):
        self._config = config
        self._calendar = MarketCalendar()
        self._running = True
        self._capture_universe = list(capture_universe or [])

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

    async def run_cycle(self, tickers: list[str]) -> CaptureHealth:
        """Run all three pipelines for each ticker, then capture the rest.

        Processes market data, fundamentals, and events for every ticker in the
        trading watchlist, then fetches and persists bars for the capture-only
        remainder. Errors for individual tickers are logged but do not stop
        processing of remaining tickers — a name IB refuses today must not cost
        the other 502 their session.

        Returns the session's :class:`CaptureHealth`.
        """
        capture_only = [t for t in self._capture_universe if t not in set(tickers)]
        expected = len(tickers) + len(capture_only)

        if not expected:
            logger.info("run_cycle_skipped", reason="no_tickers")
            return CaptureHealth(tickers_expected=0, tickers_written=0)

        logger.info(
            "run_cycle_start",
            ticker_count=len(tickers),
            capture_only_count=len(capture_only),
        )

        now = datetime.now(timezone.utc)
        today = now.date()
        yesterday = date.fromordinal(today.toordinal() - 1)
        written = 0

        for ticker in tickers:
            if not self._running:
                logger.info("run_cycle_interrupted", reason="shutdown_requested")
                break

            # Market data
            try:
                if await self._market_data.ingest(ticker, yesterday, today):
                    written += 1
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

        # Capture-only names: persisted, never published. Fundamentals and
        # events are deliberately skipped — nothing downstream reads them for
        # a name no sleeve trades, and running them would triple the IB load.
        for ticker in capture_only:
            if not self._running:
                logger.info("run_cycle_interrupted", reason="shutdown_requested")
                break
            try:
                if await self._market_data.capture(ticker, yesterday, today):
                    written += 1
            except Exception:
                logger.exception("capture_error", ticker=ticker)

        health = CaptureHealth(tickers_expected=expected, tickers_written=written)
        logger.info(
            "run_cycle_complete",
            ticker_count=len(tickers),
            capture_expected=health.tickers_expected,
            capture_written=health.tickers_written,
            capture_shortfall=health.shortfall,
        )
        return health

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


if __name__ == "__main__":
    import asyncio

    from shared.config import load_config

    config = load_config("config/default.yaml")

    async def main() -> None:
        import redis.asyncio as aioredis

        from services.data_ingestion.ib_client import IBClient
        from shared.heartbeat import register_heartbeat_collector, write_heartbeat
        from shared.observability import setup_metrics
        from shared.redis_client import RedisStreamClient

        setup_metrics("data-ingestion", port=config.observability.prometheus_port)
        # CRITICAL fix: expose heartbeat staleness THROUGH the metrics
        # server's own (independent) thread — see shared/heartbeat.py's
        # module docstring for why up==1 alone can't catch a wedged loop.
        register_heartbeat_collector()

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        redis_conn = aioredis.from_url(config.redis.url)
        redis_client = RedisStreamClient(redis_conn)
        ib_client = IBClient(
            host=config.ib.host,
            port=config.ib.paper_port if config.mode != "live" else config.ib.live_port,
            # Distinct from execution's client_id: IB kicks the older session
            # when two clients connect with the same id.
            client_id=config.ib.data_client_id,
        )
        # Real session, not None (KAN-58). The parameter has threaded through
        # to all three pipelines since the service was written, but nothing was
        # connected to the other end — so ohlcv_daily sat at 0 rows and every
        # bar this service ever fetched was published and then dropped.
        engine = create_engine(config.database.url)
        db_session = sessionmaker(bind=engine)()

        from shared.universe import resolve_capture_universe, resolve_watchlist

        tickers = resolve_watchlist(
            config.universe.watchlist_source, config.universe.custom_tickers
        )
        capture_universe = resolve_capture_universe(config.universe.capture_source)
        runner = DataIngestionRunner(
            config=config,
            ib_client=ib_client,
            redis_client=redis_client,
            db_session=db_session,
            capture_universe=capture_universe,
        )
        logger.info(
            "Data ingestion service started",
            mode=config.mode,
            watchlist_source=config.universe.watchlist_source,
            ticker_count=len(tickers),
            capture_source=config.universe.capture_source,
            capture_ticker_count=len(capture_universe),
        )
        while True:
            if runner.is_market_active() or config.mode == "backtest":
                await runner.run_cycle(tickers)
            # T6: heartbeat file for the container healthcheck (see
            # docker-compose.yml) — proves this loop is still iterating, not
            # wedged. Written once per poll cycle since this loop's own
            # cadence is minutes, not seconds.
            write_heartbeat()
            await asyncio.sleep(config.data_ingestion.polling_interval_minutes * 60)

    asyncio.run(main())
