from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis


DEAD_LETTER_SUFFIX = ":dlq"

# T6 (observability & unattended healthchecks): an unbounded stream is how a
# quiet Redis eventually OOMs and takes the whole message bus down silently
# (review Theme 6.3) — every publish() call caps its stream with
# `XADD ... MAXLEN ~ <n>` so already-consumed history gets trimmed instead of
# growing forever. `~` (approximate=True) lets Redis trim lazily in whole
# macro-nodes rather than exactly-per-entry, which is materially cheaper and
# is the documented/recommended mode for this. The bound is generous relative
# to real trading-day volume (see tests/shared/test_redis_client.py) — this
# is memory insurance, not a working-set limit anything should ever bump
# into in normal operation.
#
# NOTE: this intentionally does not touch send_to_dead_letter()'s XADD below
# (DLQ region owned by T4 in a parallel thread) — DLQ volume should stay low
# by construction (only failures land there), so it's lower priority to
# bound and left for T4 to cap the same way if/when useful.
DEFAULT_STREAM_MAXLEN = 100_000


@dataclass
class StreamMessage:
    stream: str
    message_id: str
    data: dict[str, str]


class RedisStreamClient:
    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        stream_maxlen: int | None = DEFAULT_STREAM_MAXLEN,
    ):
        self._redis = redis
        self._stream_maxlen = stream_maxlen

    async def publish(
        self,
        stream: str,
        data: dict[str, str],
        idempotency_key: str | None = None,
    ) -> str:
        if idempotency_key:
            data = {**data, "_idempotency_key": idempotency_key}
        if self._stream_maxlen is not None:
            return await self._redis.xadd(
                stream, data, maxlen=self._stream_maxlen, approximate=True
            )
        return await self._redis.xadd(stream, data)

    async def create_consumer_group(
        self,
        stream: str,
        group: str,
        start_id: str = "0",
    ) -> None:
        try:
            await self._redis.xgroup_create(stream, group, start_id, mkstream=True)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def read_group(
        self,
        stream: str,
        group: str,
        consumer: str,
        count: int = 10,
        block_ms: int = 5000,
    ) -> list[StreamMessage]:
        results = await self._redis.xreadgroup(
            group, consumer, {stream: ">"}, count=count, block=block_ms,
        )
        return self._decode_results(results)

    async def drain_pending(
        self,
        stream: str,
        group: str,
        consumer: str,
        batch_size: int = 100,
    ) -> list[StreamMessage]:
        """Claim and return ALL pending (delivered-but-unacked) messages.

        Messages read before a crash but never acked stay in the group's
        pending entries list forever; ``xreadgroup`` with ``">"`` never
        re-delivers them. Call this during service startup, before the normal
        ``read_group`` loop, so no message is silently lost across a restart.

        Uses ``XAUTOCLAIM`` (min idle 0), which claims from *any* consumer in
        the group — this also recovers messages held by a dead worker or a
        prior consumer name, not just this consumer's own.
        """
        drained: list[StreamMessage] = []
        seen: set[str] = set()
        cursor = "0-0"
        while True:
            next_cursor, entries, _deleted = await self._redis.xautoclaim(
                stream, group, consumer,
                min_idle_time=0, start_id=cursor, count=batch_size,
            )
            batch = self._decode_results([(stream, entries)])
            # XAUTOCLAIM's start is inclusive and some servers (and fakeredis)
            # return the last-claimed id rather than 0-0 as the next cursor —
            # dedupe on message id so the loop terminates on either behaviour.
            new = [m for m in batch if m.message_id not in seen]
            if not new:
                return drained
            drained.extend(new)
            seen.update(m.message_id for m in new)
            next_cursor = (
                next_cursor if isinstance(next_cursor, str) else next_cursor.decode()
            )
            if next_cursor == "0-0":
                return drained
            cursor = next_cursor

    def _decode_results(self, results: Any) -> list[StreamMessage]:
        messages = []
        for stream_name, entries in results:
            s = stream_name if isinstance(stream_name, str) else stream_name.decode()
            for msg_id, fields in entries:
                mid = msg_id if isinstance(msg_id, str) else msg_id.decode()
                decoded = {
                    (k if isinstance(k, str) else k.decode()): (
                        v if isinstance(v, str) else v.decode()
                    )
                    for k, v in fields.items()
                }
                messages.append(StreamMessage(stream=s, message_id=mid, data=decoded))
        return messages

    async def ack(self, stream: str, group: str, message_id: str) -> int:
        return await self._redis.xack(stream, group, message_id)

    async def send_to_dead_letter(
        self,
        stream: str,
        message: StreamMessage,
        error: str,
    ) -> str:
        dlq_stream = stream + DEAD_LETTER_SUFFIX
        data = {**message.data, "_error": error, "_original_id": message.message_id}
        return await self._redis.xadd(dlq_stream, data)
