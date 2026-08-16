"""The notifications service is the only consumer of ``stream:alerts``, so it
is the only place an alert can be written down. Gate 5 of the go-live checklist
reads what it writes."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from services.notifications.runner import NotificationsServiceRunner
from shared.config import AppConfig, NotificationsConfig
from shared.models.alerts import AlertRecord
from shared.models.base import Base
from shared.redis_client import StreamMessage
from shared.schemas.messages import AlertMessage


RAISED_AT = datetime(2026, 8, 14, 13, 30, tzinfo=timezone.utc)


def make_alert_stream_message(
    *,
    event_type: str = "stop_loss_triggered",
    priority: str = "critical",
    message: str = "AAPL stop-loss fired",
    message_id: str = "1755178200000-0",
    context: dict | None = None,
) -> StreamMessage:
    alert = AlertMessage(
        timestamp=RAISED_AT,
        event_type=event_type,
        priority=priority,
        message=message,
        context=context or {"symbol": "AAPL"},
    )
    return StreamMessage(
        stream="stream:alerts",
        message_id=message_id,
        data=alert.to_stream_dict(),
    )


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session


@pytest.fixture()
def mock_config():
    config = MagicMock(spec=AppConfig)
    config.notifications = NotificationsConfig(
        slack_enabled=True, email_enabled=True, sms_enabled=True
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
def runner(mock_config, mock_redis, db_session):
    return NotificationsServiceRunner(
        config=mock_config,
        redis_client=mock_redis,
        dispatcher=AsyncMock(),
        db_session=db_session,
    )


class TestAlertRecording:
    @pytest.mark.asyncio
    async def test_dispatched_alert_is_written_to_alert_records(
        self, runner, db_session
    ):
        await runner.process_message(make_alert_stream_message())

        record = db_session.scalars(select(AlertRecord)).one()
        assert record.event_type == "stop_loss_triggered"
        assert record.priority == "critical"
        assert record.message == "AAPL stop-loss fired"
        assert record.context == {"symbol": "AAPL"}
        assert record.raised_at.replace(tzinfo=timezone.utc) == RAISED_AT
        assert record.message_id == "1755178200000-0"
        # Nothing resolves itself — that is an operator action.
        assert record.resolved_at is None

    @pytest.mark.asyncio
    async def test_replayed_alert_is_not_recorded_twice(self, runner, db_session):
        """setup() replays pending alerts after a crash; a replay is the same
        incident, not a second one."""
        msg = make_alert_stream_message()

        await runner.process_message(msg)
        await runner.process_message(msg)

        assert len(db_session.scalars(select(AlertRecord)).all()) == 1

    @pytest.mark.asyncio
    async def test_alert_is_recorded_even_when_every_channel_fails(
        self, mock_config, mock_redis, db_session
    ):
        """A critical alert nobody could deliver is exactly the one gate 5 must
        see, so the row is written before dispatch."""
        dispatcher = AsyncMock()
        dispatcher.dispatch.side_effect = RuntimeError("telegram unreachable")
        runner = NotificationsServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            dispatcher=dispatcher,
            db_session=db_session,
        )

        await runner.process_message(make_alert_stream_message())

        assert len(db_session.scalars(select(AlertRecord)).all()) == 1
        mock_redis.send_to_dead_letter.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_recording_failure_does_not_swallow_the_alert(
        self, mock_config, mock_redis, db_session
    ):
        """The database is not on the delivery path: if the write fails the
        alert must still reach the channels."""
        dispatcher = AsyncMock()
        broken = MagicMock(wraps=db_session)
        broken.add.side_effect = RuntimeError("db down")
        runner = NotificationsServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            dispatcher=dispatcher,
            db_session=broken,
        )

        await runner.process_message(make_alert_stream_message())

        dispatcher.dispatch.assert_awaited_once()
        mock_redis.ack.assert_awaited_once()
        mock_redis.send_to_dead_letter.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_rollback_does_not_swallow_the_alert(
        self, mock_config, mock_redis, db_session
    ):
        """A dead connection makes rollback() raise too. That must not turn a
        recording failure into a dead-lettered, undelivered alert — which would
        happen precisely when the database is sick and criticals are likeliest.
        """
        dispatcher = AsyncMock()
        broken = MagicMock(wraps=db_session)
        broken.scalar.side_effect = RuntimeError("connection is closed")
        broken.rollback.side_effect = RuntimeError("connection is closed")
        runner = NotificationsServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            dispatcher=dispatcher,
            db_session=broken,
        )

        await runner.process_message(make_alert_stream_message())

        dispatcher.dispatch.assert_awaited_once()
        mock_redis.send_to_dead_letter.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_skipped_replay_does_not_leave_a_transaction_open(
        self, runner, db_session
    ):
        """The dedupe SELECT autobegins; returning without ending it leaves the
        connection idle-in-transaction on a long-lived session, holding a
        snapshot open until the next alert arrives."""
        msg = make_alert_stream_message()
        await runner.process_message(msg)

        await runner.process_message(msg)

        assert not db_session.in_transaction()

    @pytest.mark.asyncio
    async def test_runner_without_a_session_still_dispatches(
        self, mock_config, mock_redis
    ):
        """No DB configured is a degraded mode, not a crash — gate 5 detects the
        resulting silence itself."""
        dispatcher = AsyncMock()
        runner = NotificationsServiceRunner(
            config=mock_config,
            redis_client=mock_redis,
            dispatcher=dispatcher,
            db_session=None,
        )

        await runner.process_message(make_alert_stream_message())

        dispatcher.dispatch.assert_awaited_once()
