from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from shared.halt_state import HaltStateRepository
from shared.models import Base


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _now() -> datetime:
    return datetime(2026, 8, 7, tzinfo=timezone.utc)


class TestHaltStatePersistence:
    def test_record_then_load_returns_active_halt(self, session):
        repo = HaltStateRepository(session)
        repo.record_halt(
            mode="paper",
            source="kill",
            reason="manual kill",
            triggered_by="oper***",
            now=_now(),
        )
        session.commit()

        halt = repo.load_active_halt(mode="paper")
        assert halt is not None
        assert halt.active is True
        assert halt.source == "kill"
        assert halt.reason == "manual kill"

    def test_no_active_halt_returns_none(self, session):
        repo = HaltStateRepository(session)
        assert repo.load_active_halt(mode="paper") is None

    def test_record_is_idempotent_while_active(self, session):
        """A replayed kill must not stack duplicate active halt rows."""
        repo = HaltStateRepository(session)
        repo.record_halt(
            mode="paper", source="kill", reason="r", triggered_by="t", now=_now()
        )
        repo.record_halt(
            mode="paper", source="kill", reason="r again", triggered_by="t", now=_now()
        )
        session.commit()

        halts = repo.list_halts(mode="paper")
        assert len(halts) == 1
        # The original reason is preserved — the first activation wins.
        assert halts[0].reason == "r"

    def test_clear_marks_halt_inactive(self, session):
        repo = HaltStateRepository(session)
        repo.record_halt(
            mode="paper", source="kill", reason="r", triggered_by="t", now=_now()
        )
        session.commit()

        cleared = repo.clear_halt(mode="paper", cleared_by="admin***", now=_now())
        session.commit()

        assert cleared is True
        assert repo.load_active_halt(mode="paper") is None
        row = repo.list_halts(mode="paper")[0]
        assert row.active is False
        assert row.cleared_by == "admin***"
        assert row.cleared_at is not None

    def test_clear_with_no_active_halt_is_noop(self, session):
        repo = HaltStateRepository(session)
        assert repo.clear_halt(mode="paper", cleared_by="admin***", now=_now()) is False

    def test_halt_is_scoped_by_mode(self, session):
        repo = HaltStateRepository(session)
        repo.record_halt(
            mode="live", source="kill", reason="r", triggered_by="t", now=_now()
        )
        session.commit()
        assert repo.load_active_halt(mode="paper") is None
        assert repo.load_active_halt(mode="live") is not None
