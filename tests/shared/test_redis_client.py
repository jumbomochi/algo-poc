import pytest
from unittest.mock import AsyncMock

from shared.redis_client import RedisStreamClient, StreamMessage


class TestRedisStreamClient:
    def test_stream_message_dataclass(self):
        msg = StreamMessage(
            stream="stream:test",
            message_id="1234-0",
            data={"ticker": "AAPL", "value": "100.0"},
        )
        assert msg.stream == "stream:test"
        assert msg.data["ticker"] == "AAPL"

    @pytest.mark.asyncio
    async def test_publish_adds_to_stream(self):
        mock_redis = AsyncMock()
        mock_redis.xadd = AsyncMock(return_value="1234-0")
        client = RedisStreamClient(mock_redis)
        msg_id = await client.publish("stream:test", {"ticker": "AAPL", "price": "150.0"})
        mock_redis.xadd.assert_called_once_with(
            "stream:test",
            {"ticker": "AAPL", "price": "150.0"},
        )
        assert msg_id == "1234-0"

    @pytest.mark.asyncio
    async def test_publish_with_idempotency_key(self):
        mock_redis = AsyncMock()
        mock_redis.xadd = AsyncMock(return_value="1234-0")
        client = RedisStreamClient(mock_redis)
        await client.publish(
            "stream:test",
            {"ticker": "AAPL"},
            idempotency_key="rec-001",
        )
        call_data = mock_redis.xadd.call_args[0][1]
        assert call_data["_idempotency_key"] == "rec-001"

    @pytest.mark.asyncio
    async def test_create_consumer_group(self):
        mock_redis = AsyncMock()
        mock_redis.xgroup_create = AsyncMock()
        client = RedisStreamClient(mock_redis)
        await client.create_consumer_group("stream:test", "my-group")
        mock_redis.xgroup_create.assert_called_once()

    @pytest.mark.asyncio
    async def test_ack_message(self):
        mock_redis = AsyncMock()
        mock_redis.xack = AsyncMock(return_value=1)
        client = RedisStreamClient(mock_redis)
        result = await client.ack("stream:test", "my-group", "1234-0")
        assert result == 1
