from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from services.api.app import create_app
from services.api.auth import _LockoutTracker, _load_api_keys


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
    unbounded key-guessing loop against one specific wrong key — but for a
    real-money system, availability of the kill endpoint outranks
    brute-force resistance. A VALID key must never be blocked by lockout
    state, no matter how many prior failures came from the same address;
    only repeated failures with the *same* invalid key accumulate.
    """

    def test_repeated_failures_with_the_same_wrong_key_are_locked_out(self, client):
        from services.api.auth import LOCKOUT_MAX_FAILURES

        for _ in range(LOCKOUT_MAX_FAILURES):
            response = client.get(
                "/api/v1/auth-check", headers={"X-API-Key": "bad-key"}
            )
            assert response.status_code == 401

        locked = client.get("/api/v1/auth-check", headers={"X-API-Key": "bad-key"})
        assert locked.status_code == 429

    def test_valid_key_never_locked_out_even_after_other_failures_from_same_ip(
        self, client
    ):
        """Regression: lockout must gate only failures, never a request that
        presents a genuinely valid key — the kill switch (admin-gated by
        this same key) must stay reachable for the operator.
        """
        from services.api.auth import LOCKOUT_MAX_FAILURES

        for _ in range(LOCKOUT_MAX_FAILURES * 3):
            client.get("/api/v1/auth-check", headers={"X-API-Key": "bad-key"})

        response = client.get(
            "/api/v1/auth-check", headers={"X-API-Key": "test-key"}
        )
        assert response.status_code == 200

    def test_missing_header_requests_do_not_count_toward_lockout(self, client):
        """A request with no X-API-Key at all isn't a guessing attempt —
        it must not consume the failure budget for a real key guess.
        """
        from services.api.auth import LOCKOUT_MAX_FAILURES

        for _ in range(LOCKOUT_MAX_FAILURES * 3):
            response = client.get("/api/v1/auth-check")
            assert response.status_code == 401

        # A genuine wrong-key guess right after must still get a plain 401,
        # not an immediate 429 from budget the header-less requests used up.
        first_guess = client.get(
            "/api/v1/auth-check", headers={"X-API-Key": "bad-key"}
        )
        assert first_guess.status_code == 401

    def test_varied_wrong_keys_from_same_address_share_one_bucket(self, client):
        """Lockout is keyed on the resolved client address ALONE, not
        (address, key-prefix). A credential-guessing tool varies the
        guessed key on every attempt — a per-prefix bucket would hand it a
        fresh 5-failure budget on every guess and never actually lock it
        out. The proxy-sharing hazard that motivated a per-key bucket is
        already handled by trusting X-Forwarded-For only from a configured,
        trusted proxy (API_FORWARDED_ALLOW_IPS) — see
        docs/operations/api-security.md. IP rotation by the attacker itself
        remains a residual risk, documented rather than "solved" here.
        """
        from services.api.auth import LOCKOUT_MAX_FAILURES

        for i in range(LOCKOUT_MAX_FAILURES):
            response = client.get(
                "/api/v1/auth-check", headers={"X-API-Key": f"wrong-guess-{i}"}
            )
            assert response.status_code == 401

        # A yet another distinct wrong key from the same address must now
        # be locked out too — the budget is shared per-address.
        locked = client.get(
            "/api/v1/auth-check", headers={"X-API-Key": "yet-another-wrong-key"}
        )
        assert locked.status_code == 429


class TestLockoutTrackerBounded:
    """_LockoutTracker is keyed by an attacker-controlled string (the
    resolved client address) with no natural expiry from the caller — it
    must not grow without bound, or a slow/wide attack becomes its own
    memory-exhaustion vector. Tested directly against the class (with an
    injected fake clock, so no real sleeping) rather than through HTTP: the
    property under test is the tracker's internal size, which isn't
    observable through a response status code.
    """

    def test_stale_buckets_are_swept_after_the_window_expires(self):
        now = [0.0]
        tracker = _LockoutTracker(
            max_failures=5, window_seconds=10.0, lockout_seconds=30.0,
            now_fn=lambda: now[0],
        )

        tracker.record_failure("bucket-a")
        assert len(tracker._failures) == 1

        # Long past the window, never enough failures to lock out: the next
        # record_failure for an unrelated bucket must sweep the stale one.
        now[0] = 100.0
        tracker.record_failure("bucket-b")

        assert "bucket-a" not in tracker._failures
        assert "bucket-b" in tracker._failures

    def test_active_lockout_is_not_swept_before_it_expires(self):
        now = [0.0]
        tracker = _LockoutTracker(
            max_failures=2, window_seconds=10.0, lockout_seconds=30.0,
            now_fn=lambda: now[0],
        )

        tracker.record_failure("bucket-a")
        tracker.record_failure("bucket-a")  # crosses max_failures -> locked
        assert tracker.is_locked_out("bucket-a") is True

        now[0] = 15.0  # past the failure window, still inside the lockout
        tracker.record_failure("bucket-b")

        assert tracker.is_locked_out("bucket-a") is True

    def test_tracked_bucket_count_is_capped(self):
        tracker = _LockoutTracker(
            max_failures=5, window_seconds=60.0, lockout_seconds=300.0,
            max_tracked_keys=10,
        )

        for i in range(1000):
            tracker.record_failure(f"attacker-{i}")

        assert len(tracker._failures) <= 10

    def test_capacity_eviction_drops_oldest_bucket_first(self):
        now = [0.0]
        tracker = _LockoutTracker(
            max_failures=5, window_seconds=60.0, lockout_seconds=300.0,
            max_tracked_keys=2, now_fn=lambda: now[0],
        )

        tracker.record_failure("first")
        now[0] += 1.0
        tracker.record_failure("second")
        now[0] += 1.0
        tracker.record_failure("third")  # over capacity -> evicts "first"

        assert "first" not in tracker._failures
        assert "second" in tracker._failures

    def test_capacity_eviction_never_lifts_an_active_lockout(self):
        """Regression: eviction must skip a currently-locked-out bucket.
        Popping it would also drop its _locked_until entry, letting a
        genuinely locked-out attacker regain access early just because the
        tracker happened to be near capacity at the wrong moment.
        """
        now = [0.0]
        tracker = _LockoutTracker(
            max_failures=2, window_seconds=60.0, lockout_seconds=300.0,
            max_tracked_keys=2, now_fn=lambda: now[0],
        )

        tracker.record_failure("attacker")
        tracker.record_failure("attacker")  # crosses max_failures -> locked
        assert tracker.is_locked_out("attacker") is True

        # Push the tracker over capacity with new buckets. "attacker" is
        # the oldest entry but must never be the one evicted while locked.
        now[0] += 1.0
        tracker.record_failure("b")
        now[0] += 1.0
        tracker.record_failure("c")

        assert tracker.is_locked_out("attacker") is True


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
