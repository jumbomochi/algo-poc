from __future__ import annotations

from typing import Protocol, runtime_checkable

from shared.logging import get_logger

logger = get_logger("notifications.channels")


@runtime_checkable
class NotificationChannelProtocol(Protocol):
    """Protocol defining the interface for notification channels."""

    async def send(self, subject: str, body: str) -> None: ...


class SlackChannel:
    """Slack notification channel (stub implementation)."""

    async def send(self, subject: str, body: str) -> None:
        logger.info("Slack notification sent", subject=subject)


class EmailChannel:
    """Email notification channel (stub implementation)."""

    async def send(self, subject: str, body: str) -> None:
        logger.info("Email notification sent", subject=subject)


class SMSChannel:
    """SMS notification channel (stub implementation)."""

    async def send(self, subject: str, body: str) -> None:
        logger.info("SMS notification sent", subject=subject)
