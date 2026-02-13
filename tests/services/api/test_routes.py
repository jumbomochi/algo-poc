from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.api.app import create_app

ADMIN_HEADERS = {"X-API-Key": "test-key"}
VIEWER_HEADERS = {"X-API-Key": "viewer-key"}


@pytest.fixture()
def client():
    app = create_app()
    return TestClient(app)


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

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

    def test_kill_requires_auth(self, client):
        response = client.post("/api/v1/kill")
        assert response.status_code == 401


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
