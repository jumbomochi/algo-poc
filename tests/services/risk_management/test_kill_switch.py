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
