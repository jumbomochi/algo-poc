from __future__ import annotations

import pytest
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from services.data_ingestion.market_data import MarketDataPipeline
from shared.models import Base
from shared.models.market_data import OHLCVDaily


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def bar(day: date, *, close: float = 153.0, volume: int = 1_000_000) -> dict:
    return {
        "date": day,
        "open": 150.0,
        "high": 155.0,
        "low": 149.0,
        "close": close,
        "volume": volume,
    }


def ib_returning(*bars) -> MagicMock:
    client = MagicMock()
    client.get_daily_bars = AsyncMock(return_value=list(bars))
    return client


class TestMarketDataPipeline:
    @pytest.mark.asyncio
    async def test_fetch_daily_bars_returns_normalized_data(self, db_session):
        pipeline = MarketDataPipeline(
            ib_client=ib_returning(bar(date(2025, 1, 6))),
            redis_client=AsyncMock(),
            db_session=db_session,
        )
        bars = await pipeline.fetch_daily_bars("AAPL", date(2025, 1, 6), date(2025, 1, 6))
        assert len(bars) == 1
        assert bars[0]["ticker"] == "AAPL"
        assert bars[0]["close"] == 153.0

    @pytest.mark.asyncio
    async def test_publish_to_stream(self, db_session):
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(return_value="1234-0")
        pipeline = MarketDataPipeline(
            ib_client=ib_returning(bar(date(2025, 1, 6))),
            redis_client=mock_redis,
            db_session=db_session,
        )
        await pipeline.ingest("AAPL", date(2025, 1, 6), date(2025, 1, 6))
        mock_redis.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limiting(self, db_session):
        pipeline = MarketDataPipeline(
            ib_client=ib_returning(),
            redis_client=AsyncMock(),
            db_session=db_session,
            rate_limit_per_sec=2,
        )
        for ticker in ["AAPL", "MSFT", "GOOG"]:
            await pipeline.fetch_daily_bars(ticker, date(2025, 1, 6), date(2025, 1, 6))


class TestCapture:
    """KAN-58 — every bar fetched is kept, stamped with its trading session.

    The bar date is the whole point: ``MarketDataMessage`` carries only an
    ingestion ``timestamp``, so a consumer persisting off ``stream:market_data``
    would stamp every row with the time it happened to run and the series would
    be unusable as point-in-time history.
    """

    @pytest.mark.asyncio
    async def test_ingest_writes_a_row_stamped_with_the_bar_date(self, db_session):
        session_day = date(2025, 1, 6)
        pipeline = MarketDataPipeline(
            ib_client=ib_returning(bar(session_day)),
            redis_client=AsyncMock(),
            db_session=db_session,
        )

        await pipeline.ingest("AAPL", session_day, session_day)

        rows = db_session.scalars(select(OHLCVDaily)).all()
        assert len(rows) == 1
        assert rows[0].ticker == "AAPL"
        assert rows[0].date == session_day
        assert rows[0].date != datetime.now(timezone.utc).date()
        assert rows[0].close == 153.0
        assert rows[0].volume == 1_000_000

    @pytest.mark.asyncio
    async def test_reingesting_the_same_session_updates_rather_than_duplicates(
        self, db_session
    ):
        session_day = date(2025, 1, 6)
        pipeline = MarketDataPipeline(
            ib_client=ib_returning(bar(session_day, close=153.0)),
            redis_client=AsyncMock(),
            db_session=db_session,
        )
        await pipeline.ingest("AAPL", session_day, session_day)

        # A re-run against a corrected bar must repair, not duplicate.
        pipeline._ib.get_daily_bars = AsyncMock(
            return_value=[bar(session_day, close=154.5, volume=2_000_000)]
        )
        await pipeline.ingest("AAPL", session_day, session_day)

        rows = db_session.scalars(select(OHLCVDaily)).all()
        assert len(rows) == 1
        assert rows[0].close == 154.5
        assert rows[0].volume == 2_000_000

    @pytest.mark.asyncio
    async def test_ingest_returns_the_number_of_bars_written(self, db_session):
        pipeline = MarketDataPipeline(
            ib_client=ib_returning(bar(date(2025, 1, 6)), bar(date(2025, 1, 7))),
            redis_client=AsyncMock(),
            db_session=db_session,
        )
        assert await pipeline.ingest("AAPL", date(2025, 1, 6), date(2025, 1, 7)) == 2

    @pytest.mark.asyncio
    async def test_capture_persists_without_publishing(self, db_session):
        """The capture universe is wider than the trading universe: a name the
        sleeves never trade still needs its bars kept, but putting it on
        ``stream:market_data`` would widen what signal_generation scores."""
        mock_redis = AsyncMock()
        session_day = date(2025, 1, 6)
        pipeline = MarketDataPipeline(
            ib_client=ib_returning(bar(session_day)),
            redis_client=mock_redis,
            db_session=db_session,
        )

        written = await pipeline.capture("ABNB", session_day, session_day)

        assert written == 1
        mock_redis.publish.assert_not_called()
        rows = db_session.scalars(select(OHLCVDaily)).all()
        assert [(r.ticker, r.date) for r in rows] == [("ABNB", session_day)]

    @pytest.mark.asyncio
    async def test_a_row_survives_its_ticker_leaving_the_universe(self, db_session):
        """AC5 — retention is the entire point. Nothing on the ingest path may
        delete, so a later cycle that no longer covers a departed name leaves
        its history intact."""
        session_day = date(2025, 1, 6)
        pipeline = MarketDataPipeline(
            ib_client=ib_returning(bar(session_day)),
            redis_client=AsyncMock(),
            db_session=db_session,
        )
        await pipeline.capture("DEPARTED", session_day, session_day)

        # A later session covering only the surviving name.
        pipeline._ib.get_daily_bars = AsyncMock(
            return_value=[bar(date(2025, 1, 7))]
        )
        await pipeline.capture("AAPL", date(2025, 1, 7), date(2025, 1, 7))

        tickers = set(db_session.scalars(select(OHLCVDaily.ticker)).all())
        assert tickers == {"DEPARTED", "AAPL"}
