from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app import create_app

ADMIN_HEADERS = {"X-API-Key": "test-key"}
VIEWER_HEADERS = {"X-API-Key": "viewer-key"}


@pytest.fixture()
def redis_client():
    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=b"1700000000000-0")
    return mock


@pytest.fixture()
def client(redis_client):
    app = create_app(redis_client=redis_client)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

class TestHealthzEndpoint:
    """T6: unauthenticated liveness probe for the container healthcheck."""

    def test_healthz_returns_200_without_auth(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_healthz_works_even_with_no_redis_client(self):
        app = create_app(redis_client=None)
        no_redis_client = TestClient(app)
        response = no_redis_client.get("/healthz")
        assert response.status_code == 200


class TestPortfolioEndpoint:
    def test_portfolio_returns_200_with_auth(self, client):
        response = client.get("/api/v1/portfolio", headers=ADMIN_HEADERS)
        assert response.status_code == 200

    def test_portfolio_response_structure(self, client):
        response = client.get("/api/v1/portfolio", headers=ADMIN_HEADERS)
        data = response.json()
        assert "positions" in data
        assert "nav" in data
        assert "exposure_pct" in data
        assert "margin_utilization_pct" in data
        assert "pnl" in data

    def test_portfolio_requires_auth(self, client):
        response = client.get("/api/v1/portfolio")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

class TestPositionsEndpoint:
    def test_positions_list_returns_200(self, client):
        response = client.get("/api/v1/positions", headers=ADMIN_HEADERS)
        assert response.status_code == 200

    def test_positions_list_returns_list(self, client):
        response = client.get("/api/v1/positions", headers=ADMIN_HEADERS)
        data = response.json()
        assert isinstance(data, list)

    def test_position_detail_returns_200(self, client):
        response = client.get("/api/v1/positions/AAPL", headers=ADMIN_HEADERS)
        assert response.status_code == 200

    def test_position_detail_contains_ticker(self, client):
        response = client.get("/api/v1/positions/AAPL", headers=ADMIN_HEADERS)
        data = response.json()
        assert data["ticker"] == "AAPL"

    def test_positions_requires_auth(self, client):
        response = client.get("/api/v1/positions")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------

class TestRiskEndpoint:
    def test_risk_status_returns_200(self, client):
        response = client.get("/api/v1/risk/status", headers=ADMIN_HEADERS)
        assert response.status_code == 200

    def test_risk_status_structure(self, client):
        response = client.get("/api/v1/risk/status", headers=ADMIN_HEADERS)
        data = response.json()
        assert "drawdown_pct" in data
        assert "margin_utilization_pct" in data
        assert "kill_switch_active" in data

    def test_risk_requires_auth(self, client):
        response = client.get("/api/v1/risk/status")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------

class TestActivityEndpoint:
    def test_trades_returns_200(self, client):
        response = client.get("/api/v1/activity/trades", headers=ADMIN_HEADERS)
        assert response.status_code == 200

    def test_trades_returns_list(self, client):
        response = client.get("/api/v1/activity/trades", headers=ADMIN_HEADERS)
        data = response.json()
        assert isinstance(data, list)

    def test_audit_returns_200(self, client):
        response = client.get("/api/v1/activity/audit", headers=ADMIN_HEADERS)
        assert response.status_code == 200

    def test_audit_returns_list(self, client):
        response = client.get("/api/v1/activity/audit", headers=ADMIN_HEADERS)
        data = response.json()
        assert isinstance(data, list)

    def test_activity_requires_auth(self, client):
        response = client.get("/api/v1/activity/trades")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Kill
# ---------------------------------------------------------------------------

class TestKillEndpoint:
    def test_kill_returns_200_for_admin(self, client):
        response = client.post("/api/v1/kill", headers=ADMIN_HEADERS)
        assert response.status_code == 200

    def test_kill_returns_403_for_viewer(self, client):
        response = client.post("/api/v1/kill", headers=VIEWER_HEADERS)
        assert response.status_code == 403

    def test_kill_response_structure(self, client):
        response = client.post("/api/v1/kill", headers=ADMIN_HEADERS)
        data = response.json()
        assert data["status"] == "triggered"
        assert "triggered_by" in data
        assert "timestamp" in data
        assert "message_id" in data

    def test_kill_requires_auth(self, client):
        response = client.post("/api/v1/kill")
        assert response.status_code == 401

    def test_kill_publishes_kill_message_to_stream(self, client, redis_client):
        """The whole point of the endpoint: a KillMessage must land on stream:kill."""
        response = client.post(
            "/api/v1/kill",
            headers=ADMIN_HEADERS,
            json={"reason": "rollback: test incident"},
        )
        assert response.status_code == 200

        redis_client.publish.assert_awaited_once()
        stream, payload = redis_client.publish.call_args.args
        assert stream == "stream:kill"
        assert payload["reason"] == "rollback: test incident"
        assert payload["triggered_by"].endswith("***")
        assert "timestamp" in payload

    def test_kill_default_reason_without_body(self, client, redis_client):
        client.post("/api/v1/kill", headers=ADMIN_HEADERS)
        _, payload = redis_client.publish.call_args.args
        assert payload["reason"] == "manual kill via API"

    def test_kill_returns_503_when_redis_missing(self):
        """A kill that cannot reach the stream must not report success."""
        app = create_app(redis_client=None)
        no_redis_client = TestClient(app)
        response = no_redis_client.post("/api/v1/kill", headers=ADMIN_HEADERS)
        assert response.status_code == 503

    def test_kill_returns_503_when_publish_fails(self, redis_client):
        redis_client.publish.side_effect = ConnectionError("redis down")
        app = create_app(redis_client=redis_client)
        failing_client = TestClient(app)
        response = failing_client.post("/api/v1/kill", headers=ADMIN_HEADERS)
        assert response.status_code == 503


# ---------------------------------------------------------------------------
# ML
# ---------------------------------------------------------------------------

class TestMLEndpoint:
    def test_model_returns_200(self, client):
        response = client.get("/api/v1/ml/model", headers=ADMIN_HEADERS)
        assert response.status_code == 200

    def test_model_response_structure(self, client):
        response = client.get("/api/v1/ml/model", headers=ADMIN_HEADERS)
        data = response.json()
        assert "model_version" in data
        assert "metrics" in data

    def test_ml_requires_auth(self, client):
        response = client.get("/api/v1/ml/model")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

class TestBacktestEndpoint:
    def test_backtest_results_returns_200(self, client):
        response = client.get("/api/v1/backtest/results", headers=ADMIN_HEADERS)
        assert response.status_code == 200

    def test_backtest_results_structure(self, client):
        response = client.get("/api/v1/backtest/results", headers=ADMIN_HEADERS)
        data = response.json()
        assert "last_run" in data
        assert "sharpe_ratio" in data
        assert "total_return_pct" in data
        assert "max_drawdown_pct" in data

    def test_backtest_requires_auth(self, client):
        response = client.get("/api/v1/backtest/results")
        assert response.status_code == 401
