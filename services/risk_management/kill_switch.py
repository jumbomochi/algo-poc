from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from services.risk_management.engine import RiskDecision
from shared.halt_state import HaltStateRepository


class KillSwitch:
    """Emergency kill switch that immediately halts all trading.

    When active, all risk checks via ``check()`` return rejected decisions.
    Activation and deactivation are logged to the provided audit logger.

    When a ``halt_store`` is provided the switch is **durable**: activation is
    persisted so a restart reloads it (fail-closed), and it is cleared only by an
    explicit human action. Without a store it behaves as a pure in-memory switch.
    """

    def __init__(
        self,
        logger: Any,
        halt_store: HaltStateRepository | None = None,
        mode: str = "paper",
    ) -> None:
        self._logger = logger
        self._halt_store = halt_store
        self._mode = mode
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

    def activate(
        self,
        reason: str,
        triggered_by: str,
        *,
        source: str = "kill",
        now: datetime | None = None,
    ) -> None:
        """Activate the kill switch, halting all trading.

        Args:
            reason: Human-readable explanation for the activation.
            triggered_by: Identifier of the system or person that triggered it.
            source: ``"kill"`` (manual/API) or ``"circuit_breaker"`` (automated).
            now: activation timestamp (defaults to now); injectable for tests.
        """
        activated_at = now or datetime.now(timezone.utc)
        self._active = True
        self._activated_at = activated_at
        self._reason = reason
        self._triggered_by = triggered_by

        # Persist first: a kill that is not durable would fail OPEN on the next
        # restart. Commit immediately so the halt survives a crash mid-liquidation.
        if self._halt_store is not None:
            try:
                self._halt_store.record_halt(
                    mode=self._mode,
                    source=source,
                    reason=reason,
                    triggered_by=triggered_by,
                    now=activated_at,
                )
                self._halt_store.session.commit()
            except Exception:
                self._halt_store.session.rollback()
                self._logger.critical(
                    "Kill switch activated but halt could not be persisted",
                    reason=reason,
                    triggered_by=triggered_by,
                )

        self._logger.critical(
            "Kill switch activated",
            reason=reason,
            triggered_by=triggered_by,
            source=source,
            activated_at=activated_at.isoformat(),
        )

    def deactivate(self, cleared_by: str = "manual") -> None:
        """Deactivate the kill switch, allowing trading to resume.

        This is the explicit human clear. With a store it also clears the durable
        halt so a subsequent restart does not re-halt.
        """
        prev_reason = self._reason
        prev_triggered_by = self._triggered_by

        if self._halt_store is not None:
            try:
                self._halt_store.clear_halt(
                    mode=self._mode,
                    cleared_by=cleared_by,
                    now=datetime.now(timezone.utc),
                )
                self._halt_store.session.commit()
            except Exception:
                self._halt_store.session.rollback()
                self._logger.exception("Failed to clear persisted halt")

        self._active = False
        self._activated_at = None
        self._reason = None
        self._triggered_by = None

        self._logger.info(
            "Kill switch deactivated",
            previous_reason=prev_reason,
            previous_triggered_by=prev_triggered_by,
            cleared_by=cleared_by,
        )

    def reload_from_store(self) -> None:
        """Adopt the persisted halt on startup (fail-closed after a restart)."""
        if self._halt_store is None:
            return
        halt = self._halt_store.load_active_halt(mode=self._mode)
        self._halt_store.session.rollback()
        if halt is not None:
            self._active = True
            self._activated_at = halt.activated_at
            self._reason = halt.reason
            self._triggered_by = halt.triggered_by
            self._logger.critical(
                "Kill switch reloaded from durable halt — staying halted",
                reason=halt.reason,
                triggered_by=halt.triggered_by,
                source=halt.source,
            )

    def sync_from_store(self) -> None:
        """Reconcile in-memory state with the durable halt.

        Called on the periodic cadence so an out-of-band clear (the admin API
        endpoint) resumes trading, and an out-of-band halt is adopted. A restart
        is not required for either to take effect.
        """
        if self._halt_store is None:
            return
        halt = self._halt_store.load_active_halt(mode=self._mode)
        self._halt_store.session.rollback()
        if halt is not None and not self._active:
            self._active = True
            self._activated_at = halt.activated_at
            self._reason = halt.reason
            self._triggered_by = halt.triggered_by
            self._logger.critical(
                "Adopted out-of-band halt on re-sync",
                reason=halt.reason,
                triggered_by=halt.triggered_by,
            )
        elif halt is None and self._active:
            self._active = False
            self._activated_at = None
            self._reason = None
            self._triggered_by = None
            self._logger.info("Halt cleared out-of-band; resuming on re-sync")

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
