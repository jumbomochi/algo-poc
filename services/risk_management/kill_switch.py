from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from services.risk_management.engine import RiskDecision


class KillSwitch:
    """Emergency kill switch that immediately halts all trading.

    When active, all risk checks via ``check()`` return rejected decisions.
    Activation and deactivation are logged to the provided audit logger.
    """

    def __init__(self, logger: Any) -> None:
        self._logger = logger
        self._active: bool = False
        self._activated_at: datetime | None = None
        self._reason: str | None = None
        self._triggered_by: str | None = None

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def activated_at(self) -> datetime | None:
        return self._activated_at

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def triggered_by(self) -> str | None:
        return self._triggered_by

    def activate(self, reason: str, triggered_by: str) -> None:
        """Activate the kill switch, halting all trading.

        Args:
            reason: Human-readable explanation for the activation.
            triggered_by: Identifier of the system or person that triggered it.
        """
        self._active = True
        self._activated_at = datetime.now(timezone.utc)
        self._reason = reason
        self._triggered_by = triggered_by

        self._logger.critical(
            "Kill switch activated",
            reason=reason,
            triggered_by=triggered_by,
            activated_at=self._activated_at.isoformat(),
        )

    def deactivate(self) -> None:
        """Deactivate the kill switch, allowing trading to resume."""
        prev_reason = self._reason
        prev_triggered_by = self._triggered_by

        self._active = False
        self._activated_at = None
        self._reason = None
        self._triggered_by = None

        self._logger.info(
            "Kill switch deactivated",
            previous_reason=prev_reason,
            previous_triggered_by=prev_triggered_by,
        )

    def check(self) -> RiskDecision:
        """Check whether the kill switch is active.

        Returns:
            RiskDecision with approved=False if active, True otherwise.
        """
        if self._active:
            return RiskDecision(
                approved=False,
                reason=f"Kill switch active: {self._reason}",
                adjusted_quantity=0,
            )
        return RiskDecision(
            approved=True,
            reason="Kill switch inactive",
            adjusted_quantity=0,
        )
