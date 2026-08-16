"""Paper-to-live promotion gate checker.

Validates all prerequisite gates before promoting from paper trading to live.
Each gate is an independent check that returns a structured result.  The actual
data retrieval is abstracted behind ``DataSourceProtocol`` so tests can inject
mocks without touching real infrastructure.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from scripts.ops.gate_data_source import GateDataUnavailable, PostgresGateDataSource


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


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

#: Gate name -> checker method. The name is spelled out here because a gate
#: whose data source raises never gets to name itself, and an unmeasurable gate
#: still has to appear in the report.
_GATES: tuple[tuple[str, str], ...] = (
    ("paper_duration", "check_paper_duration"),
    ("risk_stability", "check_risk_stability"),
    ("drawdown_bound", "check_drawdown"),
    ("execution_quality", "check_execution_quality"),
    ("reliability", "check_reliability"),
    ("data_integrity", "check_data_integrity"),
    ("model_governance", "check_model_governance"),
    ("backtest_regression", "check_backtest_regression"),
)


def evaluate(
    checker: GoLiveGateChecker, *, session: Session | None = None
) -> list[GateResult]:
    """Run all eight gates, containing per-gate evidence failures.

    ``run_all_gates`` is fine against an in-memory source, but a real one can
    fail to measure — no fills yet, no backtest artifact, a silent alert
    recorder, a database that has not had ``alembic upgrade head`` run against
    it. Such a gate becomes a **failure with the reason attached** rather than
    an exception that hides the other seven. It is never a pass: the difference
    between "we looked and it is bad" and "we could not look" must survive all
    the way to the operator's screen.

    Pass ``session`` when the source reads a database. Postgres aborts the whole
    transaction on a failed statement, so without a rollback between gates one
    missing table makes every later gate report "current transaction is
    aborted" — a cascade of false unavailables that hides what those gates would
    actually have said.
    """
    results: list[GateResult] = []
    for name, method in _GATES:
        try:
            results.append(getattr(checker, method)())
        except (GateDataUnavailable, SQLAlchemyError) as exc:
            if session is not None:
                session.rollback()
            results.append(
                GateResult(
                    name=name,
                    passed=False,
                    message=f"evidence unavailable: {exc}",
                    details={"data_unavailable": True},
                )
            )
    return results


def exit_code(results: list[GateResult]) -> int:
    """``0`` only when every gate passes."""
    return 0 if all(r.passed for r in results) else 1


def render_text(
    results: list[GateResult], *, mode: str, evaluated_at: datetime
) -> str:
    """Human-readable report: one line per gate, verdict last."""
    width = max((len(r.name) for r in results), default=0)
    lines = [
        f"Go-live gate — mode={mode}, evaluated {evaluated_at.isoformat()}",
        "",
    ]
    lines += [
        f"  {'PASS' if r.passed else 'FAIL'}  {r.name.ljust(width)}  {r.message}"
        for r in results
    ]
    failed = [r.name for r in results if not r.passed]
    lines += ["", f"{len(results) - len(failed)}/{len(results)} gates pass."]
    if failed:
        lines.append("NOT READY for live — blocked by: " + ", ".join(failed))
    else:
        lines.append("READY for live — all gates pass.")
    return "\n".join(lines)


def render_json(
    results: list[GateResult], *, mode: str, evaluated_at: datetime
) -> str:
    """Machine-readable report, for the gate-day review record."""
    return json.dumps(
        {
            "mode": mode,
            "evaluated_at": evaluated_at.isoformat(),
            "ready": all(r.passed for r in results),
            "gates": [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "message": r.message,
                    "details": r.details,
                }
                for r in results
            ],
        },
        indent=2,
        sort_keys=False,
        default=str,
    )


def main(argv: list[str] | None = None) -> int:
    """Evaluate the eight promotion gates against the real book."""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.ops.go_live_gate",
        description=(
            "Evaluate the paper-to-live promotion gates. Exits 0 only when all "
            "eight pass; a gate whose evidence cannot be measured fails."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Defaults to config/default.yaml (ALGO_DATABASE_URL overrides it).",
    )
    parser.add_argument("--mode", default="paper", choices=["paper", "live"])
    parser.add_argument(
        "--paper-start",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "Start of the current paper era. Defaults to the earliest equity "
            "snapshot, which reads across a re-baseline; pass the date recorded "
            "in docs/operations/go-live-checklist.md when the clock restarted."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Where backtest_multi_*.json artifacts live.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args(argv)

    database_url = args.database_url
    if database_url is None:
        from shared.config import load_config

        database_url = load_config("config/default.yaml").database.url

    evaluated_at = datetime.now(timezone.utc)
    engine = create_engine(database_url)
    try:
        # Probed once, before any gate runs. Per-gate containment is right for
        # a gate that cannot be measured, but it would render an unreachable
        # database as eight tidy "evidence unavailable" lines — indistinguishable
        # from an empty book, and the config default (localhost:5432) is not
        # where the local stack listens. A wrong URL must look wrong.
        try:
            with engine.connect():
                pass
        except SQLAlchemyError as exc:
            print(
                f"Cannot reach the database at {_redact(database_url)}: {exc}\n"
                "Set --database-url or ALGO_DATABASE_URL to the running stack.",
                file=sys.stderr,
            )
            return 2

        with sessionmaker(bind=engine)() as session:
            results = evaluate(
                GoLiveGateChecker(
                    PostgresGateDataSource(
                        session,
                        mode=args.mode,
                        output_dir=args.output_dir,
                        paper_start=args.paper_start,
                    )
                ),
                session=session,
            )
    finally:
        engine.dispose()

    render = render_json if args.json else render_text
    print(render(results, mode=args.mode, evaluated_at=evaluated_at))
    return exit_code(results)


def _redact(database_url: str) -> str:
    """Strip any password before a URL reaches the operator's terminal."""
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", database_url)


if __name__ == "__main__":
    raise SystemExit(main())
