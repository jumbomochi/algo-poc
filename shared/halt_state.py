"""Durable halt-state repository over :class:`SystemHaltState`.

The kill switch is in-memory and per-process, so it fails OPEN on restart. This
repository persists a halt so the risk service can reload it on startup and stay
halted (fail-closed) until an explicit human clear.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.system_halt import SystemHaltState


class HaltStateRepository:
    """Transaction-neutral repository. The caller owns commit/rollback."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def load_active_halt(self, *, mode: str) -> SystemHaltState | None:
        """Return the current active halt for ``mode``, or None."""
        return self.session.scalar(
            select(SystemHaltState)
            .where(
                SystemHaltState.mode == mode,
                SystemHaltState.active.is_(True),
            )
            .order_by(SystemHaltState.activated_at.desc(), SystemHaltState.id.desc())
            .limit(1)
        )

    def list_halts(self, *, mode: str) -> list[SystemHaltState]:
        """All halt rows for ``mode``, newest first (audit / tests)."""
        return list(
            self.session.scalars(
                select(SystemHaltState)
                .where(SystemHaltState.mode == mode)
                .order_by(
                    SystemHaltState.activated_at.desc(), SystemHaltState.id.desc()
                )
            )
        )

    def record_halt(
        self,
        *,
        mode: str,
        source: str,
        reason: str,
        triggered_by: str,
        now: datetime,
    ) -> SystemHaltState:
        """Persist a halt. Idempotent: if a halt is already active for ``mode``
        the existing row is returned unchanged (a replayed kill must not stack
        duplicate rows), so the original activation wins."""
        existing = self.load_active_halt(mode=mode)
        if existing is not None:
            return existing
        halt = SystemHaltState(
            mode=mode,
            active=True,
            source=source,
            reason=reason,
            triggered_by=triggered_by,
            activated_at=now,
        )
        self.session.add(halt)
        self.session.flush()
        return halt

    def clear_halt(self, *, mode: str, cleared_by: str, now: datetime) -> bool:
        """Clear the active halt for ``mode``. Returns True if one was cleared."""
        halt = self.load_active_halt(mode=mode)
        if halt is None:
            return False
        halt.active = False
        halt.cleared_at = now
        halt.cleared_by = cleared_by
        self.session.flush()
        return True
