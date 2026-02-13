from __future__ import annotations

from services.notifications.channels import NotificationChannelProtocol
from shared.logging import get_logger
from shared.schemas.messages import AlertMessage

logger = get_logger("notifications.dispatcher")

# Priority -> list of channel attribute names to send to
PRIORITY_ROUTING: dict[str, list[str]] = {
    "critical": ["slack", "email", "sms"],
    "high": ["slack", "email"],
    "medium": ["slack", "email"],
    "low": ["slack"],
}


class NotificationDispatcher:
    """Routes alert messages to appropriate notification channels based on priority.

    Routing rules:
        - ``critical``: Slack + Email + SMS
        - ``high``: Slack + Email
        - ``medium``: Slack + Email
        - ``low``: Slack only
    """

    def __init__(
        self,
        slack: NotificationChannelProtocol,
        email: NotificationChannelProtocol,
        sms: NotificationChannelProtocol,
    ) -> None:
        self._channels: dict[str, NotificationChannelProtocol] = {
            "slack": slack,
            "email": email,
            "sms": sms,
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
