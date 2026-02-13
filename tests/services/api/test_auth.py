from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.api.app import create_app


@pytest.fixture()
def client():
    app = create_app()
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
        assert "api_key" in data

    def test_viewer_key_authenticates(self, client):
        response = client.get(
            "/api/v1/auth-check",
            headers={"X-API-Key": "viewer-key"},
        )
        assert response.status_code == 200
        assert response.json()["role"] == "viewer"


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
