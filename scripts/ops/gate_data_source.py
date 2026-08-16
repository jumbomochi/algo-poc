"""Concrete :class:`~scripts.ops.go_live_gate.DataSourceProtocol` over Postgres.

``go_live_gate.py`` has implemented the eight promotion gates and their
thresholds since the original plan, but nothing ever fed them: the only
implementations of the protocol were ``MagicMock``s in its own test. Sixty days
into an epoch, on gate day, half the review would have had no machine
evaluation behind it. This module is the missing half — one query per protocol
method, no gate logic and no threshold touched.

Two rules govern everything here:

**Never answer by ignorance.** A method that cannot produce a trustworthy
number raises :class:`GateDataUnavailable` rather than returning a default. A
zero returned because nothing was recorded reads identically to a zero that was
measured, and the second one is the only one a gate may pass on. The CLI turns
the exception into a failed gate with the reason attached.

**Never let a drill move a number.** Every portfolio-scoped query excludes
``_``-prefixed portfolios per KAN-24's exclusion contract, or a synthetic
stop-loss drill contaminates execution quality and drawdown alike.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from shared.evidence_store import equity_series, max_drawdown_pct
from shared.models.alerts import AlertRecord
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.ml_models import ModelVersion
from shared.models.order_ledger import (
    ExecutionFill,
    OrderIntent,
    OrderStatus,
    ReconciliationReport,
)
from shared.models.system_halt import SystemHaltState
from shared.universe import EXCLUDED_PORTFOLIO_PREFIX


class GateDataUnavailable(Exception):
    """A gate's evidence cannot be measured, so the gate cannot be judged.

    Distinct from "the gate failed": the difference between *we looked and it
    is bad* and *we have no way to look* is the whole point of the readiness
    effort, and collapsing the two is how a checklist passes by ignorance.
    """


#: Statuses that mean the order was handed to the broker (or the hand-off
#: failed). Anything earlier — ``PROPOSED``, ``RISK_REJECTED``, ``APPROVED`` —
#: never reached submission and so cannot be a submission failure.
_SUBMISSION_ATTEMPTED = (
    OrderStatus.SUBMISSION_FAILED.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
    OrderStatus.FILLED.value,
    OrderStatus.CANCELLED.value,
    OrderStatus.EXPIRED.value,
)

#: ``SUBMISSION_FAILED`` is overloaded: the execution service also writes it for
#: two outcomes it explicitly does not consider failures, and neither ever
#: reached the broker.
#:
#: * ``reason='halted'`` — a buy refused because the system is halted
#:   (``services/execution/runner.py:355-366``). Gate 2 already counts the halt;
#:   counting it again here would fail gate 4 for the same incident.
#: * the fractional-rounding skip — ``services/execution/ib_executor.py:184-187``,
#:   whose own comment reads *"Not a failure: the order cannot be sized on this
#:   account"*. On a ~5,000 SGD book against a no-fractional account these are
#:   common, and the status is terminal, so they would never wash out of a 1%
#:   threshold.
#:
#: Matched on ``reason`` because that is the only signal the ledger records. If
#: either producer's wording changes, this drifts silently — the tests in
#: ``tests/operations/test_gate_data_source.py`` pin the current strings.
_NOT_A_BROKER_FAILURE = (
    "halted",
    "%rounds to zero whole shares%",
)

#: How old the newest reconciliation report may be before gate 6's evidence
#: stops counting. A paper run reconciles every session, so a week covers a long
#: weekend plus a bad day without covering a pipeline that has silently stopped.
_MAX_RECONCILIATION_AGE_DAYS = 7


def _as_utc(moment: datetime) -> datetime:
    """sqlite hands back naive datetimes; Postgres hands back aware ones."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class PostgresGateDataSource:
    """Feeds :class:`~scripts.ops.go_live_gate.GoLiveGateChecker` from the book.

    Two known limits of what the schema can express:

    * ``equity_snapshots`` has no mode or account column, so gates 1 and 3 are
      **not** mode-scoped — ``--mode live`` still measures the combined equity
      series. Only ``system_halt``, ``order_intents`` and
      ``reconciliation_reports`` carry a mode.
    * Gate 1 measures calendar span, not continuity: it is the earliest
      snapshot ever recorded for a real portfolio, gaps included. A book that
      started 90 days ago and was dark for 60 of them still reads as 90. That
      matches the gate's wording ("minimum calendar days"); continuity is what
      the divergence monitor's blindness tracking is for.

    Args:
        session: An open session against the trading database.
        mode: ``paper`` or ``live`` — scopes the mode-aware tables so a live
            reconciliation can never answer a paper gate.
        output_dir: Where ``backtest_multi_*.json`` artifacts live.
        paper_start: Overrides the earliest-snapshot clock. Needed whenever the
            book carries history from before a re-baseline — see
            :meth:`get_paper_start_date`.
        now: Injectable clock, so tests are not calendar-dependent.
    """

    def __init__(
        self,
        session: Session,
        *,
        mode: str = "paper",
        output_dir: str | Path = "output",
        excluded_prefix: str = EXCLUDED_PORTFOLIO_PREFIX,
        paper_start: date | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._mode = mode
        self._output_dir = Path(output_dir)
        self._excluded_prefix = excluded_prefix
        self._paper_start = paper_start
        self._now = now or (lambda: datetime.now(timezone.utc))

    # -- gate 1: paper duration --------------------------------------------

    def get_paper_start_date(self) -> datetime:
        """Earliest equity snapshot of a real portfolio, as UTC midnight.

        An explicit ``paper_start`` wins. It has to be available, because the
        earliest snapshot is not always the start of the *current* paper era: on
        the real paper account it is 2026-07-10, while the checklist records the
        clock as restarting 2026-07-30 after the Path A flatten-and-refund.
        Reading across that reset both overstates the elapsed days and reports
        the re-baseline as a ~68% drawdown on gate 3 — a capital event dressed
        up as a trading loss. Pass the date the checklist records; it is a
        stated, reviewable input rather than a silently wrong number.
        """
        if self._paper_start is not None:
            return datetime.combine(self._paper_start, time.min, tzinfo=timezone.utc)
        first: date | None = self._session.scalar(
            select(func.min(EquitySnapshot.date)).where(self._real_equity())
        )
        if first is None:
            raise GateDataUnavailable(
                "no equity snapshots exist, so the paper clock has never started"
            )
        return datetime.combine(first, time.min, tzinfo=timezone.utc)

    # -- gate 2: risk stability --------------------------------------------

    def get_circuit_breaker_events(self, since: datetime) -> list[dict[str, Any]]:
        """Automated halts in the window. A manual kill is an operator
        decision, not an instability signal, so ``source='kill'`` is excluded."""
        rows = self._session.scalars(
            select(SystemHaltState)
            .where(
                SystemHaltState.mode == self._mode,
                SystemHaltState.source == "circuit_breaker",
                SystemHaltState.activated_at >= since,
            )
            .order_by(SystemHaltState.activated_at)
        ).all()
        return [
            {
                "reason": row.reason,
                "triggered_by": row.triggered_by,
                "activated_at": row.activated_at.isoformat(),
                "cleared_at": row.cleared_at.isoformat() if row.cleared_at else None,
            }
            for row in rows
        ]

    # -- gate 3: drawdown ---------------------------------------------------

    def get_max_drawdown(self) -> float:
        """Worst peak-to-trough NAV decline since the paper clock started.

        Delegates to ``shared/evidence_store`` so this number and the epoch
        report's are the same number, computed once. That module speaks
        percent; the gate's threshold is a fraction.
        """
        start = self.get_paper_start_date().date()
        rows = equity_series(
            self._session,
            start=start,
            end=self._now().date(),
            excluded_prefix=self._excluded_prefix,
        )
        if not rows:
            raise GateDataUnavailable(
                "no equity snapshots in the paper window, so drawdown is "
                "unmeasured rather than zero"
            )
        return max_drawdown_pct(rows) / 100.0

    # -- gate 4: execution quality -----------------------------------------

    def get_median_slippage_bps(self) -> float:
        """Median cost of crossing, in basis points against the intent's limit.

        Signed so that "worse than the limit" is positive for both sides: a buy
        filled above its limit and a sell filled below its limit both cost
        money.

        One sample per *order*, not per fill: ``recommendation_id`` is unique on
        the intent, so the join fans out over partial fills and an order that
        filled in twenty pieces would otherwise vote twenty times. Pieces are
        collapsed to a volume-weighted average price first.
        """
        rows = self._session.execute(
            select(
                OrderIntent.action,
                OrderIntent.limit_price,
                func.sum(ExecutionFill.price * ExecutionFill.quantity)
                / func.sum(ExecutionFill.quantity),
            )
            .join(
                ExecutionFill,
                ExecutionFill.recommendation_id == OrderIntent.recommendation_id,
            )
            .where(
                OrderIntent.mode == self._mode,
                OrderIntent.limit_price.is_not(None),
                OrderIntent.limit_price > 0,
                ExecutionFill.quantity > 0,
                ~OrderIntent.portfolio.startswith(
                    self._excluded_prefix, autoescape=True
                ),
            )
            .group_by(
                OrderIntent.recommendation_id,
                OrderIntent.action,
                OrderIntent.limit_price,
            )
        ).all()
        if not rows:
            raise GateDataUnavailable(
                "no fills are matched to a limit order, so slippage is "
                "unmeasured rather than zero"
            )
        slippages = [
            (
                (price - limit) if action.upper() == "BUY" else (limit - price)
            )
            / limit
            * 10_000.0
            for action, limit, price in rows
        ]
        return statistics.median(slippages)

    def get_failed_order_rate(self) -> float:
        """Broker rejections over the orders that reached the broker.

        Orders the system itself declined to send — halted buys, sizes it
        cannot round — are neither failures nor submissions, so they leave both
        sides of the ratio (see :data:`_NOT_A_BROKER_FAILURE`).
        """
        attempted = self._session.scalar(
            select(func.count())
            .select_from(OrderIntent)
            .where(
                self._real_intent(),
                OrderIntent.status.in_(_SUBMISSION_ATTEMPTED),
                self._reached_the_broker(),
            )
        )
        if not attempted:
            raise GateDataUnavailable(
                "no orders have reached submission, so the failure rate is "
                "undefined rather than zero"
            )
        failed = self._session.scalar(
            select(func.count())
            .select_from(OrderIntent)
            .where(
                self._real_intent(),
                OrderIntent.status == OrderStatus.SUBMISSION_FAILED.value,
                self._reached_the_broker(),
            )
        )
        return (failed or 0) / attempted

    # -- gate 5: reliability ------------------------------------------------

    def get_critical_alerts_count(self, since: datetime) -> int:
        """Unresolved critical alerts raised in the window.

        Guarded by a liveness check: if the recorder wrote nothing at all in the
        window, a count of zero would mean "the notifications service was down"
        just as readily as "nothing went wrong", and the gate must not pass on
        the first reading. Recovery is an operator action — publish one alert
        (``scripts/ops/send_test_alert.py``) and the pipe proves itself.
        """
        recorded_any = self._session.scalar(
            select(func.count())
            .select_from(AlertRecord)
            .where(AlertRecord.recorded_at >= since)
        )
        if not recorded_any:
            raise GateDataUnavailable(
                "no alert of any priority was recorded in the window; the "
                "alert recorder was silent, so zero criticals is ignorance "
                "rather than evidence"
            )
        return (
            self._session.scalar(
                select(func.count())
                .select_from(AlertRecord)
                .where(
                    AlertRecord.priority == "critical",
                    AlertRecord.raised_at >= since,
                    AlertRecord.resolved_at.is_(None),
                )
            )
            or 0
        )

    # -- gate 6: data integrity --------------------------------------------

    def get_latest_reconciliation_status(self) -> str:
        """Severity of the newest persisted reconciliation for this mode.

        Bounded by age. ``scripts/reconcile_paper.py`` writes a row on every
        paper run, so a report older than :data:`_MAX_RECONCILIATION_AGE_DAYS`
        means the run stopped happening — and a months-old ``ok`` passing gate 6
        forever is ignorance wearing a passing grade. This bounds the
        *evidence*, not the gate: the "must be ok" rule is untouched.
        """
        row = self._session.execute(
            select(ReconciliationReport.status, ReconciliationReport.created_at)
            .where(ReconciliationReport.mode == self._mode)
            # id breaks ties: two reports written in one transaction share a
            # timestamp, and the later row is the later reconciliation.
            .order_by(
                ReconciliationReport.created_at.desc(),
                ReconciliationReport.id.desc(),
            )
            .limit(1)
        ).first()
        if row is None:
            raise GateDataUnavailable(
                f"no reconciliation report has been persisted for mode "
                f"{self._mode!r}"
            )
        status, created_at = row
        age_days = (self._now() - _as_utc(created_at)).days
        if age_days > _MAX_RECONCILIATION_AGE_DAYS:
            raise GateDataUnavailable(
                f"the newest reconciliation report is {age_days} days old "
                f"(limit {_MAX_RECONCILIATION_AGE_DAYS}); it is stale evidence, "
                f"not a current {status!r}"
            )
        return status

    # -- gate 7: model governance ------------------------------------------

    def get_model_status(self) -> str:
        """``none`` / ``inactive`` / ``active`` — deliberately never ``approved``.

        ``model_versions`` records no approval, so reporting one would invent
        governance this repo does not have. Gate 7 therefore fails until the
        ML decision (KAN-35) and the approval substitute (KAN-37) land, which
        is the truthful answer rather than an error.
        """
        total = self._session.scalar(select(func.count()).select_from(ModelVersion))
        if not total:
            return "none"
        active = self._session.scalar(
            select(func.count())
            .select_from(ModelVersion)
            .where(ModelVersion.is_active.is_(True))
        )
        return "active" if active else "inactive"

    # -- gate 8: backtest regression ---------------------------------------

    def get_backtest_metrics(self) -> dict[str, float]:
        """Aggregate metrics of the newest ``backtest_multi_*.json``.

        The artifact names the ratio ``sharpe_ratio``; the gate reads
        ``sharpe``. Mapping it here rather than widening the gate keeps the
        checker untouched.
        """
        candidates = sorted(self._output_dir.glob("backtest_multi_*.json"))
        if not candidates:
            raise GateDataUnavailable(
                f"no backtest_multi_*.json artifact under {self._output_dir}"
            )
        newest = candidates[-1]
        try:
            metrics = json.loads(newest.read_text())["aggregate"]["metrics"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise GateDataUnavailable(
                f"{newest.name} has no readable aggregate metrics block: {exc}"
            ) from exc
        return {
            "sharpe": float(metrics["sharpe_ratio"]),
            "max_drawdown": float(metrics["max_drawdown"]),
            "win_rate": float(metrics["win_rate"]),
        }

    # -- shared predicates --------------------------------------------------

    def _real_equity(self):
        return ~EquitySnapshot.portfolio.startswith(
            self._excluded_prefix, autoescape=True
        )

    def _real_intent(self):
        return (OrderIntent.mode == self._mode) & ~OrderIntent.portfolio.startswith(
            self._excluded_prefix, autoescape=True
        )

    def _reached_the_broker(self):
        """False for the outcomes the system declined to send itself."""
        return and_(
            *(
                OrderIntent.reason.is_(None) | ~OrderIntent.reason.like(pattern)
                for pattern in _NOT_A_BROKER_FAILURE
            )
        )
