from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from services.risk_management.kill_switch import KillSwitch


@pytest.fixture()
def mock_logger():
    return MagicMock()


class TestKillSwitch:
    def test_initially_inactive(self, mock_logger):
        ks = KillSwitch(logger=mock_logger)
        assert ks.is_active is False

    def test_activate_sets_active(self, mock_logger):
        ks = KillSwitch(logger=mock_logger)
        ks.activate(reason="margin call", triggered_by="risk_engine")
        assert ks.is_active is True

    def test_activate_records_time(self, mock_logger):
        ks = KillSwitch(logger=mock_logger)
        ks.activate(reason="margin call", triggered_by="risk_engine")
        assert ks.activated_at is not None
        assert isinstance(ks.activated_at, datetime)

    def test_activate_records_reason_and_trigger(self, mock_logger):
        ks = KillSwitch(logger=mock_logger)
        ks.activate(reason="margin call", triggered_by="risk_engine")
        assert ks.reason == "margin call"
        assert ks.triggered_by == "risk_engine"

    def test_activate_logs_to_audit_trail(self, mock_logger):
        ks = KillSwitch(logger=mock_logger)
        ks.activate(reason="margin call", triggered_by="risk_engine")
        mock_logger.critical.assert_called_once()
        call_args = mock_logger.critical.call_args
        assert "kill switch" in call_args[0][0].lower() or "kill" in str(call_args).lower()

    def test_deactivate_resets_state(self, mock_logger):
        ks = KillSwitch(logger=mock_logger)
        ks.activate(reason="margin call", triggered_by="risk_engine")
        assert ks.is_active is True

        ks.deactivate()
        assert ks.is_active is False
        assert ks.activated_at is None
        assert ks.reason is None
        assert ks.triggered_by is None

    def test_deactivate_logs(self, mock_logger):
        ks = KillSwitch(logger=mock_logger)
        ks.activate(reason="test", triggered_by="admin")
        ks.deactivate()
        # Should have logged both activate and deactivate
        assert mock_logger.critical.call_count >= 1
        assert mock_logger.info.call_count >= 1 or mock_logger.warning.call_count >= 1

    def test_check_when_active_rejects(self, mock_logger):
        ks = KillSwitch(logger=mock_logger)
        ks.activate(reason="margin call", triggered_by="risk_engine")
        decision = ks.check()
        assert decision.approved is False
        assert "kill switch" in decision.reason.lower()

    def test_check_when_inactive_approves(self, mock_logger):
        ks = KillSwitch(logger=mock_logger)
        decision = ks.check()
        assert decision.approved is True

    def test_multiple_activations_keep_latest(self, mock_logger):
        ks = KillSwitch(logger=mock_logger)
        ks.activate(reason="first reason", triggered_by="system_a")
        ks.activate(reason="second reason", triggered_by="system_b")
        assert ks.is_active is True
        assert ks.reason == "second reason"
        assert ks.triggered_by == "system_b"


class TestKillSwitchPersistence:
    """The kill switch must survive a restart (fail-closed) by persisting to the
    durable halt table (review finding 1.1)."""

    @pytest.fixture()
    def store(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from shared.halt_state import HaltStateRepository
        from shared.models import Base

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        return HaltStateRepository(Session(engine))

    def test_activate_persists_halt(self, mock_logger, store):
        ks = KillSwitch(logger=mock_logger, halt_store=store, mode="paper")
        ks.activate(reason="margin call", triggered_by="risk_engine")

        halt = store.load_active_halt(mode="paper")
        assert halt is not None
        assert halt.active is True
        assert halt.reason == "margin call"
        assert halt.source == "kill"

    def test_reload_from_store_stays_halted_after_restart(self, mock_logger, store):
        # First process activates and persists.
        KillSwitch(logger=mock_logger, halt_store=store, mode="paper").activate(
            reason="breaker", triggered_by="risk_engine"
        )
        # A fresh process (new in-memory switch) reloads the persisted halt.
        restarted = KillSwitch(logger=mock_logger, halt_store=store, mode="paper")
        assert restarted.is_active is False  # not yet reloaded
        restarted.reload_from_store()
        assert restarted.is_active is True
        assert restarted.check().approved is False

    def test_deactivate_clears_persisted_halt(self, mock_logger, store):
        ks = KillSwitch(logger=mock_logger, halt_store=store, mode="paper")
        ks.activate(reason="r", triggered_by="t")
        ks.deactivate(cleared_by="admin***")

        assert ks.is_active is False
        assert store.load_active_halt(mode="paper") is None

    def test_sync_picks_up_external_clear(self, mock_logger, store):
        """The clear API endpoint writes the DB; the running risk process must
        pick that up on its periodic re-sync and resume."""
        ks = KillSwitch(logger=mock_logger, halt_store=store, mode="paper")
        ks.activate(reason="r", triggered_by="t")
        assert ks.is_active is True

        # Simulate the admin endpoint clearing the halt out-of-band.
        store.clear_halt(mode="paper", cleared_by="admin***", now=datetime.now(timezone.utc))
        store.session.commit()

        ks.sync_from_store()
        assert ks.is_active is False

    def test_sync_picks_up_external_halt(self, mock_logger, store):
        """A halt persisted by another actor is adopted on re-sync (fail-closed)."""
        ks = KillSwitch(logger=mock_logger, halt_store=store, mode="paper")
        store.record_halt(
            mode="paper",
            source="kill",
            reason="external",
            triggered_by="other",
            now=datetime.now(timezone.utc),
        )
        store.session.commit()

        ks.sync_from_store()
        assert ks.is_active is True
