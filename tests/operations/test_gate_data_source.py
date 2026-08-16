"""The eight go-live gates finally have something to read.

Every test seeds a scratch database and asserts a known expected value, because
the point of this data source is that the gate report is measured rather than
assumed. ``__drill__`` rows appear throughout: a drill must never move a gate's
number (KAN-24's exclusion contract).
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from scripts.ops.gate_data_source import GateDataUnavailable, PostgresGateDataSource
from scripts.ops.go_live_gate import DataSourceProtocol
from shared.evidence_store import equity_series, max_drawdown_pct
from shared.models.alerts import AlertRecord
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.ml_models import ModelVersion
from shared.models.order_ledger import (
    ExecutionFill,
    OrderIntent,
    OrderStatus,
    ReconciliationReport,
)
from shared.models.system_halt import SystemHaltState


NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
DRILL = "__drill__"
SLEEVE = "momentum"


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        yield db


@pytest.fixture()
def source(session: Session, tmp_path) -> PostgresGateDataSource:
    return PostgresGateDataSource(
        session, mode="paper", output_dir=tmp_path, now=lambda: NOW
    )


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def add_equity(
    session: Session, *, portfolio: str, day: date, equity: float
) -> None:
    session.add(
        EquitySnapshot(
            portfolio=portfolio,
            date=day,
            equity=equity,
            cash=0.0,
            market_value=equity,
            created_at=NOW,
        )
    )
    session.commit()


def add_intent(
    session: Session,
    *,
    recommendation_id: str,
    status: str,
    portfolio: str = SLEEVE,
    action: str = "BUY",
    limit_price: float | None = 100.0,
    reason: str | None = None,
) -> None:
    session.add(
        OrderIntent(
            reason=reason,
            recommendation_id=recommendation_id,
            account_id="DUN551088",
            mode="paper",
            portfolio=portfolio,
            con_id=1,
            symbol="AAPL",
            exchange="SMART",
            currency="USD",
            action=action,
            requested_quantity=10.0,
            limit_price=limit_price,
            order_type="LMT",
            status=status,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.commit()


def add_fill(
    session: Session,
    *,
    recommendation_id: str,
    price: float,
    execution_id: str,
    quantity: float = 10.0,
) -> None:
    session.add(
        ExecutionFill(
            account_id="DUN551088",
            execution_id=execution_id,
            ib_order_id="1",
            recommendation_id=recommendation_id,
            con_id=1,
            symbol="AAPL",
            exchange="SMART",
            currency="USD",
            side="BUY",
            quantity=quantity,
            price=price,
            executed_at=NOW,
        )
    )
    session.commit()


def add_alert(
    session: Session,
    *,
    priority: str,
    raised_at: datetime,
    resolved_at: datetime | None = None,
    message_id: str | None = None,
) -> None:
    session.add(
        AlertRecord(
            message_id=message_id or f"{raised_at.timestamp()}-{priority}",
            event_type="stop_loss_triggered",
            priority=priority,
            message="something happened",
            context={},
            raised_at=raised_at,
            resolved_at=resolved_at,
            recorded_at=raised_at,
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

def test_satisfies_the_runtime_checkable_protocol(source):
    assert isinstance(source, DataSourceProtocol)


# ---------------------------------------------------------------------------
# Gate 1 — paper duration
# ---------------------------------------------------------------------------

class TestPaperStartDate:
    def test_returns_the_earliest_equity_snapshot_as_utc_midnight(
        self, session, source
    ):
        add_equity(session, portfolio=SLEEVE, day=date(2026, 6, 16), equity=5000.0)
        add_equity(session, portfolio=SLEEVE, day=date(2026, 7, 1), equity=5100.0)

        assert source.get_paper_start_date() == datetime(
            2026, 6, 16, tzinfo=timezone.utc
        )

    def test_a_drill_portfolio_cannot_backdate_the_clock(self, session, source):
        add_equity(session, portfolio=SLEEVE, day=date(2026, 7, 1), equity=5000.0)
        add_equity(session, portfolio=DRILL, day=date(2026, 1, 1), equity=99.0)

        assert source.get_paper_start_date() == datetime(
            2026, 7, 1, tzinfo=timezone.utc
        )

    def test_no_snapshots_is_unavailable_not_today(self, source):
        with pytest.raises(GateDataUnavailable):
            source.get_paper_start_date()

    def test_an_explicit_paper_start_overrides_the_earliest_snapshot(
        self, session, tmp_path
    ):
        """The book carries pre-re-baseline history: on the real paper account
        the earliest snapshot is 2026-07-10, while the checklist records the
        clock as restarting 2026-07-30 after the Path A flatten. Reading across
        the reset measures a capital event, not trading."""
        add_equity(session, portfolio=SLEEVE, day=date(2026, 7, 10), equity=100000.0)
        add_equity(session, portfolio=SLEEVE, day=date(2026, 7, 30), equity=31733.0)
        source = PostgresGateDataSource(
            session,
            output_dir=tmp_path,
            now=lambda: NOW,
            paper_start=date(2026, 7, 30),
        )

        assert source.get_paper_start_date() == datetime(
            2026, 7, 30, tzinfo=timezone.utc
        )

    def test_an_explicit_paper_start_also_bounds_the_drawdown_series(
        self, session, tmp_path
    ):
        """Otherwise gate 3 reports the re-baseline as a 68% drawdown — a
        bookkeeping event dressed up as a trading loss, which is a gate the
        operator learns to argue away."""
        add_equity(session, portfolio=SLEEVE, day=date(2026, 7, 25), equity=100000.0)
        add_equity(session, portfolio=SLEEVE, day=date(2026, 7, 28), equity=31733.0)
        add_equity(session, portfolio=SLEEVE, day=date(2026, 7, 30), equity=101000.0)
        add_equity(session, portfolio=SLEEVE, day=date(2026, 8, 1), equity=99000.0)
        source = PostgresGateDataSource(
            session,
            output_dir=tmp_path,
            now=lambda: NOW,
            paper_start=date(2026, 7, 30),
        )

        # 101000 -> 99000 is ~1.98%, not the 68% the reset would report.
        assert source.get_max_drawdown() == pytest.approx(0.019801980, abs=1e-6)


# ---------------------------------------------------------------------------
# Gate 2 — circuit-breaker events
# ---------------------------------------------------------------------------

class TestCircuitBreakerEvents:
    def _halt(self, session, *, source_name: str, activated_at: datetime) -> None:
        session.add(
            SystemHaltState(
                mode="paper",
                active=False,
                source=source_name,
                reason="drawdown breach",
                triggered_by="risk_service",
                activated_at=activated_at,
            )
        )
        session.commit()

    def test_returns_only_circuit_breaker_halts_inside_the_window(
        self, session, source
    ):
        self._halt(session, source_name="circuit_breaker", activated_at=NOW)
        self._halt(
            session,
            source_name="circuit_breaker",
            activated_at=NOW - timedelta(days=90),
        )
        # A manual kill is an operator decision, not an instability signal.
        self._halt(session, source_name="kill", activated_at=NOW)

        events = source.get_circuit_breaker_events(since=NOW - timedelta(days=30))

        assert len(events) == 1
        assert events[0]["reason"] == "drawdown breach"
        assert events[0]["triggered_by"] == "risk_service"

    def test_a_quiet_window_is_an_empty_list_not_an_error(self, source):
        assert source.get_circuit_breaker_events(since=NOW - timedelta(days=30)) == []


# ---------------------------------------------------------------------------
# Gate 3 — drawdown
# ---------------------------------------------------------------------------

class TestMaxDrawdown:
    def test_matches_evidence_store_for_identical_data(self, session, source):
        """AC5: one implementation of the arithmetic, not two."""
        add_equity(session, portfolio=SLEEVE, day=date(2026, 6, 16), equity=5000.0)
        add_equity(session, portfolio=SLEEVE, day=date(2026, 6, 17), equity=5500.0)
        add_equity(session, portfolio=SLEEVE, day=date(2026, 6, 18), equity=4675.0)
        add_equity(session, portfolio=SLEEVE, day=date(2026, 6, 19), equity=5200.0)

        expected = max_drawdown_pct(
            equity_series(session, start=date(2026, 6, 16), end=NOW.date())
        )

        assert expected == pytest.approx(15.0)
        # The gate compares against a fraction; evidence_store speaks percent.
        assert source.get_max_drawdown() == pytest.approx(expected / 100.0)

    def test_a_drill_drawdown_does_not_move_the_gate(self, session, source):
        add_equity(session, portfolio=SLEEVE, day=date(2026, 6, 16), equity=5000.0)
        add_equity(session, portfolio=SLEEVE, day=date(2026, 6, 17), equity=5000.0)
        add_equity(session, portfolio=DRILL, day=date(2026, 6, 17), equity=1.0)

        assert source.get_max_drawdown() == pytest.approx(0.0)

    def test_no_snapshots_is_unavailable_not_zero(self, source):
        with pytest.raises(GateDataUnavailable):
            source.get_max_drawdown()


# ---------------------------------------------------------------------------
# Gate 4 — execution quality
# ---------------------------------------------------------------------------

class TestMedianSlippage:
    def test_median_of_signed_slippage_against_the_intent_limit(
        self, session, source
    ):
        for i, (limit, fill) in enumerate(
            [(100.0, 100.1), (100.0, 100.2), (100.0, 100.5)]
        ):
            add_intent(
                session,
                recommendation_id=f"rec-{i}",
                status=OrderStatus.FILLED.value,
                limit_price=limit,
            )
            add_fill(
                session,
                recommendation_id=f"rec-{i}",
                price=fill,
                execution_id=f"ex-{i}",
            )

        # 10, 20, 50 bps paid above the limit -> median 20.
        assert source.get_median_slippage_bps() == pytest.approx(20.0)

    def test_a_sell_filled_below_its_limit_is_positive_slippage(
        self, session, source
    ):
        add_intent(
            session,
            recommendation_id="rec-sell",
            status=OrderStatus.FILLED.value,
            action="SELL",
            limit_price=100.0,
        )
        add_fill(
            session, recommendation_id="rec-sell", price=99.7, execution_id="ex-sell"
        )

        assert source.get_median_slippage_bps() == pytest.approx(30.0)

    def test_drill_fills_are_excluded(self, session, source):
        add_intent(
            session,
            recommendation_id="rec-live",
            status=OrderStatus.FILLED.value,
            limit_price=100.0,
        )
        add_fill(
            session, recommendation_id="rec-live", price=100.1, execution_id="ex-live"
        )
        add_intent(
            session,
            recommendation_id="rec-drill",
            status=OrderStatus.FILLED.value,
            portfolio=DRILL,
            limit_price=100.0,
        )
        add_fill(
            session,
            recommendation_id="rec-drill",
            price=150.0,
            execution_id="ex-drill",
        )

        assert source.get_median_slippage_bps() == pytest.approx(10.0)

    def test_a_partially_filled_order_contributes_one_sample_not_many(
        self, session, source
    ):
        """``recommendation_id`` is unique on the intent, so the join fans out
        1:N over partial fills. An order that filled in pieces would otherwise
        vote once per piece and dominate the median on a small book."""
        add_intent(
            session, recommendation_id="split", status=OrderStatus.FILLED.value
        )
        add_fill(session, recommendation_id="split", price=100.5, execution_id="s1")
        add_fill(session, recommendation_id="split", price=100.1, execution_id="s2")
        add_intent(session, recommendation_id="b", status=OrderStatus.FILLED.value)
        add_fill(session, recommendation_id="b", price=100.1, execution_id="b1")
        add_intent(session, recommendation_id="c", status=OrderStatus.FILLED.value)
        add_fill(session, recommendation_id="c", price=100.2, execution_id="c1")

        # Per order: VWAP 100.3 -> 30 bps, then 10 and 20 -> median 20.
        # Per fill it would be [50, 10, 10, 20] -> median 15.
        assert source.get_median_slippage_bps() == pytest.approx(20.0)

    def test_a_volume_weighted_average_not_a_simple_one(self, session, source):
        add_intent(session, recommendation_id="w", status=OrderStatus.FILLED.value)
        add_fill(
            session,
            recommendation_id="w",
            price=100.1,
            execution_id="w1",
            quantity=90.0,
        )
        add_fill(
            session,
            recommendation_id="w",
            price=101.0,
            execution_id="w2",
            quantity=10.0,
        )

        # VWAP = (90*100.1 + 10*101.0)/100 = 100.19 -> 19 bps, not 55.
        assert source.get_median_slippage_bps() == pytest.approx(19.0)

    def test_no_matched_fills_is_unavailable_not_zero_slippage(self, source):
        with pytest.raises(GateDataUnavailable):
            source.get_median_slippage_bps()


class TestFailedOrderRate:
    def test_failures_over_orders_that_reached_submission(self, session, source):
        add_intent(
            session, recommendation_id="a", status=OrderStatus.FILLED.value
        )
        add_intent(
            session, recommendation_id="b", status=OrderStatus.SUBMITTED.value
        )
        add_intent(
            session, recommendation_id="c", status=OrderStatus.CANCELLED.value
        )
        add_intent(
            session, recommendation_id="d", status=OrderStatus.SUBMISSION_FAILED.value
        )
        # Never reached the broker: risk rejected it, so it is not a submission.
        add_intent(
            session, recommendation_id="e", status=OrderStatus.RISK_REJECTED.value
        )

        assert source.get_failed_order_rate() == pytest.approx(0.25)

    def test_a_halted_buy_is_not_a_broker_failure(self, session, source):
        """``services/execution/runner.py:355-366`` writes SUBMISSION_FAILED with
        ``reason='halted'`` for an order deliberately never sent. Counting it
        would fail gate 4 for a halt that gate 2 already counts."""
        add_intent(session, recommendation_id="a", status=OrderStatus.FILLED.value)
        add_intent(
            session,
            recommendation_id="halted",
            status=OrderStatus.SUBMISSION_FAILED.value,
            reason="halted",
        )

        assert source.get_failed_order_rate() == pytest.approx(0.0)

    def test_an_unsizeable_order_is_not_a_broker_failure(self, session, source):
        """``OrderSkippedError`` — the execution service's own comment reads
        "Not a failure: the order cannot be sized on this account"."""
        add_intent(session, recommendation_id="a", status=OrderStatus.FILLED.value)
        add_intent(
            session,
            recommendation_id="skipped",
            status=OrderStatus.SUBMISSION_FAILED.value,
            reason=(
                "AAPL: fractional quantity 0.4 rounds to zero whole shares "
                "(account has no fractional API support)"
            ),
        )

        assert source.get_failed_order_rate() == pytest.approx(0.0)

    def test_a_real_broker_rejection_still_counts(self, session, source):
        add_intent(session, recommendation_id="a", status=OrderStatus.FILLED.value)
        add_intent(
            session,
            recommendation_id="rejected",
            status=OrderStatus.SUBMISSION_FAILED.value,
            reason="IB error 201: insufficient margin",
        )

        assert source.get_failed_order_rate() == pytest.approx(0.5)

    def test_drill_orders_do_not_move_the_rate(self, session, source):
        add_intent(
            session, recommendation_id="a", status=OrderStatus.FILLED.value
        )
        add_intent(
            session,
            recommendation_id="drill",
            status=OrderStatus.SUBMISSION_FAILED.value,
            portfolio=DRILL,
        )

        assert source.get_failed_order_rate() == pytest.approx(0.0)

    def test_no_submitted_orders_is_unavailable(self, source):
        with pytest.raises(GateDataUnavailable):
            source.get_failed_order_rate()


# ---------------------------------------------------------------------------
# Gate 5 — reliability
# ---------------------------------------------------------------------------

class TestCriticalAlerts:
    def test_counts_unresolved_criticals_in_the_window(self, session, source):
        add_alert(session, priority="critical", raised_at=NOW - timedelta(days=1))
        add_alert(session, priority="critical", raised_at=NOW - timedelta(days=2))
        add_alert(
            session,
            priority="critical",
            raised_at=NOW - timedelta(days=3),
            resolved_at=NOW,
        )
        add_alert(session, priority="high", raised_at=NOW - timedelta(days=1))
        add_alert(session, priority="critical", raised_at=NOW - timedelta(days=40))

        assert source.get_critical_alerts_count(since=NOW - timedelta(days=14)) == 2

    def test_a_quiet_window_with_a_live_recorder_is_zero(self, session, source):
        add_alert(session, priority="low", raised_at=NOW - timedelta(days=1))

        assert source.get_critical_alerts_count(since=NOW - timedelta(days=14)) == 0

    def test_a_silent_recorder_is_unavailable_not_zero(self, session, source):
        """The failure this whole readiness effort exists to eliminate: a gate
        that passes because nothing was written down."""
        add_alert(session, priority="low", raised_at=NOW - timedelta(days=40))

        with pytest.raises(GateDataUnavailable):
            source.get_critical_alerts_count(since=NOW - timedelta(days=14))


# ---------------------------------------------------------------------------
# Gate 6 — reconciliation
# ---------------------------------------------------------------------------

class TestReconciliationStatus:
    def _report(self, session, *, status: str, created_at: datetime) -> None:
        session.add(
            ReconciliationReport(
                account_id="DUN551088",
                mode="paper",
                status=status,
                entries_allowed=True,
                result={},
                created_at=created_at,
            )
        )
        session.commit()

    def test_reads_the_newest_persisted_report(self, session, source):
        self._report(session, status="major", created_at=NOW - timedelta(days=2))
        self._report(session, status="ok", created_at=NOW - timedelta(hours=1))

        assert source.get_latest_reconciliation_status() == "ok"

    def test_a_live_report_does_not_answer_for_paper(self, session, source):
        session.add(
            ReconciliationReport(
                account_id="U123",
                mode="live",
                status="ok",
                entries_allowed=True,
                result={},
                created_at=NOW,
            )
        )
        session.commit()

        with pytest.raises(GateDataUnavailable):
            source.get_latest_reconciliation_status()

    def test_no_report_is_unavailable_not_ok(self, source):
        with pytest.raises(GateDataUnavailable):
            source.get_latest_reconciliation_status()

    def test_a_stale_ok_is_unavailable_not_ok(self, session, source):
        """Reconciliation runs every paper session. A months-old 'ok' would
        otherwise pass gate 6 forever — ignorance wearing a passing grade."""
        self._report(session, status="ok", created_at=NOW - timedelta(days=45))

        with pytest.raises(GateDataUnavailable) as excinfo:
            source.get_latest_reconciliation_status()
        assert "45" in str(excinfo.value) or "stale" in str(excinfo.value).lower()

    def test_reports_within_the_freshness_window_are_read_normally(
        self, session, source
    ):
        self._report(session, status="ok", created_at=NOW - timedelta(days=6))

        assert source.get_latest_reconciliation_status() == "ok"

    def test_same_timestamp_reports_resolve_deterministically(
        self, session, source
    ):
        """Two reports written in the same transaction share a timestamp; the
        later row is the later reconciliation."""
        self._report(session, status="ok", created_at=NOW)
        self._report(session, status="major", created_at=NOW)

        assert source.get_latest_reconciliation_status() == "major"


# ---------------------------------------------------------------------------
# Gate 7 — model governance
# ---------------------------------------------------------------------------

class TestModelStatus:
    def _version(self, session, *, version: str, is_active: bool) -> None:
        session.add(
            ModelVersion(
                version=version,
                training_window_start=date(2026, 1, 1),
                training_window_end=date(2026, 6, 1),
                metrics={},
                model_path=f"/models/{version}.txt",
                is_active=is_active,
                created_at=NOW,
            )
        )
        session.commit()

    def test_no_model_versions_reports_none(self, source):
        assert source.get_model_status() == "none"

    def test_an_active_version_is_active_never_approved(self, session, source):
        """There is no approval field in ``model_versions``; reporting one would
        be inventing governance the repo does not have (KAN-35/KAN-37)."""
        self._version(session, version="v1", is_active=True)

        assert source.get_model_status() == "active"

    def test_versions_with_none_active_report_inactive(self, session, source):
        self._version(session, version="v1", is_active=False)

        assert source.get_model_status() == "inactive"


# ---------------------------------------------------------------------------
# Gate 8 — backtest regression
# ---------------------------------------------------------------------------

class TestBacktestMetrics:
    def _write(self, tmp_path, *, name: str, metrics: dict) -> None:
        (tmp_path / name).write_text(
            json.dumps({"aggregate": {"metrics": metrics}})
        )

    def test_reads_the_newest_backtest_and_renames_sharpe_ratio(
        self, tmp_path, source
    ):
        self._write(
            tmp_path,
            name="backtest_multi_20260721_053247.json",
            metrics={"sharpe_ratio": 0.4, "max_drawdown": 0.30, "win_rate": 0.40},
        )
        self._write(
            tmp_path,
            name="backtest_multi_20260804_075705.json",
            metrics={"sharpe_ratio": 1.91, "max_drawdown": 0.105, "win_rate": 0.523},
        )

        assert source.get_backtest_metrics() == {
            "sharpe": pytest.approx(1.91),
            "max_drawdown": pytest.approx(0.105),
            "win_rate": pytest.approx(0.523),
        }

    def test_no_backtest_artifact_is_unavailable(self, source):
        with pytest.raises(GateDataUnavailable):
            source.get_backtest_metrics()
