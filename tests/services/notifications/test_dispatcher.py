from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from services.notifications.dispatcher import NotificationDispatcher
from shared.schemas.messages import AlertMessage


def make_alert(
    event_type: str = "test_event",
    priority: str = "low",
    message: str = "test message",
) -> AlertMessage:
    return AlertMessage(
        timestamp=datetime.now(timezone.utc),
        event_type=event_type,
        priority=priority,
        message=message,
    )


@pytest.fixture()
def slack():
    return AsyncMock()


@pytest.fixture()
def email():
    return AsyncMock()


@pytest.fixture()
def sms():
    return AsyncMock()


@pytest.fixture()
def dispatcher(slack, email, sms):
    return NotificationDispatcher(slack=slack, email=email, sms=sms)


class TestNotificationDispatcher:
    @pytest.mark.asyncio
    async def test_critical_alert_sends_to_all_channels(self, dispatcher, slack, email, sms):
        """Critical alerts should be dispatched to Slack, Email, and SMS."""
        alert = make_alert(
            event_type="circuit_breaker",
            priority="critical",
            message="Portfolio drawdown exceeded 20%",
        )

        await dispatcher.dispatch(alert)

        slack.send.assert_called_once()
        email.send.assert_called_once()
        sms.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_high_priority_sends_slack_and_email(self, dispatcher, slack, email, sms):
        """High priority alerts should be dispatched to Slack and Email only."""
        alert = make_alert(
            event_type="risk_breach",
            priority="high",
            message="Position size limit breached for AAPL",
        )

        await dispatcher.dispatch(alert)

        slack.send.assert_called_once()
        email.send.assert_called_once()
        sms.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_medium_priority_sends_slack_and_email(self, dispatcher, slack, email, sms):
        """Medium priority alerts should be dispatched to Slack and Email only."""
        alert = make_alert(
            event_type="model_retrained",
            priority="medium",
            message="LightGBM model retrained with 5000 samples",
        )

        await dispatcher.dispatch(alert)

        slack.send.assert_called_once()
        email.send.assert_called_once()
        sms.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_priority_only_sends_slack(self, dispatcher, slack, email, sms):
        """Low priority alerts should be dispatched to Slack only."""
        alert = make_alert(
            event_type="trade_executed",
            priority="low",
            message="Bought 50 AAPL",
        )

        await dispatcher.dispatch(alert)

        slack.send.assert_called_once()
        email.send.assert_not_called()
        sms.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_passes_subject_and_body(self, dispatcher, slack, email, sms):
        """Dispatched messages should include the event type as subject and message as body."""
        alert = make_alert(
            event_type="circuit_breaker",
            priority="critical",
            message="Portfolio drawdown exceeded 20%",
        )

        await dispatcher.dispatch(alert)

        call_kwargs = slack.send.call_args
        subject = call_kwargs[1]["subject"] if call_kwargs[1] else call_kwargs[0][0]
        body = call_kwargs[1]["body"] if call_kwargs[1] else call_kwargs[0][1]
        assert "circuit_breaker" in subject
        assert "Portfolio drawdown exceeded 20%" in body

    @pytest.mark.asyncio
    async def test_channel_error_does_not_block_other_channels(
        self, slack, email, sms
    ):
        """If one channel raises, other channels should still be attempted."""
        slack.send.side_effect = RuntimeError("Slack is down")
        dispatcher = NotificationDispatcher(slack=slack, email=email, sms=sms)

        alert = make_alert(priority="critical", message="Something critical")

        # Should not raise
        await dispatcher.dispatch(alert)

        slack.send.assert_called_once()
        email.send.assert_called_once()
        sms.send.assert_called_once()
