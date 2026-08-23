from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import ANY, AsyncMock, MagicMock, call

import pytest
from sqlalchemy.exc import DataError, OperationalError

from services.portfolio_accounting.projector import UnattributedFillError
from services.portfolio_accounting.runner import (
    CONSUMER_GROUP,
    CONSUMER_NAME,
    FILLS_STREAM,
    PortfolioAccountingRunner,
)
from shared.redis_client import StreamMessage
from shared.schemas.messages import FillMessage


def raw_fill(message_id: str = "1-0") -> StreamMessage:
    fill = FillMessage(
        ticker="AAPL",
        timestamp=datetime(2026, 7, 19, tzinfo=timezone.utc),
        side="buy",
        quantity=1,
        fill_price=100,
        commission=1,
        recommendation_id="rec-1",
        order_id="42",
        execution_id="e-1",
        account_id="DU12345",
        cumulative_quantity=1,
        portfolio="momentum",
        con_id=265598,
        exchange="SMART",
        currency="USD",
    )
    return StreamMessage(FILLS_STREAM, message_id, fill.to_stream_dict())


@pytest.fixture
def redis_client():
    redis = AsyncMock()
    redis.drain_pending.return_value = []
    return redis


@pytest.mark.asyncio
async def test_setup_drains_pending_and_applies_before_ack(redis_client):
    message = raw_fill()
    redis_client.drain_pending.return_value = [message]
    events = []
    projector = MagicMock()
    projector.apply.side_effect = lambda fill: events.append("apply") or True
    redis_client.ack.side_effect = lambda *args: events.append("ack")
    runner = PortfolioAccountingRunner(redis_client, projector)

    await runner.setup()

    redis_client.create_consumer_group.assert_awaited_once_with(
        FILLS_STREAM, CONSUMER_GROUP
    )
    redis_client.drain_pending.assert_awaited_once_with(
        FILLS_STREAM, CONSUMER_GROUP, CONSUMER_NAME
    )
    assert events == ["apply", "ack"]


@pytest.mark.asyncio
@pytest.mark.parametrize("applied", [True, False])
async def test_new_and_duplicate_fill_are_successfully_acked(redis_client, applied):
    projector = MagicMock()
    projector.apply.return_value = applied
    runner = PortfolioAccountingRunner(redis_client, projector)
    message = raw_fill()

    await runner.process_message(message)

    redis_client.ack.assert_awaited_once_with(
        FILLS_STREAM, CONSUMER_GROUP, message.message_id
    )
    redis_client.send_to_dead_letter.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message,error",
    [
        (StreamMessage(FILLS_STREAM, "bad-1", {"ticker": "AAPL"}), None),
        (raw_fill("bad-2"), UnattributedFillError("unknown intent")),
    ],
)
async def test_malformed_and_unattributed_fill_are_dlqd_and_acked(
    redis_client, message, error
):
    projector = MagicMock()
    if error is not None:
        projector.apply.side_effect = error
    runner = PortfolioAccountingRunner(redis_client, projector)

    await runner.process_message(message)

    redis_client.send_to_dead_letter.assert_awaited_once()
    redis_client.ack.assert_awaited_once_with(
        FILLS_STREAM, CONSUMER_GROUP, message.message_id
    )
    assert redis_client.mock_calls.index(
        call.send_to_dead_letter(FILLS_STREAM, message, ANY)
    ) < redis_client.mock_calls.index(
        call.ack(FILLS_STREAM, CONSUMER_GROUP, message.message_id)
    )


@pytest.mark.asyncio
async def test_unexpected_database_failure_remains_pending(redis_client):
    projector = MagicMock()
    projector.apply.side_effect = RuntimeError("database unavailable")
    runner = PortfolioAccountingRunner(redis_client, projector)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await runner.process_message(raw_fill())

    redis_client.send_to_dead_letter.assert_not_awaited()
    redis_client.ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_column_overflow_is_dlqd_and_acked(redis_client):
    """A DataError reaching the runner is a poison pill, not an outage.

    The projector converts an overflow raised by sleeve accounting into a
    FillProjectionError, so this is the backstop for one raised anywhere else
    in apply() -- e.g. by the execution_fills insert itself. Either way the
    message can never be stored, so it must be quarantined rather than retried
    until the process dies (KAN-61).
    """
    projector = MagicMock()
    projector.apply.side_effect = DataError(
        "INSERT INTO trades (...) VALUES (...)",
        {},
        Exception("value too long for type character varying(50)"),
    )
    runner = PortfolioAccountingRunner(redis_client, projector)
    message = raw_fill("overflow-1")

    await runner.process_message(message)

    redis_client.send_to_dead_letter.assert_awaited_once()
    redis_client.ack.assert_awaited_once_with(
        FILLS_STREAM, CONSUMER_GROUP, message.message_id
    )


@pytest.mark.asyncio
async def test_database_outage_is_not_mistaken_for_bad_data(redis_client):
    """OperationalError must stay pending. Quarantining it would lose good fills.

    This is the boundary that keeps the DataError catch honest: broadening it to
    SQLAlchemyError would dead-letter every fill in flight during a Postgres
    restart and silently drop them.
    """
    projector = MagicMock()
    projector.apply.side_effect = OperationalError(
        "SELECT 1", {}, Exception("server closed the connection unexpectedly")
    )
    runner = PortfolioAccountingRunner(redis_client, projector)

    with pytest.raises(OperationalError):
        await runner.process_message(raw_fill("outage-1"))

    redis_client.send_to_dead_letter.assert_not_awaited()
    redis_client.ack.assert_not_awaited()
