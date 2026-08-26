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

# How far back each cycle re-fetches. Costs no extra IB requests (duration
# is derived from the window, one request per ticker either way) and is what
# lets a completed session land at all, plus lets a gap self-heal. Must stay
# comfortably longer than the longest market closure plus any plausible
# outage — a session that falls out of the window is unrecoverable.
CAPTURE_LOOKBACK_DAYS = 7


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
    # Names the per-cycle request budget held back. Scheduled work, not
    # failure: a later cycle picks them up and the lookback window means the
    # session is still in range when it does.
    tickers_deferred: int = 0

    @property
    def shortfall(self) -> int:
        return self.tickers_expected - self.tickers_written - self.tickers_deferred


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
        self._ib_client = ib_client
        # Where the next cycle resumes in the capture universe. Without a
        # cursor a budgeted cycle would restart from the top every time and the
        # tail of the universe would never be fetched at all.
        self._capture_cursor = 0

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

    async def ensure_ib_connected(self) -> bool:
        """Connect to IB if not already connected. Returns whether we are.

        The service constructed an ``IBClient`` and never dialled it, so
        ``_ib`` stayed ``None`` and every ``get_daily_bars`` raised
        ``AttributeError`` — which is what the "~21 error lines against one
        ``run_cycle_complete``" in the container logs was. Nothing was ever
        published or captured.

        Never raises. The gateway restarts nightly and drops the API on an
        error 1100, so a connect failure has to leave the process alive to
        retry on the next cycle; raising here would be a crash loop under
        ``restart: unless-stopped``. Only connects when disconnected — a second
        ``connectAsync`` on the same client id makes IB evict the older
        session, so a reconnect every cycle would fight itself.
        """
        try:
            if self._ib_client.is_connected():
                return True
            await self._ib_client.connect()
        except Exception:
            logger.exception(
                "ib_connect_failed",
                host=self._config.ib.host,
                client_id=self._config.ib.data_client_id,
                consequence="no bars this cycle; will retry on the next one",
            )
            return False
        logger.info(
            "ib_connected",
            host=self._config.ib.host,
            client_id=self._config.ib.data_client_id,
            readonly=True,
        )
        return True

    async def run_cycle(self, tickers: list[str]) -> CaptureHealth:
        """Run all three pipelines for each ticker, then capture the rest.

        Processes market data, fundamentals, and events for every ticker in the
        trading watchlist, then fetches and persists bars for the capture-only
        remainder. Errors for individual tickers are logged but do not stop
        processing of remaining tickers — a name IB refuses today must not cost
        the other 502 their session.

        Returns the session's :class:`CaptureHealth`.
        """
        trading = set(tickers)
        capture_only = [t for t in self._capture_universe if t not in trading]
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
        # A lookback window, not just yesterday. `IBClient.get_daily_bars`
        # derives durationStr from (end - start), so a wider window is the SAME
        # number of requests — one per ticker — and buys two things a two-day
        # window cannot. First, completed sessions actually land: the cycle runs
        # only while the market is open, so today's bar is skipped as a partial
        # (see MarketDataPipeline._persist) and a session is written on a later
        # day. A two-day window from a Monday reaches back only to Sunday, so
        # Friday's bar would never be fetched at all. Second, a gap self-heals —
        # after an outage or a redeploy the missed sessions are still inside the
        # window and get upserted on the next cycle.
        start = date.fromordinal(today.toordinal() - CAPTURE_LOOKBACK_DAYS)
        written = 0

        for ticker in tickers:
            if not self._running:
                logger.info("run_cycle_interrupted", reason="shutdown_requested")
                break

            # Market data
            try:
                if await self._market_data.ingest(ticker, start, today):
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
        #
        # Budgeted, and resumed from a cursor so successive cycles walk the
        # whole universe instead of re-fetching its head. The trading watchlist
        # above is deliberately NOT budgeted: signal_generation depends on it
        # every cycle.
        due, deferred = self._capture_slice(capture_only)
        for ticker in due:
            if not self._running:
                logger.info("run_cycle_interrupted", reason="shutdown_requested")
                break
            try:
                if await self._market_data.capture(ticker, start, today):
                    written += 1
            except Exception:
                logger.exception("capture_error", ticker=ticker)

        health = CaptureHealth(
            tickers_expected=expected,
            tickers_written=written,
            tickers_deferred=deferred,
        )
        logger.info(
            "run_cycle_complete",
            ticker_count=len(tickers),
            capture_expected=health.tickers_expected,
            capture_written=health.tickers_written,
            capture_deferred=health.tickers_deferred,
            capture_shortfall=health.shortfall,
        )
        return health

    def _capture_slice(self, capture_only: list[str]) -> tuple[list[str], int]:
        """The capture-only names due this cycle, and how many were deferred.

        Walks the universe from a persistent cursor so each cycle takes the
        next slice and the tail is reached, wrapping when it runs off the end.
        A budget of 0 means no cap.
        """
        budget = self._config.data_ingestion.capture_max_requests_per_cycle
        if budget <= 0 or len(capture_only) <= budget:
            self._capture_cursor = 0
            return capture_only, 0

        start = self._capture_cursor % len(capture_only)
        due = capture_only[start : start + budget]
        if len(due) < budget:  # wrapped
            due += capture_only[: budget - len(due)]
        self._capture_cursor = (start + budget) % len(capture_only)
        return due, len(capture_only) - len(due)

    def is_market_active(self) -> bool:
        """Check if the market is currently open using MarketCalendar."""
        now = datetime.now(timezone.utc)
        return self._calendar.is_market_open(now)

    async def shutdown(self) -> None:
        """Signal a graceful shutdown of the runner and release the IB session.

        Dropping the session matters on restart: it holds ``data_client_id``,
        and a new process connecting with the same id would otherwise race the
        one it just left behind.
        """
        logger.info("shutdown_requested")
        self._running = False
        try:
            await self._ib_client.disconnect()
        except Exception:
            # Shutdown must complete. A gateway that has already gone away is
            # the common case here, not an error worth propagating.
            logger.exception("ib_disconnect_failed")


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
        # pool_pre_ping: this session outlives any single connection. The
        # nightly db backup and any `docker compose restart postgres` drop the
        # pooled one, and without a pre-ping the next cycle fails on a stale
        # handle rather than transparently reconnecting.
        engine = create_engine(config.database.url, pool_pre_ping=True)
        db_session = sessionmaker(bind=engine)()

        from shared.universe import resolve_capture_universe, resolve_watchlist

        tickers = resolve_watchlist(
            config.universe.watchlist_source, config.universe.custom_tickers
        )
        # Capture must not be able to take the service down. If the membership
        # snapshot is missing or unreadable the right outcome is an ingest
        # service that still feeds the trading path, with the lost capture loud
        # in the log and visible as a shortfall on the daily digest — not a
        # crash loop under `restart: unless-stopped` that stops market data too.
        try:
            capture_universe = resolve_capture_universe(config.universe.capture_source)
        except Exception:
            logger.exception(
                "capture_universe_unavailable",
                capture_source=config.universe.capture_source,
                consequence="capture disabled for this process; trading path unaffected",
            )
            capture_universe = []
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
                # Dial IB before each cycle. Connecting once at startup is not
                # enough: the gateway restarts nightly and drops the API on an
                # error 1100, and this loop outlives both. `ensure_ib_connected`
                # is a no-op when the session is already up.
                if await runner.ensure_ib_connected():
                    await runner.run_cycle(tickers)
                else:
                    # Skip rather than run: without a connection every one of
                    # the 544 names would raise and bury the real cause under a
                    # wall of per-ticker tracebacks.
                    logger.warning(
                        "cycle_skipped_no_ib",
                        ticker_count=len(tickers),
                        capture_ticker_count=len(capture_universe),
                    )
            # T6: heartbeat file for the container healthcheck (see
            # docker-compose.yml) — proves this loop is still iterating, not
            # wedged. Written once per poll cycle since this loop's own
            # cadence is minutes, not seconds.
            write_heartbeat()
            await asyncio.sleep(config.data_ingestion.polling_interval_minutes * 60)

    asyncio.run(main())
