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


class TestPendingReplay:
    """Crash-recovery semantics: delivered-but-unacked messages are replayable."""

    @pytest.fixture
    def fake_client(self):
        import fakeredis.aioredis

        return RedisStreamClient(fakeredis.aioredis.FakeRedis())

    @pytest.mark.asyncio
    async def test_drain_pending_returns_unacked_messages(self, fake_client):
        await fake_client.create_consumer_group("s", "g")
        for i in range(3):
            await fake_client.publish("s", {"n": str(i)})

        # Deliver but do not ack — simulates a crash mid-processing.
        delivered = await fake_client.read_group("s", "g", "c1", block_ms=1)
        assert len(delivered) == 3

        # "Restart": the same consumer drains its pending list.
        pending = await fake_client.drain_pending("s", "g", "c1")
        assert [m.data["n"] for m in pending] == ["0", "1", "2"]

    @pytest.mark.asyncio
    async def test_drain_pending_empty_after_ack(self, fake_client):
        await fake_client.create_consumer_group("s", "g")
        await fake_client.publish("s", {"n": "0"})
        delivered = await fake_client.read_group("s", "g", "c1", block_ms=1)
        await fake_client.ack("s", "g", delivered[0].message_id)

        assert await fake_client.drain_pending("s", "g", "c1") == []

    @pytest.mark.asyncio
    async def test_drain_pending_paginates_without_ack(self, fake_client):
        """The cursor must advance even though nothing is acked mid-drain."""
        await fake_client.create_consumer_group("s", "g")
        for i in range(5):
            await fake_client.publish("s", {"n": str(i)})
        await fake_client.read_group("s", "g", "c1", block_ms=1)

        pending = await fake_client.drain_pending("s", "g", "c1", batch_size=2)
        assert len(pending) == 5

    @pytest.mark.asyncio
    async def test_drain_pending_recovers_other_consumers_messages(self, fake_client):
        """XAUTOCLAIM claims across consumers: a restarted worker with a new
        name still recovers messages a dead worker left pending."""
        await fake_client.create_consumer_group("s", "g")
        await fake_client.publish("s", {"n": "0"})
        await fake_client.read_group("s", "g", "dead_worker", block_ms=1)

        pending = await fake_client.drain_pending("s", "g", "new_worker")
        assert len(pending) == 1
        assert pending[0].data["n"] == "0"

    @pytest.mark.asyncio
    async def test_normal_read_does_not_redeliver_pending(self, fake_client):
        """Documents WHY replay is needed: '>' never re-delivers pending."""
        await fake_client.create_consumer_group("s", "g")
        await fake_client.publish("s", {"n": "0"})
        await fake_client.read_group("s", "g", "c1", block_ms=1)

        again = await fake_client.read_group("s", "g", "c1", block_ms=1)
        assert again == []


@pytest.mark.asyncio
async def test_stream_length_wraps_xlen():
    mock_redis = AsyncMock()
    mock_redis.xlen = AsyncMock(return_value=3)
    client = RedisStreamClient(mock_redis)
    assert await client.stream_length("stream:fills:dlq") == 3
    mock_redis.xlen.assert_awaited_once_with("stream:fills:dlq")
