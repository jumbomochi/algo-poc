from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class AlertRecord(Base):
    """One durable row per alert delivered to ``stream:alerts``.

    Alerts were fire-and-forget until KAN-42: published to the stream, fanned
    out to Slack/Telegram/email, and then gone.  The go-live checklist's
    reliability gate asks "were there unresolved critical alerts in the last
    14 days?", and a question with no stored answer is a gate that passes by
    ignorance — the exact failure this readiness work exists to remove.

    The notifications service writes a row *before* dispatching, so an alert
    that no channel could deliver is still on the record.  ``message_id`` is
    the Redis stream id and is unique: at-least-once delivery replays pending
    alerts after a crash, and a replay must not read as a second incident.

    ``resolved_at``/``resolved_by`` are operator-set (see
    ``docs/operations/go-live-checklist.md``); nothing resolves itself.
    """

    __tablename__ = "alert_records"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_alert_records_message_id"),
        Index("ix_alert_records_priority_raised", "priority", "raised_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: NOT NULL on purpose: Postgres lets a unique index hold unlimited NULLs,
    #: so a nullable message_id would silently disable the replay guard.
    message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: When the publisher says the condition occurred.
    raised_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: When this row was written — the recorder's own liveness evidence.
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
