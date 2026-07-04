from __future__ import annotations

from services.notifications.channels import NotificationChannelProtocol
from shared.logging import get_logger
from shared.schemas.messages import AlertMessage

logger = get_logger("notifications.dispatcher")

# Priority -> list of channel attribute names to send to.
# Telegram is the primary operator channel and receives every priority.
PRIORITY_ROUTING: dict[str, list[str]] = {
    "critical": ["telegram", "slack", "email", "sms"],
    "high": ["telegram", "slack", "email"],
    "medium": ["telegram", "slack", "email"],
    "low": ["telegram", "slack"],
}


class NotificationDispatcher:
    """Routes alert messages to appropriate notification channels based on priority.

    Routing rules:
        - ``critical``: Telegram + Slack + Email + SMS
        - ``high``/``medium``: Telegram + Slack + Email
        - ``low``: Telegram + Slack

    Channels passed as ``None`` (disabled) are skipped silently.
    """

    def __init__(
        self,
        slack: NotificationChannelProtocol | None = None,
        email: NotificationChannelProtocol | None = None,
        sms: NotificationChannelProtocol | None = None,
        telegram: NotificationChannelProtocol | None = None,
    ) -> None:
        self._channels: dict[str, NotificationChannelProtocol | None] = {
            "slack": slack,
            "email": email,
            "sms": sms,
            "telegram": telegram,
        }

    async def dispatch(self, alert: AlertMessage) -> None:
        """Dispatch an alert to the appropriate channels based on its priority.

        Each channel is called independently; a failure in one channel does not
        prevent delivery to others.

        Args:
            alert: The alert message to dispatch.
        """
        channel_names = PRIORITY_ROUTING.get(alert.priority, ["slack"])
        subject = f"[{alert.priority.upper()}] {alert.event_type}"
        body = alert.message

        for name in channel_names:
            channel = self._channels.get(name)
            if channel is None:
                continue
            try:
                await channel.send(subject=subject, body=body)
            except Exception:
                logger.exception(
                    "Failed to send notification",
                    channel=name,
                    event_type=alert.event_type,
                    priority=alert.priority,
                )
