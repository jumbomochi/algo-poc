"""Publish a test alert to stream:alerts to verify notification delivery.

Usage:
    python -m scripts.ops.send_test_alert                     # low priority
    python -m scripts.ops.send_test_alert --priority critical
    ALGO_REDIS_URL=redis://:$REDIS_PASSWORD@localhost:56379/0 python -m scripts.ops.send_test_alert

The notifications service (docker compose) must be running; watch its logs
and your Telegram chat for the delivery.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from shared.config import load_config
from shared.redis_client import RedisStreamClient
from shared.schemas.messages import AlertMessage

ALERTS_STREAM = "stream:alerts"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--priority",
        default="low",
        choices=["low", "medium", "high", "critical"],
    )
    parser.add_argument("--message", default="Test alert from send_test_alert.py")
    args = parser.parse_args()

    import redis.asyncio as aioredis

    config = load_config("config/default.yaml")
    redis_conn = aioredis.from_url(config.redis.url)
    client = RedisStreamClient(redis_conn)

    alert = AlertMessage(
        timestamp=datetime.now(timezone.utc),
        event_type="ops_test",
        priority=args.priority,
        message=args.message,
    )
    message_id = await client.publish(ALERTS_STREAM, alert.to_stream_dict())
    print(f"Published {args.priority} test alert to {ALERTS_STREAM}: {message_id}")
    await redis_conn.aclose()


if __name__ == "__main__":
    asyncio.run(main())
