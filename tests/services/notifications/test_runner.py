from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.notifications.runner import NotificationsServiceRunner
from shared.config import AppConfig, NotificationsConfig
from shared.redis_client import StreamMessage
from shared.schemas.messages import AlertMessage


def make_alert_stream_message(
    event_type: str = "test_event",
    priority: str = "low",
    message: str = "test alert",
    message_id: str = "1-0",
) -> StreamMessage:
    alert = AlertMessage(
        timestamp=datetime.now(timezone.utc),
        event_type=event_type,
        priority=priority,
        message=message,
    )
    return StreamMessage(
        stream="stream:alerts",
        message_id=message_id,
        data=alert.to_stream_dict(),
    )


@pytest.fixture()
def mock_config():
    config = MagicMock(spec=AppConfig)
    config.notifications = NotificationsConfig(
        slack_enabled=True,
        email_enabled=True,
        sms_enabled=True,
    )
    return config


@pytest.fixture()
def mock_redis():
    redis = AsyncMock()
    redis.create_consumer_group = AsyncMock()
    redis.read_group = AsyncMock(return_value=[])
    redis.ack = AsyncMock()
    redis.send_to_dead_letter = AsyncMock()
    return redis


@pytest.fixture()
def mock_dispatcher():
    return AsyncMock()


@pytest.fixture()
def runner(mock_config, mock_redis, mock_dispatcher):
    return NotificationsServiceRunner(
        config=mock_config,
        redis_client=mock_redis,
        dispatcher=mock_dispatcher,
    )


class TestNotificationsServiceRunner:
    @pytest.mark.asyncio
    async def test_setup_creates_consumer_group(self, runner, mock_redis):
        """Setup should create a consumer group for stream:alerts."""
        await runner.setup()

        mock_redis.create_consumer_group.assert_called_once_with(
            "stream:alerts", "notifications_service"
        )

    @pytest.mark.asyncio
    async def test_process_alert_calls_dispatcher(
        self, runner, mock_redis, mock_dispatcher
    ):
        """Processing an alert message should call dispatcher.dispatch with the AlertMessage."""
        stream_msg = make_alert_stream_message(
            event_type="circuit_breaker",
            priority="critical",
            message="Portfolio drawdown exceeded 20%",
        )

        await runner.process_message(stream_msg)

        mock_dispatcher.dispatch.assert_called_once()
        dispatched_alert = mock_dispatcher.dispatch.call_args[0][0]
        assert isinstance(dispatched_alert, AlertMessage)
        assert dispatched_alert.event_type == "circuit_breaker"
        assert dispatched_alert.priority == "critical"

    @pytest.mark.asyncio
    async def test_process_alert_acks_message(
        self, runner, mock_redis, mock_dispatcher
    ):
        """After successful dispatch, the message should be acknowledged."""
        stream_msg = make_alert_stream_message(message_id="42-0")

        await runner.process_message(stream_msg)

        mock_redis.ack.assert_called_once_with(
            "stream:alerts", "notifications_service", "42-0"
        )

    @pytest.mark.asyncio
    async def test_process_alert_sends_to_dlq_on_error(
        self, runner, mock_redis, mock_dispatcher
    ):
        """If dispatch fails, the message should be sent to the dead letter queue."""
        mock_dispatcher.dispatch.side_effect = RuntimeError("dispatch failed")
        stream_msg = make_alert_stream_message(message_id="99-0")

        await runner.process_message(stream_msg)

        mock_redis.send_to_dead_letter.assert_called_once()
        call_args = mock_redis.send_to_dead_letter.call_args
        assert call_args[0][0] == "stream:alerts"

    @pytest.mark.asyncio
    async def test_health_check_returns_ok(self, runner):
        """Health check should return a dict with status ok."""
        result = await runner.health_check()

        assert result["status"] == "ok"
        assert result["service"] == "notifications_service"

    @pytest.mark.asyncio
    async def test_run_loop_reads_from_stream(self, runner, mock_redis, mock_dispatcher):
        """The run loop should read from stream:alerts and process messages."""
        stream_msg = make_alert_stream_message(
            event_type="trade_executed",
            priority="low",
            message="Bought 50 AAPL",
            message_id="7-0",
        )
        # Return one message on first call, then empty to allow loop exit
        call_count = 0

        async def read_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [stream_msg]
            runner._running = False
            return []

        mock_redis.read_group.side_effect = read_side_effect

        await runner.run()

        mock_dispatcher.dispatch.assert_called_once()
        mock_redis.ack.assert_called_once_with(
            "stream:alerts", "notifications_service", "7-0"
        )
