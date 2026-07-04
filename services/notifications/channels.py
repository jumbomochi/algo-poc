from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

import httpx

from shared.logging import get_logger

logger = get_logger("notifications.channels")

TELEGRAM_API_BASE = "https://api.telegram.org"
# Telegram hard limit is 4096 chars per message; leave headroom for the subject line.
TELEGRAM_MAX_BODY = 3900


@runtime_checkable
class NotificationChannelProtocol(Protocol):
    """Protocol defining the interface for notification channels."""

    async def send(self, subject: str, body: str) -> None: ...


class TelegramChannel:
    """Telegram bot notification channel.

    Sends alerts as plain-text messages via the Bot API ``sendMessage``
    endpoint. Credentials come from the environment (never config files):

    - ``TELEGRAM_BOT_TOKEN`` — from @BotFather
    - ``TELEGRAM_CHAT_ID``   — the chat to deliver to

    Raises on delivery failure so the dispatcher's per-channel error
    handling logs it (and other channels still receive the alert).
    """

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self._timeout = timeout_seconds
        if not self.is_configured:
            logger.warning(
                "Telegram channel constructed without credentials; sends will fail",
            )

    @property
    def is_configured(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    async def send(self, subject: str, body: str) -> None:
        if not self.is_configured:
            raise RuntimeError(
                "TelegramChannel missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID"
            )
        text = f"{subject}\n\n{body[:TELEGRAM_MAX_BODY]}"
        url = f"{TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                url,
                json={"chat_id": self._chat_id, "text": text},
            )
            response.raise_for_status()
        logger.info("Telegram notification sent", subject=subject)


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
