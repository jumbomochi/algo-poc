from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select

from shared.logging import get_logger
from shared.models.market_data import OHLCVDaily
from shared.redis_client import RedisStreamClient
from shared.schemas.messages import MarketDataMessage
from services.data_ingestion.ib_client import IBClientProtocol

logger = get_logger("market_data_pipeline")


class MarketDataPipeline:
    """Fetches daily bars, keeps them, and publishes them to the trading path.

    Two exits, deliberately separate (KAN-58):

    * :meth:`ingest` — persist **and** publish. Used for the trading watchlist,
      whose bars signal_generation scores.
    * :meth:`capture` — persist only. Used for the rest of the index, so the
      history of a name accumulates from today whether or not any sleeve
      trades it, and survives the name leaving the index.

    Persistence happens *here*, at the fetch, and not off ``stream:market_data``:
    :class:`~shared.schemas.messages.MarketDataMessage` carries an ingestion
    ``timestamp`` and no bar date, so a stream consumer could only stamp rows
    with the time it ran. ``ohlcv_daily`` exists to be point-in-time history;
    a row stamped with anything but its own trading session is worthless for
    that.
    """

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

    async def ingest(self, ticker: str, start: date, end: date) -> int:
        """Fetch, persist, and publish. Returns the number of bars persisted."""
        bars = await self.fetch_daily_bars(ticker, start, end)
        written = self._persist(bars)
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
        return written

    async def capture(self, ticker: str, start: date, end: date) -> int:
        """Fetch and persist without publishing. Returns bars persisted."""
        return self._persist(await self.fetch_daily_bars(ticker, start, end))

    def _persist(self, bars: list[dict[str, Any]]) -> int:
        """Upsert completed sessions on the ``(ticker, date)`` unique index.

        Idempotent by construction so re-running a session repairs rather than
        duplicates — a corrected bar overwrites, and a re-run after a partial
        failure completes the day. Deliberately a read-then-write rather than a
        dialect-specific ``ON CONFLICT``: the same code has to run against
        Postgres in the container and sqlite in the suite, and the volume here
        is ~one row per name per session.

        **Today's bar is never kept.** A bar fetched during the session is a
        partial — its close, high, low and volume are only whatever the session
        had reached — and the ingest loop runs *only* while the market is open,
        so no later cycle would ever correct it. It would freeze as a wrong
        daily bar. The caller's lookback window is what brings the completed
        session in on a subsequent day.

        Never raises. A failed commit is rolled back and reported as zero
        written: the session is created once at service startup and reused for
        the life of the process, so an un-rolled-back failure would leave the
        transaction deactivated and every later ticker raising
        ``PendingRollbackError`` — capture would stop permanently, behind a
        green healthcheck, because the runner logs per-ticker errors and keeps
        looping. Swallowing here also keeps a Postgres outage from suppressing
        the publish in :meth:`ingest`; the stream is the trading path and must
        not acquire a dependency on the archive.

        There is no delete path, and there must never be one — a ticker leaving
        the watchlist must leave its bars behind. That is the only reason this
        table can produce a point-in-time baseline (KAN-52 established that IB
        will not serve a departed name's history back retroactively).
        """
        if self._db is None:
            return 0

        now = datetime.now(timezone.utc)
        today = now.date()
        completed = [b for b in bars if b["date"] < today]
        if not completed:
            return 0

        try:
            for bar in completed:
                existing = self._db.scalars(
                    select(OHLCVDaily).where(
                        OHLCVDaily.ticker == bar["ticker"],
                        OHLCVDaily.date == bar["date"],
                    )
                ).one_or_none()
                if existing is None:
                    self._db.add(
                        OHLCVDaily(
                            ticker=bar["ticker"],
                            date=bar["date"],
                            open=bar["open"],
                            high=bar["high"],
                            low=bar["low"],
                            close=bar["close"],
                            volume=bar["volume"],
                            ingested_at=now,
                        )
                    )
                else:
                    existing.open = bar["open"]
                    existing.high = bar["high"]
                    existing.low = bar["low"]
                    existing.close = bar["close"]
                    existing.volume = bar["volume"]
                    existing.ingested_at = now
            self._db.commit()
        except Exception:
            self._db.rollback()
            logger.exception(
                "capture_persist_failed",
                ticker=completed[0]["ticker"],
                bars=len(completed),
            )
            return 0
        return len(completed)
