from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from services.api.app import create_app


@pytest.fixture()
def redis_client():
    return AsyncMock()


class TestInteractiveDocsGating:
    """Interactive API docs (Swagger UI, ReDoc, the raw OpenAPI schema) leak
    the full route/schema surface to anyone who can reach the API. They must
    not be exposed in live mode.
    """

    def test_docs_enabled_in_paper_mode(self, redis_client):
        app = create_app(redis_client=redis_client, mode="paper")
        client = TestClient(app)

        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200

    def test_docs_disabled_in_live_mode(self, redis_client):
        app = create_app(redis_client=redis_client, mode="live")
        client = TestClient(app)

        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404

    def test_default_mode_resolves_from_validated_config(self, redis_client):
        """No explicit mode passed: create_app must resolve it the same way
        auth.py does (via AppConfig), not default to permissive."""
        fake_config = MagicMock(mode="live")

        with patch("services.api.auth.load_config", return_value=fake_config):
            app = create_app(redis_client=redis_client)
            client = TestClient(app)

        assert client.get("/docs").status_code == 404
