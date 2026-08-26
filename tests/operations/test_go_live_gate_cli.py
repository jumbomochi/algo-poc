"""The evaluator behind ``docs/operations/go-live-checklist.md``.

``docs/plans/2026-02-13-trading-bot-implementation.md:2740`` has told operators
to "run scripts/ops/go_live_gate.py" since long before there was anything to
run. These tests are that command.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from scripts.ops.gate_data_source import (
    _MAX_RECONCILIATION_AGE_DAYS,
    GateDataUnavailable,
    PostgresGateDataSource,
)
from scripts.ops.go_live_gate import (
    GateResult,
    GoLiveGateChecker,
    evaluate,
    exit_code,
    main,
    render_json,
    render_text,
)
from shared.models.alerts import AlertRecord
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.order_ledger import ReconciliationReport


NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def _passing_source() -> MagicMock:
    ds = MagicMock()
    # The gates read the wall clock, so the start date must be relative to it.
    ds.get_paper_start_date.return_value = datetime.now(timezone.utc) - timedelta(
        days=90
    )
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


class TestEvaluate:
    def test_all_eight_gates_are_reported_in_order(self):
        results = evaluate(GoLiveGateChecker(_passing_source()))

        assert [r.name for r in results] == [
            "paper_duration",
            "risk_stability",
            "drawdown_bound",
            "execution_quality",
            "reliability",
            "data_integrity",
            "model_governance",
            "backtest_regression",
        ]
        assert all(r.passed for r in results)

    def test_unmeasurable_evidence_fails_its_gate_without_killing_the_report(self):
        """A gate whose evidence is missing must fail loudly and leave the other
        seven readable — an evaluator that raises tells the operator nothing."""
        ds = _passing_source()
        ds.get_critical_alerts_count.side_effect = GateDataUnavailable(
            "the alert recorder was silent"
        )

        results = evaluate(GoLiveGateChecker(ds))

        assert len(results) == 8
        reliability = next(r for r in results if r.name == "reliability")
        assert reliability.passed is False
        assert "the alert recorder was silent" in reliability.message
        assert reliability.details["data_unavailable"] is True
        assert all(r.passed for r in results if r.name != "reliability")

    def test_a_database_fault_fails_its_gate_rather_than_the_report(self):
        """An un-migrated or unreachable database must still produce a report
        that says which gate could not be read, not a traceback."""
        ds = _passing_source()
        ds.get_critical_alerts_count.side_effect = OperationalError(
            "SELECT ...", {}, Exception("no such table: alert_records")
        )

        results = evaluate(GoLiveGateChecker(ds))

        assert len(results) == 8
        reliability = next(r for r in results if r.name == "reliability")
        assert reliability.passed is False
        assert "alert_records" in reliability.message
        assert reliability.details["data_unavailable"] is True


class TestTransactionRecovery:
    def test_one_failed_query_does_not_abort_every_later_gate(self, tmp_path):
        """Postgres aborts the whole transaction on a failed statement, so
        without a rollback a single missing table makes every subsequent gate
        report 'current transaction is aborted' — a cascade of false
        unavailables that hides what those gates would actually have said.
        Observed against the real paper book: gate 5's absent ``alert_records``
        took gates 6 and 7 down with it.
        """
        database_url = f"sqlite:///{tmp_path / 'cascade.db'}"
        engine = create_engine(database_url)
        Base.metadata.create_all(engine)
        AlertRecord.__table__.drop(engine)
        with sessionmaker(bind=engine)() as real_session:
            real_session.add(
                ReconciliationReport(
                    account_id="DUN551088",
                    mode="paper",
                    status="ok",
                    entries_allowed=True,
                    result={},
                    created_at=datetime.now(timezone.utc),
                )
            )
            real_session.commit()
            session = MagicMock(wraps=real_session)

            results = evaluate(
                GoLiveGateChecker(
                    PostgresGateDataSource(
                        session, output_dir=str(tmp_path)
                    )
                ),
                session=session,
            )

        gates = {r.name: r for r in results}
        assert gates["reliability"].details["data_unavailable"] is True
        session.rollback.assert_called()
        # The gates after the broken one are measured, not collateral damage.
        assert gates["data_integrity"].passed is True
        assert gates["model_governance"].details["model_status"] == "none"


class TestExitCode:
    def test_zero_only_when_every_gate_passes(self):
        assert exit_code(evaluate(GoLiveGateChecker(_passing_source()))) == 0

    def test_nonzero_when_any_gate_fails(self):
        ds = _passing_source()
        ds.get_model_status.return_value = "none"

        assert exit_code(evaluate(GoLiveGateChecker(ds))) == 1


class TestRendering:
    def test_text_report_names_every_gate_and_its_measured_value(self):
        results = evaluate(GoLiveGateChecker(_passing_source()))

        report = render_text(results, mode="paper", evaluated_at=NOW)

        assert "paper_duration" in report
        assert "Paper duration 90d >= 60d" in report
        assert report.count("PASS") == 8
        assert "READY" in report

    def test_json_report_is_machine_readable(self):
        results = evaluate(GoLiveGateChecker(_passing_source()))

        payload = json.loads(render_json(results, mode="paper", evaluated_at=NOW))

        assert payload["mode"] == "paper"
        assert payload["evaluated_at"] == NOW.isoformat()
        assert payload["ready"] is True
        assert len(payload["gates"]) == 8
        assert payload["gates"][0] == {
            "name": "paper_duration",
            "passed": True,
            "message": "Paper duration 90d >= 60d",
            "details": {"days_elapsed": 90, "required": 60},
        }

    def test_a_failing_report_is_not_ready(self):
        results = [GateResult(name="x", passed=False, message="nope")]

        assert json.loads(render_json(results, mode="paper", evaluated_at=NOW))[
            "ready"
        ] is False
        assert "NOT READY" in render_text(results, mode="paper", evaluated_at=NOW)


class TestCommandLine:
    def _seed(self, database_url: str) -> None:
        """Seed a book that is *current* as of the wall clock.

        Every timestamp here is an offset from a single reading of the wall
        clock. The gates measure evidence age against ``datetime.now`` and the
        CLI gives them no seam to inject a fixed one — ``main`` constructs
        ``PostgresGateDataSource`` without a ``now=`` — so a hardcoded date does
        not fail on the day it is written. It passes until real time drifts past
        some gate's window, and then fails forever.

        That is exactly what happened on 2026-08-24: ``ReconciliationReport
        .created_at`` was pinned to ``NOW`` (2026-08-16) and silently aged past
        ``gate_data_source._MAX_RECONCILIATION_AGE_DAYS`` (7). Gate 6 flipped
        from "measured and passing" to "evidence unavailable", and ``main`` went
        red on a tree that had been green the day before. Nothing in the repo
        had changed.

        The module-level ``NOW`` is still correct for ``TestRenderers``, which
        only formats it and never compares it against a clock.
        """
        engine = create_engine(database_url)
        Base.metadata.create_all(engine)
        now = datetime.now(timezone.utc)
        with sessionmaker(bind=engine)() as session:
            session.add(
                EquitySnapshot(
                    portfolio="momentum",
                    date=now.date() - timedelta(days=1),
                    equity=5000.0,
                    cash=0.0,
                    market_value=5000.0,
                    created_at=now,
                )
            )
            session.add(
                ReconciliationReport(
                    account_id="DUN551088",
                    mode="paper",
                    status="ok",
                    entries_allowed=True,
                    result={},
                    created_at=now,
                )
            )
            session.add(
                AlertRecord(
                    message_id="1-0",
                    event_type="heartbeat",
                    priority="low",
                    message="daily run finished",
                    context={},
                    raised_at=now - timedelta(days=1),
                    recorded_at=now - timedelta(days=1),
                )
            )
            session.commit()

    def test_a_real_book_produces_a_truthful_mixed_report(self, tmp_path, capsys):
        """AC7: today several gates fail — the 60-day clock, model status — and
        that is the correct output, not an error."""
        database_url = f"sqlite:///{tmp_path / 'gate.db'}"
        self._seed(database_url)

        code = main(
            [
                "--database-url",
                database_url,
                "--output-dir",
                str(tmp_path),
                "--json",
            ]
        )

        payload = json.loads(capsys.readouterr().out)
        assert code == 1
        assert payload["ready"] is False
        gates = {g["name"]: g for g in payload["gates"]}
        assert len(gates) == 8

        # Measured and failing.
        assert gates["paper_duration"]["passed"] is False
        assert gates["paper_duration"]["details"]["days_elapsed"] == 1
        assert gates["model_governance"]["passed"] is False
        assert gates["model_governance"]["details"]["model_status"] == "none"

        # Measured and passing.
        assert gates["data_integrity"]["passed"] is True
        assert gates["risk_stability"]["passed"] is True
        assert gates["reliability"]["passed"] is True

        # No evidence at all — reported as unavailable, never as a pass.
        assert gates["execution_quality"]["details"]["data_unavailable"] is True
        assert gates["backtest_regression"]["details"]["data_unavailable"] is True

    def test_an_unreachable_database_fails_loudly_with_its_url(
        self, tmp_path, capsys
    ):
        """Per-gate containment would otherwise render eight tidy "evidence
        unavailable" lines — indistinguishable from an empty book, and the
        default URL does not point at this operator's stack."""
        code = main(
            [
                "--database-url",
                "postgresql://algo:algo@127.0.0.1:1/algo_poc",
                "--output-dir",
                str(tmp_path),
            ]
        )

        captured = capsys.readouterr()
        assert code == 2
        assert "127.0.0.1:1" in captured.err
        assert "PASS" not in captured.out
        assert "FAIL" not in captured.out

    def test_text_output_is_the_default(self, tmp_path, capsys):
        database_url = f"sqlite:///{tmp_path / 'gate_text.db'}"
        self._seed(database_url)

        code = main(["--database-url", database_url, "--output-dir", str(tmp_path)])

        out = capsys.readouterr().out
        assert code == 1
        assert "NOT READY" in out
        assert "model_governance" in out

    def test_the_seeded_book_cannot_age_out_of_the_gates_windows(self, tmp_path):
        """Guard on the 2026-08-24 breakage: `_seed` must track the wall clock.

        The failure mode this prevents is nasty because it is delayed. A
        timestamp pinned to a literal date passes CI on the day it is written
        and keeps passing, then crosses a gate's freshness window weeks later
        and fails on every branch at once, with nothing in the diff to explain
        it. `main` went red exactly this way on a tree that had been green the
        day before.

        Asserting the seeded evidence is minutes old, not merely inside the
        7-day window, is what makes a reintroduced literal fail *immediately*
        rather than becoming the same time bomb with a later fuse.
        """
        database_url = f"sqlite:///{tmp_path / 'freshness.db'}"
        self._seed(database_url)

        with sessionmaker(bind=create_engine(database_url))() as session:
            reconciliation = session.execute(
                select(ReconciliationReport.created_at)
            ).scalar_one()
            alert = session.execute(select(AlertRecord.raised_at)).scalar_one()
            snapshot = session.execute(select(EquitySnapshot.date)).scalar_one()

        now = datetime.now(timezone.utc)

        def age_of(stamp: datetime) -> timedelta:
            # sqlite hands back naive datetimes; the gate treats those as UTC.
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return now - stamp

        age = age_of(reconciliation)
        assert age < timedelta(minutes=5), (
            f"the seeded reconciliation is {age} old, so it was pinned to a "
            "literal date rather than derived from the wall clock. It will age "
            "past gate_data_source._MAX_RECONCILIATION_AGE_DAYS and take gate 6 "
            "red on a tree nobody touched — see this test's docstring."
        )
        # Well inside the limit the gate actually enforces, read from the code
        # so a change to the limit cannot silently invalidate this guard.
        assert age.days <= _MAX_RECONCILIATION_AGE_DAYS

        assert age_of(alert) < timedelta(days=1, minutes=5)
        assert snapshot == now.date() - timedelta(days=1)
