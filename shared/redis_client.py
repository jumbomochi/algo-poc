from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis


DEAD_LETTER_SUFFIX = ":dlq"


@dataclass
class StreamMessage:
    stream: str
    message_id: str
    data: dict[str, str]


class RedisStreamClient:
    def __init__(self, redis: aioredis.Redis):
        self._redis = redis

    async def publish(
        self,
        stream: str,
        data: dict[str, str],
        idempotency_key: str | None = None,
    ) -> str:
        if idempotency_key:
            data = {**data, "_idempotency_key": idempotency_key}
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
