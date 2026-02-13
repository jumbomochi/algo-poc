"""Paper-to-live promotion gate checker.

Validates all prerequisite gates before promoting from paper trading to live.
Each gate is an independent check that returns a structured result.  The actual
data retrieval is abstracted behind ``DataSourceProtocol`` so tests can inject
mocks without touching real infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateResult:
    """Outcome of a single promotion-gate check."""

    name: str
    passed: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Data-source abstraction
# ---------------------------------------------------------------------------

@runtime_checkable
class DataSourceProtocol(Protocol):
    """Interface for retrieving promotion-gate data.

    Implementations may read from PostgreSQL, Redis, audit logs, etc.
    """

    def get_paper_start_date(self) -> datetime: ...

    def get_circuit_breaker_events(self, since: datetime) -> list[dict[str, Any]]: ...

    def get_max_drawdown(self) -> float: ...

    def get_median_slippage_bps(self) -> float: ...

    def get_failed_order_rate(self) -> float: ...

    def get_critical_alerts_count(self, since: datetime) -> int: ...

    def get_latest_reconciliation_status(self) -> str: ...

    def get_model_status(self) -> str: ...

    def get_backtest_metrics(self) -> dict[str, float]: ...


# ---------------------------------------------------------------------------
# Default thresholds
# ---------------------------------------------------------------------------

_DEFAULT_THRESHOLDS: dict[str, Any] = {
    "min_paper_days": 60,
    "circuit_breaker_lookback_days": 30,
    "max_drawdown_pct": 0.12,
    "max_median_slippage_bps": 20.0,
    "max_failed_order_rate": 0.01,
    "critical_alert_lookback_days": 14,
    "backtest_min_sharpe": 1.0,
    "backtest_max_drawdown": 0.15,
    "backtest_min_win_rate": 0.50,
}


# ---------------------------------------------------------------------------
# Gate checker
# ---------------------------------------------------------------------------

class GoLiveGateChecker:
    """Evaluates all promotion gates for paper -> live transition."""

    def __init__(
        self,
        data_source: DataSourceProtocol,
        thresholds: dict[str, Any] | None = None,
    ) -> None:
        self._ds = data_source
        self._cfg = {**_DEFAULT_THRESHOLDS, **(thresholds or {})}

    # -- individual gates ---------------------------------------------------

    def check_paper_duration(self) -> GateResult:
        """Gate 1: minimum calendar days in paper mode."""
        start = self._ds.get_paper_start_date()
        now = datetime.now(timezone.utc)
        days_elapsed = (now - start).days
        required = self._cfg["min_paper_days"]
        passed = days_elapsed >= required
        return GateResult(
            name="paper_duration",
            passed=passed,
            message=(
                f"Paper duration {days_elapsed}d >= {required}d"
                if passed
                else f"Paper duration {days_elapsed}d < required {required}d"
            ),
            details={"days_elapsed": days_elapsed, "required": required},
        )

    def check_risk_stability(self) -> GateResult:
        """Gate 2: no circuit-breaker events in lookback window."""
        lookback = self._cfg["circuit_breaker_lookback_days"]
        since = datetime.now(timezone.utc) - timedelta(days=lookback)
        events = self._ds.get_circuit_breaker_events(since=since)
        passed = len(events) == 0
        return GateResult(
            name="risk_stability",
            passed=passed,
            message=(
                f"No circuit-breaker events in last {lookback}d"
                if passed
                else f"{len(events)} circuit-breaker event(s) in last {lookback}d"
            ),
            details={"events": events, "lookback_days": lookback},
        )

    def check_drawdown(self) -> GateResult:
        """Gate 3: paper max drawdown within threshold."""
        max_dd = self._ds.get_max_drawdown()
        threshold = self._cfg["max_drawdown_pct"]
        passed = max_dd <= threshold
        return GateResult(
            name="drawdown_bound",
            passed=passed,
            message=(
                f"Max drawdown {max_dd:.2%} <= {threshold:.2%}"
                if passed
                else f"Max drawdown {max_dd:.2%} exceeds {threshold:.2%}"
            ),
            details={"max_drawdown": max_dd, "threshold": threshold},
        )

    def check_execution_quality(self) -> GateResult:
        """Gate 4: median slippage and failed-order rate within tolerance."""
        slippage = self._ds.get_median_slippage_bps()
        failed_rate = self._ds.get_failed_order_rate()
        max_slip = self._cfg["max_median_slippage_bps"]
        max_fail = self._cfg["max_failed_order_rate"]
        slip_ok = slippage <= max_slip
        fail_ok = failed_rate <= max_fail
        passed = slip_ok and fail_ok
        parts: list[str] = []
        if not slip_ok:
            parts.append(f"slippage {slippage:.1f} bps > {max_slip:.1f} bps")
        if not fail_ok:
            parts.append(f"failed-order rate {failed_rate:.2%} > {max_fail:.2%}")
        message = "Execution quality within tolerance" if passed else "; ".join(parts)
        return GateResult(
            name="execution_quality",
            passed=passed,
            message=message,
            details={
                "median_slippage_bps": slippage,
                "max_slippage_bps": max_slip,
                "failed_order_rate": failed_rate,
                "max_failed_order_rate": max_fail,
            },
        )

    def check_reliability(self) -> GateResult:
        """Gate 5: no unresolved critical alerts in lookback window."""
        lookback = self._cfg["critical_alert_lookback_days"]
        since = datetime.now(timezone.utc) - timedelta(days=lookback)
        count = self._ds.get_critical_alerts_count(since=since)
        passed = count == 0
        return GateResult(
            name="reliability",
            passed=passed,
            message=(
                f"No critical alerts in last {lookback}d"
                if passed
                else f"{count} unresolved critical alert(s) in last {lookback}d"
            ),
            details={"alert_count": count, "lookback_days": lookback},
        )

    def check_data_integrity(self) -> GateResult:
        """Gate 6: latest reconciliation passes."""
        status = self._ds.get_latest_reconciliation_status()
        passed = status.lower() == "ok"
        return GateResult(
            name="data_integrity",
            passed=passed,
            message=(
                "Reconciliation status OK"
                if passed
                else f"Reconciliation status: {status}"
            ),
            details={"status": status},
        )

    def check_model_governance(self) -> GateResult:
        """Gate 7: model version approved and not in rollback/caution."""
        status = self._ds.get_model_status()
        passed = status.lower() == "approved"
        return GateResult(
            name="model_governance",
            passed=passed,
            message=(
                "Model version approved"
                if passed
                else f"Model status: {status} (requires 'approved')"
            ),
            details={"model_status": status},
        )

    def check_backtest_regression(self) -> GateResult:
        """Gate 8: latest backtest metrics within tolerance of baseline."""
        metrics = self._ds.get_backtest_metrics()
        min_sharpe = self._cfg["backtest_min_sharpe"]
        max_dd = self._cfg["backtest_max_drawdown"]
        min_wr = self._cfg["backtest_min_win_rate"]

        failures: list[str] = []
        if metrics.get("sharpe", 0) < min_sharpe:
            failures.append(
                f"Sharpe {metrics.get('sharpe', 0):.2f} < {min_sharpe:.2f}"
            )
        if metrics.get("max_drawdown", 1.0) > max_dd:
            failures.append(
                f"drawdown {metrics.get('max_drawdown', 1.0):.2%} > {max_dd:.2%}"
            )
        if metrics.get("win_rate", 0) < min_wr:
            failures.append(
                f"win rate {metrics.get('win_rate', 0):.2%} < {min_wr:.2%}"
            )

        passed = len(failures) == 0
        return GateResult(
            name="backtest_regression",
            passed=passed,
            message=(
                "Backtest regression passed"
                if passed
                else "Backtest regression failed: " + "; ".join(failures)
            ),
            details={"metrics": metrics, "thresholds": {
                "min_sharpe": min_sharpe,
                "max_drawdown": max_dd,
                "min_win_rate": min_wr,
            }},
        )

    # -- aggregate ----------------------------------------------------------

    def run_all_gates(self) -> list[GateResult]:
        """Execute every promotion gate and return the results."""
        return [
            self.check_paper_duration(),
            self.check_risk_stability(),
            self.check_drawdown(),
            self.check_execution_quality(),
            self.check_reliability(),
            self.check_data_integrity(),
            self.check_model_governance(),
            self.check_backtest_regression(),
        ]

    def is_ready_for_live(self) -> bool:
        """Return ``True`` only when **all** gates pass."""
        return all(r.passed for r in self.run_all_gates())
