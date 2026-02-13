from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from scripts.ops.go_live_gate import GoLiveGateChecker, GateResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_passing_data_source() -> MagicMock:
    """Return a mock data source where every gate passes by default."""
    ds = MagicMock()
    ds.get_paper_start_date.return_value = datetime.now(timezone.utc) - timedelta(days=90)
    ds.get_circuit_breaker_events.return_value = []
    ds.get_max_drawdown.return_value = 0.08
    ds.get_median_slippage_bps.return_value = 15.0
    ds.get_failed_order_rate.return_value = 0.005
    ds.get_critical_alerts_count.return_value = 0
    ds.get_latest_reconciliation_status.return_value = "ok"
    ds.get_model_status.return_value = "approved"
    ds.get_backtest_metrics.return_value = {
        "sharpe": 1.5,
        "max_drawdown": 0.10,
        "win_rate": 0.55,
    }
    return ds


# ---------------------------------------------------------------------------
# Gate-result dataclass
# ---------------------------------------------------------------------------

class TestGateResult:
    def test_gate_result_fields(self):
        r = GateResult(name="test", passed=True, message="ok", details={"k": 1})
        assert r.name == "test"
        assert r.passed is True
        assert r.message == "ok"
        assert r.details == {"k": 1}

    def test_gate_result_default_details(self):
        r = GateResult(name="x", passed=False, message="fail")
        assert r.details == {}


# ---------------------------------------------------------------------------
# All gates pass
# ---------------------------------------------------------------------------

class TestAllGatesPass:
    def test_all_gates_pass(self):
        ds = _make_passing_data_source()
        checker = GoLiveGateChecker(data_source=ds)
        assert checker.is_ready_for_live() is True

    def test_run_all_gates_returns_eight_results(self):
        ds = _make_passing_data_source()
        checker = GoLiveGateChecker(data_source=ds)
        results = checker.run_all_gates()
        assert len(results) == 8
        assert all(r.passed for r in results)


# ---------------------------------------------------------------------------
# Gate 1 — paper duration
# ---------------------------------------------------------------------------

class TestPaperDuration:
    def test_insufficient_paper_duration_fails(self):
        ds = _make_passing_data_source()
        ds.get_paper_start_date.return_value = datetime.now(timezone.utc) - timedelta(days=30)
        checker = GoLiveGateChecker(data_source=ds)
        results = checker.run_all_gates()
        paper = next(r for r in results if r.name == "paper_duration")
        assert paper.passed is False

    def test_exact_threshold_passes(self):
        ds = _make_passing_data_source()
        ds.get_paper_start_date.return_value = datetime.now(timezone.utc) - timedelta(days=60)
        checker = GoLiveGateChecker(data_source=ds)
        result = checker.check_paper_duration()
        assert result.passed is True

    def test_custom_threshold(self):
        ds = _make_passing_data_source()
        ds.get_paper_start_date.return_value = datetime.now(timezone.utc) - timedelta(days=45)
        checker = GoLiveGateChecker(data_source=ds, thresholds={"min_paper_days": 30})
        result = checker.check_paper_duration()
        assert result.passed is True


# ---------------------------------------------------------------------------
# Gate 2 — risk stability (circuit breaker)
# ---------------------------------------------------------------------------

class TestRiskStability:
    def test_circuit_breaker_event_fails(self):
        ds = _make_passing_data_source()
        ds.get_circuit_breaker_events.return_value = [{"date": "2026-02-01"}]
        checker = GoLiveGateChecker(data_source=ds)
        results = checker.run_all_gates()
        risk = next(r for r in results if r.name == "risk_stability")
        assert risk.passed is False

    def test_no_events_passes(self):
        ds = _make_passing_data_source()
        checker = GoLiveGateChecker(data_source=ds)
        result = checker.check_risk_stability()
        assert result.passed is True


# ---------------------------------------------------------------------------
# Gate 3 — drawdown
# ---------------------------------------------------------------------------

class TestDrawdown:
    def test_drawdown_exceeds_threshold_fails(self):
        ds = _make_passing_data_source()
        ds.get_max_drawdown.return_value = 0.15
        checker = GoLiveGateChecker(data_source=ds)
        results = checker.run_all_gates()
        dd = next(r for r in results if r.name == "drawdown_bound")
        assert dd.passed is False

    def test_drawdown_at_threshold_passes(self):
        ds = _make_passing_data_source()
        ds.get_max_drawdown.return_value = 0.12
        checker = GoLiveGateChecker(data_source=ds)
        result = checker.check_drawdown()
        assert result.passed is True

    def test_custom_drawdown_threshold(self):
        ds = _make_passing_data_source()
        ds.get_max_drawdown.return_value = 0.10
        checker = GoLiveGateChecker(
            data_source=ds, thresholds={"max_drawdown_pct": 0.08}
        )
        result = checker.check_drawdown()
        assert result.passed is False


# ---------------------------------------------------------------------------
# Gate 4 — execution quality
# ---------------------------------------------------------------------------

class TestExecutionQuality:
    def test_slippage_breach_fails(self):
        ds = _make_passing_data_source()
        ds.get_median_slippage_bps.return_value = 25.0
        checker = GoLiveGateChecker(data_source=ds)
        result = checker.check_execution_quality()
        assert result.passed is False
        assert "slippage" in result.message

    def test_failed_order_rate_breach_fails(self):
        ds = _make_passing_data_source()
        ds.get_failed_order_rate.return_value = 0.02
        checker = GoLiveGateChecker(data_source=ds)
        result = checker.check_execution_quality()
        assert result.passed is False
        assert "failed-order" in result.message

    def test_both_within_tolerance_passes(self):
        ds = _make_passing_data_source()
        checker = GoLiveGateChecker(data_source=ds)
        result = checker.check_execution_quality()
        assert result.passed is True


# ---------------------------------------------------------------------------
# Gate 5 — reliability
# ---------------------------------------------------------------------------

class TestReliability:
    def test_critical_alerts_fails(self):
        ds = _make_passing_data_source()
        ds.get_critical_alerts_count.return_value = 3
        checker = GoLiveGateChecker(data_source=ds)
        result = checker.check_reliability()
        assert result.passed is False

    def test_no_alerts_passes(self):
        ds = _make_passing_data_source()
        checker = GoLiveGateChecker(data_source=ds)
        result = checker.check_reliability()
        assert result.passed is True


# ---------------------------------------------------------------------------
# Gate 6 — data integrity
# ---------------------------------------------------------------------------

class TestDataIntegrity:
    def test_reconciliation_failure(self):
        ds = _make_passing_data_source()
        ds.get_latest_reconciliation_status.return_value = "discrepancy"
        checker = GoLiveGateChecker(data_source=ds)
        result = checker.check_data_integrity()
        assert result.passed is False

    def test_reconciliation_ok(self):
        ds = _make_passing_data_source()
        checker = GoLiveGateChecker(data_source=ds)
        result = checker.check_data_integrity()
        assert result.passed is True


# ---------------------------------------------------------------------------
# Gate 7 — model governance
# ---------------------------------------------------------------------------

class TestModelGovernance:
    def test_model_not_approved_fails(self):
        ds = _make_passing_data_source()
        ds.get_model_status.return_value = "rollback"
        checker = GoLiveGateChecker(data_source=ds)
        result = checker.check_model_governance()
        assert result.passed is False

    def test_model_approved_passes(self):
        ds = _make_passing_data_source()
        checker = GoLiveGateChecker(data_source=ds)
        result = checker.check_model_governance()
        assert result.passed is True


# ---------------------------------------------------------------------------
# Gate 8 — backtest regression
# ---------------------------------------------------------------------------

class TestBacktestRegression:
    def test_low_sharpe_fails(self):
        ds = _make_passing_data_source()
        ds.get_backtest_metrics.return_value = {
            "sharpe": 0.5,
            "max_drawdown": 0.10,
            "win_rate": 0.55,
        }
        checker = GoLiveGateChecker(data_source=ds)
        result = checker.check_backtest_regression()
        assert result.passed is False
        assert "Sharpe" in result.message

    def test_high_drawdown_fails(self):
        ds = _make_passing_data_source()
        ds.get_backtest_metrics.return_value = {
            "sharpe": 1.5,
            "max_drawdown": 0.20,
            "win_rate": 0.55,
        }
        checker = GoLiveGateChecker(data_source=ds)
        result = checker.check_backtest_regression()
        assert result.passed is False
        assert "drawdown" in result.message

    def test_low_win_rate_fails(self):
        ds = _make_passing_data_source()
        ds.get_backtest_metrics.return_value = {
            "sharpe": 1.5,
            "max_drawdown": 0.10,
            "win_rate": 0.40,
        }
        checker = GoLiveGateChecker(data_source=ds)
        result = checker.check_backtest_regression()
        assert result.passed is False
        assert "win rate" in result.message

    def test_all_metrics_pass(self):
        ds = _make_passing_data_source()
        checker = GoLiveGateChecker(data_source=ds)
        result = checker.check_backtest_regression()
        assert result.passed is True


# ---------------------------------------------------------------------------
# Aggregate behaviour
# ---------------------------------------------------------------------------

class TestAggregate:
    def test_single_failure_blocks_promotion(self):
        ds = _make_passing_data_source()
        ds.get_max_drawdown.return_value = 0.20  # gate 3 fails
        checker = GoLiveGateChecker(data_source=ds)
        assert checker.is_ready_for_live() is False

    def test_multiple_failures_all_reported(self):
        ds = _make_passing_data_source()
        ds.get_max_drawdown.return_value = 0.20
        ds.get_model_status.return_value = "caution"
        checker = GoLiveGateChecker(data_source=ds)
        results = checker.run_all_gates()
        failed = [r for r in results if not r.passed]
        assert len(failed) == 2
        names = {r.name for r in failed}
        assert "drawdown_bound" in names
        assert "model_governance" in names
