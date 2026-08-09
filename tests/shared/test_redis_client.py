import pytest
from unittest.mock import AsyncMock

from shared.redis_client import DEFAULT_STREAM_MAXLEN, RedisStreamClient, StreamMessage


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
            maxlen=DEFAULT_STREAM_MAXLEN,
            approximate=True,
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


class TestStreamBounding:
    """T6: unbounded streams are how a quiet Redis OOMs the whole bus (review
    Theme 6.3). publish() now caps every stream it writes to with XADD
    MAXLEN ~ so old, already-consumed entries get trimmed automatically
    instead of growing forever."""

    @pytest.mark.asyncio
    async def test_publish_bounds_stream_length_via_maxlen(self):
        import fakeredis.aioredis

        client = RedisStreamClient(fakeredis.aioredis.FakeRedis(), stream_maxlen=5)
        for i in range(50):
            await client.publish("s", {"n": str(i)})

        length = await client._redis.xlen("s")
        assert length <= 5

    @pytest.mark.asyncio
    async def test_publish_with_maxlen_none_leaves_stream_unbounded(self):
        import fakeredis.aioredis

        client = RedisStreamClient(fakeredis.aioredis.FakeRedis(), stream_maxlen=None)
        for i in range(20):
            await client.publish("s", {"n": str(i)})

        assert await client._redis.xlen("s") == 20

    @pytest.mark.asyncio
    async def test_default_maxlen_is_a_large_generous_bound(self):
        # Sanity check on the constant itself: large enough that no normal
        # trading-day volume is ever truncated mid-processing, small enough
        # to actually bound memory.
        assert 10_000 <= DEFAULT_STREAM_MAXLEN <= 1_000_000

    def test_default_maxlen_fits_the_maxmemory_ceiling_with_headroom(self):
        """IMPORTANT fix: DEFAULT_STREAM_MAXLEN and docker-compose.yml's
        redis --maxmemory were previously picked independently. This proves
        the arithmetic documented next to DEFAULT_STREAM_MAXLEN in
        shared/redis_client.py: worst case (all primary streams
        simultaneously at cap) must fit well inside the ceiling, leaving
        headroom for Redis overhead, PEL, and uncapped DLQ growth.
        See tests/deploy/test_observability_healthchecks.py for the
        companion test that reads the actual --maxmemory value out of
        docker-compose.yml and checks it against this same constant.
        """
        assumed_worst_case_entry_bytes = 1024  # 1 KiB, see the constant's docstring
        primary_stream_count = 9  # market_data, fundamentals, events, signals,
        # recommendations, approved_orders, fills, alerts, kill

        worst_case_bytes = (
            primary_stream_count * DEFAULT_STREAM_MAXLEN * assumed_worst_case_entry_bytes
        )
        maxmemory_bytes = 512 * 1024 * 1024  # matches docker-compose.yml's --maxmemory 512mb

        assert worst_case_bytes < maxmemory_bytes * 0.5, (
            "DEFAULT_STREAM_MAXLEN no longer leaves >=50% headroom under the "
            "512mb maxmemory ceiling — reconcile both numbers together (see "
            "the comment above DEFAULT_STREAM_MAXLEN) rather than changing "
            "one in isolation"
        )
