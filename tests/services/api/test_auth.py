from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from services.api.app import create_app
from services.api.auth import _load_api_keys


@pytest.fixture()
def client():
    # Auth tests exercise authz, not stream plumbing — inject a mock Redis so
    # the kill endpoint doesn't 503 on the missing connection.
    app = create_app(redis_client=AsyncMock())
    return TestClient(app)


class TestUnauthenticatedAccess:
    def test_unauthenticated_request_returns_401(self, client):
        response = client.get("/api/v1/portfolio")
        assert response.status_code == 401

    def test_missing_api_key_returns_401(self, client):
        response = client.get("/api/v1/auth-check")
        assert response.status_code == 401

    def test_invalid_api_key_returns_401(self, client):
        response = client.get(
            "/api/v1/auth-check",
            headers={"X-API-Key": "bad-key"},
        )
        assert response.status_code == 401


class TestAuthenticatedAccess:
    def test_authenticated_request_succeeds(self, client):
        response = client.get(
            "/api/v1/auth-check",
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 200

    def test_auth_check_returns_user_info(self, client):
        response = client.get(
            "/api/v1/auth-check",
            headers={"X-API-Key": "test-key"},
        )
        data = response.json()
        assert data["role"] == "admin"
        # The raw API key must never be echoed back in the response.
        assert "api_key" not in data

    def test_viewer_key_authenticates(self, client):
        response = client.get(
            "/api/v1/auth-check",
            headers={"X-API-Key": "viewer-key"},
        )
        assert response.status_code == 200
        assert response.json()["role"] == "viewer"


class TestLiveModeGuard:
    """The dev-default API keys must never be reachable when the system is
    actually running in live mode — regardless of how "live" was decided.
    The mode must come from the validated AppConfig, not a raw env var, so a
    typo'd or missing ALGO_MODE can't silently leave dev keys active.
    """

    def test_live_mode_refuses_dev_defaults_when_no_api_keys_env(self, monkeypatch):
        monkeypatch.delenv("API_KEYS", raising=False)

        with pytest.raises(RuntimeError, match="API_KEYS must be set"):
            _load_api_keys(mode="live")

    def test_paper_mode_falls_back_to_dev_defaults(self, monkeypatch):
        monkeypatch.delenv("API_KEYS", raising=False)

        keys = _load_api_keys(mode="paper")

        assert keys["test-key"] == "admin"

    def test_live_mode_accepts_explicit_api_keys(self, monkeypatch):
        monkeypatch.setenv("API_KEYS", "prod-key:admin")

        keys = _load_api_keys(mode="live")

        assert keys == {"prod-key": "admin"}

    def test_mode_resolved_from_validated_config_not_raw_env_var(self, monkeypatch):
        """Regression: the old check read os.environ['ALGO_MODE'] directly.
        If mode=live was set in the *config file* (not the env var), the old
        code missed it entirely and would silently allow dev-default keys in
        an actually-live deployment. Resolution must go through AppConfig,
        proven here by faking AppConfig.mode="live" with ALGO_MODE unset.
        """
        monkeypatch.delenv("API_KEYS", raising=False)
        monkeypatch.delenv("ALGO_MODE", raising=False)
        fake_config = MagicMock(mode="live")

        with patch("services.api.auth.load_config", return_value=fake_config) as mock_load:
            with pytest.raises(RuntimeError, match="API_KEYS must be set"):
                _load_api_keys()

        mock_load.assert_called_once()


class TestRateLimitLockout:
    """An internet-reachable API guarding a kill switch must not allow an
    unbounded key-guessing loop. Repeated X-API-Key failures from the same
    client are locked out for a cooldown window.
    """

    def test_repeated_failures_are_locked_out(self, client):
        from services.api.auth import LOCKOUT_MAX_FAILURES

        for _ in range(LOCKOUT_MAX_FAILURES):
            response = client.get(
                "/api/v1/auth-check", headers={"X-API-Key": "bad-key"}
            )
            assert response.status_code == 401

        locked = client.get("/api/v1/auth-check", headers={"X-API-Key": "bad-key"})
        assert locked.status_code == 429

    def test_lockout_blocks_even_a_subsequently_valid_key(self, client):
        from services.api.auth import LOCKOUT_MAX_FAILURES

        for _ in range(LOCKOUT_MAX_FAILURES):
            client.get("/api/v1/auth-check", headers={"X-API-Key": "bad-key"})

        response = client.get(
            "/api/v1/auth-check", headers={"X-API-Key": "test-key"}
        )
        assert response.status_code == 429

    def test_successful_auth_resets_the_failure_count(self, client):
        from services.api.auth import LOCKOUT_MAX_FAILURES

        for _ in range(LOCKOUT_MAX_FAILURES - 1):
            client.get("/api/v1/auth-check", headers={"X-API-Key": "bad-key"})

        ok = client.get("/api/v1/auth-check", headers={"X-API-Key": "test-key"})
        assert ok.status_code == 200

        # The prior success cleared the failure count, so a single further
        # failure must not be enough to lock out.
        still_not_locked = client.get(
            "/api/v1/auth-check", headers={"X-API-Key": "bad-key"}
        )
        assert still_not_locked.status_code == 401


class TestRoleBasedAccess:
    def test_kill_switch_requires_admin_role(self, client):
        response = client.post(
            "/api/v1/kill",
            headers={"X-API-Key": "viewer-key"},
        )
        assert response.status_code == 403

    def test_kill_switch_allowed_for_admin(self, client):
        response = client.post(
            "/api/v1/kill",
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 200

    def test_operator_cannot_access_admin_endpoint(self, client):
        response = client.post(
            "/api/v1/kill",
            headers={"X-API-Key": "operator-key"},
        )
        assert response.status_code == 403
