import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from services.data_ingestion.events import EventsPipeline, EventsSourceProtocol


class TestEventsPipeline:
    @pytest.mark.asyncio
    async def test_ingest_fetches_from_source_and_publishes(self):
        """Events pipeline should fetch from source and publish to Redis stream."""
        mock_source = MagicMock(spec=EventsSourceProtocol)
        mock_source.get_events = AsyncMock(return_value=[
            {
                "event_type": "earnings",
                "headline": "AAPL beats Q3 estimates",
                "sentiment_score": 0.8,
            },
        ])
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(return_value="1234-0")
        mock_db = MagicMock()

        pipeline = EventsPipeline(
            events_source=mock_source,
            redis_client=mock_redis,
            db_session=mock_db,
        )
        await pipeline.ingest("AAPL")

        mock_source.get_events.assert_called_once_with("AAPL")
        mock_redis.publish.assert_called_once()

        call_args = mock_redis.publish.call_args
        assert call_args[0][0] == "stream:events"
        published_data = call_args[0][1]
        assert published_data["ticker"] == "AAPL"

    @pytest.mark.asyncio
    async def test_ingest_attaches_point_in_time_fields(self):
        """Every published event must have effective_at, ingested_at, source_revision."""
        mock_source = MagicMock(spec=EventsSourceProtocol)
        mock_source.get_events = AsyncMock(return_value=[
            {"event_type": "news", "headline": "Test headline"},
        ])
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(return_value="1234-0")

        pipeline = EventsPipeline(
            events_source=mock_source,
            redis_client=mock_redis,
            db_session=MagicMock(),
        )
        await pipeline.ingest("MSFT")

        call_args = mock_redis.publish.call_args
        published_data = call_args[0][1]
        assert "effective_at" in published_data
        assert "ingested_at" in published_data
        assert "source_revision" in published_data

    @pytest.mark.asyncio
    async def test_ingest_handles_empty_results(self):
        """Pipeline should handle empty event list without publishing."""
        mock_source = MagicMock(spec=EventsSourceProtocol)
        mock_source.get_events = AsyncMock(return_value=[])
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(return_value="1234-0")

        pipeline = EventsPipeline(
            events_source=mock_source,
            redis_client=mock_redis,
            db_session=MagicMock(),
        )
        await pipeline.ingest("GOOG")

        mock_source.get_events.assert_called_once_with("GOOG")
        mock_redis.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_ingest_publishes_multiple_events(self):
        """Pipeline should publish one message per event."""
        mock_source = MagicMock(spec=EventsSourceProtocol)
        mock_source.get_events = AsyncMock(return_value=[
            {"event_type": "earnings", "headline": "Q3 earnings"},
            {"event_type": "dividend", "headline": "Dividend announced"},
            {"event_type": "news", "headline": "CEO interview"},
        ])
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(return_value="1234-0")

        pipeline = EventsPipeline(
            events_source=mock_source,
            redis_client=mock_redis,
            db_session=MagicMock(),
        )
        await pipeline.ingest("TSLA")

        assert mock_redis.publish.call_count == 3
        for call in mock_redis.publish.call_args_list:
            assert call[0][0] == "stream:events"
            published_data = call[0][1]
            assert published_data["ticker"] == "TSLA"
            assert "effective_at" in published_data
            assert "ingested_at" in published_data
            assert "source_revision" in published_data

    @pytest.mark.asyncio
    async def test_ingest_includes_sentiment_score_when_present(self):
        """If source provides sentiment_score, it should be in the message."""
        mock_source = MagicMock(spec=EventsSourceProtocol)
        mock_source.get_events = AsyncMock(return_value=[
            {
                "event_type": "news",
                "headline": "Positive outlook",
                "sentiment_score": 0.75,
            },
        ])
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(return_value="1234-0")

        pipeline = EventsPipeline(
            events_source=mock_source,
            redis_client=mock_redis,
            db_session=MagicMock(),
        )
        await pipeline.ingest("AMZN")

        call_args = mock_redis.publish.call_args
        published_data = call_args[0][1]
        assert published_data["sentiment_score"] == "0.75"

    @pytest.mark.asyncio
    async def test_events_source_protocol_conformance(self):
        """Verify that a class implementing EventsSourceProtocol works."""
        class StubEventsSource:
            async def get_events(self, ticker: str) -> list[dict]:
                return [{"event_type": "test", "headline": "stub"}]

        source = StubEventsSource()
        result = await source.get_events("TEST")
        assert len(result) == 1
        assert result[0]["event_type"] == "test"
