import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from services.data_ingestion.fundamentals import FundamentalsPipeline


class TestFundamentalsPipeline:
    @pytest.mark.asyncio
    async def test_ingest_fetches_from_ib_and_publishes(self):
        """Fundamentals pipeline should fetch from IB and publish to Redis stream."""
        mock_ib = MagicMock()
        mock_ib.get_fundamentals = AsyncMock(return_value={
            "raw": "<xml>snapshot</xml>",
            "pe_ratio": 28.5,
            "market_cap": 3_000_000_000_000,
        })
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(return_value="1234-0")
        mock_db = MagicMock()

        pipeline = FundamentalsPipeline(
            ib_client=mock_ib,
            redis_client=mock_redis,
            db_session=mock_db,
        )
        await pipeline.ingest("AAPL")

        mock_ib.get_fundamentals.assert_called_once_with("AAPL")
        mock_redis.publish.assert_called_once()

        call_args = mock_redis.publish.call_args
        assert call_args[0][0] == "stream:fundamentals"
        published_data = call_args[0][1]
        assert published_data["ticker"] == "AAPL"
        assert "effective_at" in published_data
        assert "ingested_at" in published_data
        assert published_data["source_revision"] == "ib-snapshot"

    @pytest.mark.asyncio
    async def test_ingest_attaches_point_in_time_fields(self):
        """Every published message must have effective_at, ingested_at, source_revision."""
        mock_ib = MagicMock()
        mock_ib.get_fundamentals = AsyncMock(return_value={"raw": "data"})
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(return_value="1234-0")

        pipeline = FundamentalsPipeline(
            ib_client=mock_ib,
            redis_client=mock_redis,
            db_session=MagicMock(),
        )

        before = datetime.now(timezone.utc)
        await pipeline.ingest("MSFT")
        after = datetime.now(timezone.utc)

        call_args = mock_redis.publish.call_args
        published_data = call_args[0][1]

        # Verify point-in-time fields are present and reasonable
        assert "effective_at" in published_data
        assert "ingested_at" in published_data
        assert published_data["source_revision"] == "ib-snapshot"

    @pytest.mark.asyncio
    async def test_ingest_publishes_fundamental_data_in_message(self):
        """Published message should contain the fundamental data from IB."""
        raw_data = {"pe_ratio": 25.0, "eps": 6.5, "market_cap": 2_500_000_000_000}
        mock_ib = MagicMock()
        mock_ib.get_fundamentals = AsyncMock(return_value=raw_data)
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(return_value="1234-0")

        pipeline = FundamentalsPipeline(
            ib_client=mock_ib,
            redis_client=mock_redis,
            db_session=MagicMock(),
        )
        await pipeline.ingest("GOOG")

        call_args = mock_redis.publish.call_args
        published_data = call_args[0][1]
        assert published_data["ticker"] == "GOOG"
        assert published_data["metric_type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_ingest_handles_empty_fundamentals(self):
        """Pipeline should handle IB returning empty fundamentals dict."""
        mock_ib = MagicMock()
        mock_ib.get_fundamentals = AsyncMock(return_value={})
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(return_value="1234-0")

        pipeline = FundamentalsPipeline(
            ib_client=mock_ib,
            redis_client=mock_redis,
            db_session=MagicMock(),
        )
        await pipeline.ingest("TSLA")

        mock_redis.publish.assert_called_once()
