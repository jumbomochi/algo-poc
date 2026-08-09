from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class SystemHaltState(Base):
    """Durable kill-switch / circuit-breaker halt state.

    The in-memory :class:`KillSwitch` fails OPEN on restart. This table lets the
    risk service reload a halt on startup and stay halted until an explicit human
    clear. One row per halt incident: ``active=True`` while halted, flipped to
    ``active=False`` with ``cleared_at``/``cleared_by`` set when a human resumes.
    Scoped by ``mode`` so a paper halt never bleeds into live.
    """

    __tablename__ = "system_halt"
    __table_args__ = (
        Index("ix_system_halt_mode_active", "mode", "active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # kill | circuit_breaker
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(120), nullable=False)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    cleared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cleared_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
