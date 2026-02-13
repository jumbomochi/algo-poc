import pytest
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from services.data_ingestion.market_data import MarketDataPipeline


class TestMarketDataPipeline:
    @pytest.mark.asyncio
    async def test_fetch_daily_bars_returns_normalized_data(self):
        mock_ib = MagicMock()
        mock_ib.get_daily_bars = AsyncMock(return_value=[
            {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 149.0, "close": 153.0, "volume": 1000000},
        ])
        pipeline = MarketDataPipeline(ib_client=mock_ib, redis_client=AsyncMock(), db_session=MagicMock())
        bars = await pipeline.fetch_daily_bars("AAPL", date(2025, 1, 6), date(2025, 1, 6))
        assert len(bars) == 1
        assert bars[0]["ticker"] == "AAPL"
        assert bars[0]["close"] == 153.0

    @pytest.mark.asyncio
    async def test_publish_to_stream(self):
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(return_value="1234-0")
        mock_ib = MagicMock()
        mock_ib.get_daily_bars = AsyncMock(return_value=[
            {"date": date(2025, 1, 6), "open": 150.0, "high": 155.0, "low": 149.0, "close": 153.0, "volume": 1000000},
        ])
        pipeline = MarketDataPipeline(ib_client=mock_ib, redis_client=mock_redis, db_session=MagicMock())
        await pipeline.ingest("AAPL", date(2025, 1, 6), date(2025, 1, 6))
        mock_redis.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limiting(self):
        mock_ib = MagicMock()
        mock_ib.get_daily_bars = AsyncMock(return_value=[])
        pipeline = MarketDataPipeline(
            ib_client=mock_ib, redis_client=AsyncMock(), db_session=MagicMock(),
            rate_limit_per_sec=2,
        )
        for ticker in ["AAPL", "MSFT", "GOOG"]:
            await pipeline.fetch_daily_bars(ticker, date(2025, 1, 6), date(2025, 1, 6))
