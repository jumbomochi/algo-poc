from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.models import (
    DivergenceDaily,
    DrillOutcome,
    GateEpoch,
    GateEpochEvent,
)
from shared.models.evidence import (
    DivergenceStatus,
    DrillType,
    EpochState,
    validate_manifest,
)


ROOT = Path(__file__).resolve().parents[2]

VALID_MANIFEST = {
    "baseline_id": "backtest_multi_20260812_101500.json",
    "sleeves": [
        "momentum",
        "sector_rotation",
        "thematic_momentum",
        "quality_value",
        "earnings_drift",
        "tail_risk_hedge",
    ],
    "weights": {
        "momentum": 0.2308,
        "sector_rotation": 0.1538,
        "thematic_momentum": 0.1410,
        "quality_value": 0.1538,
        "earnings_drift": 0.1923,
        "tail_risk_hedge": 0.1283,
    },
    "membership_snapshot": "data/universe/sp500_membership.json",
    "membership_snapshot_sha256": "a" * 64,
    "divergence": {"window_sessions": 30, "threshold": 0.20},
    "cost_model": {
        "slippage_bps": 10.0,
        "commission_per_share": 0.005,
        "commission_minimum": 1.0,
    },
    "money_path": {
        "services/risk_management": "b" * 40,
        "services/execution": "c" * 40,
        "scripts/run_paper.py": "d" * 40,
        "shared/order_ledger.py": "e" * 40,
        "shared/liquidation.py": "f" * 40,
    },
}


@pytest.fixture(scope="module")
def migrated_engine(tmp_path_factory):
    """A sqlite DB built by ``alembic upgrade head``.

    The models are exercised against the migration's schema on purpose: it is
    the migration's CHECK constraints and UNIQUE indexes that must hold in
    production, not whatever ``Base.metadata.create_all`` would have produced.
    """
    database_url = f"sqlite:///{tmp_path_factory.mktemp('evidence') / 'evidence.db'}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)

    previous = os.environ.get("ALGO_DATABASE_URL")
    os.environ["ALGO_DATABASE_URL"] = database_url
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("ALGO_DATABASE_URL", None)
        else:
            os.environ["ALGO_DATABASE_URL"] = previous

    engine = create_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(migrated_engine):
    with Session(migrated_engine) as session:
        yield session
        session.rollback()


def _manifest_without(*keys: str) -> dict:
    manifest = {k: v for k, v in VALID_MANIFEST.items()}
    manifest["money_path"] = dict(VALID_MANIFEST["money_path"])
    manifest["weights"] = dict(VALID_MANIFEST["weights"])
    for key in keys:
        manifest.pop(key, None)
    return manifest


def _now() -> datetime:
    return datetime(2026, 8, 15, 4, 45, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# ORM round trips
# --------------------------------------------------------------------------


def test_divergence_daily_round_trips(session):
    session.add(
        DivergenceDaily(
            sleeve="momentum",
            session_date=date(2026, 8, 14),
            status=DivergenceStatus.OK,
            baseline_id="backtest_multi_20260812_101500.json",
            window_sessions=30,
            threshold=0.20,
            metric_value=0.031,
            created_at=_now(),
        )
    )
    session.flush()

    row = session.query(DivergenceDaily).one()
    assert row.sleeve == "momentum"
    assert row.session_date == date(2026, 8, 14)
    assert row.status == "OK"
    assert row.window_sessions == 30
    assert row.threshold == pytest.approx(0.20)
    assert row.metric_value == pytest.approx(0.031)


def test_divergence_daily_allows_a_null_metric_for_no_data(session):
    session.add(
        DivergenceDaily(
            sleeve="tail_risk_hedge",
            session_date=date(2026, 8, 14),
            status=DivergenceStatus.NO_DATA,
            baseline_id="baseline-null-metric",
            window_sessions=30,
            threshold=0.20,
            metric_value=None,
            created_at=_now(),
        )
    )
    session.flush()

    row = session.query(DivergenceDaily).one()
    assert row.status == "NO_DATA"
    assert row.metric_value is None


def test_gate_epoch_round_trips_with_a_nested_manifest(session):
    session.add(
        GateEpoch(
            label="v2",
            rung=0,
            manifest=VALID_MANIFEST,
            started_at=_now(),
        )
    )
    session.flush()
    session.expire_all()

    row = session.query(GateEpoch).one()
    assert row.label == "v2"
    assert row.rung == 0
    assert row.manifest == VALID_MANIFEST
    assert row.manifest["money_path"]["shared/liquidation.py"] == "f" * 40
    assert row.manifest["divergence"]["window_sessions"] == 30


def test_gate_epoch_event_round_trips(session):
    session.add(
        GateEpochEvent(
            epoch_id=7,
            event_type="rung_change",
            rung_after=1,
            incident_id="INC-2026-08-15-01",
            reason="clean epoch, promoting",
            detail={"from_rung": 0, "evidence": {"clean_sessions": 30}},
            occurred_at=_now(),
        )
    )
    session.flush()
    session.expire_all()

    row = session.query(GateEpochEvent).one()
    assert row.epoch_id == 7
    assert row.event_type == "rung_change"
    assert row.rung_after == 1
    assert row.incident_id == "INC-2026-08-15-01"
    assert row.detail == {"from_rung": 0, "evidence": {"clean_sessions": 30}}


def test_gate_epoch_event_nullable_fields_stay_null(session):
    session.add(
        GateEpochEvent(
            epoch_id=7,
            event_type="started",
            occurred_at=_now(),
        )
    )
    session.flush()

    row = session.query(GateEpochEvent).one()
    assert row.rung_after is None
    assert row.incident_id is None
    assert row.reason is None
    assert row.detail is None


def test_drill_outcome_round_trips(session):
    session.add(
        DrillOutcome(
            epoch_id=7,
            drill_type=DrillType.RESTART_HALT,
            passed=True,
            detail="halt survived a full service restart",
            occurred_at=_now(),
        )
    )
    session.flush()

    row = session.query(DrillOutcome).one()
    assert row.epoch_id == 7
    assert row.drill_type == "restart_halt"
    assert row.passed is True
    assert row.detail == "halt survived a full service restart"


# --------------------------------------------------------------------------
# Uniqueness
# --------------------------------------------------------------------------


def test_divergence_daily_rejects_a_duplicate_sleeve_date_baseline(session):
    def row(**overrides):
        payload = {
            "sleeve": "momentum",
            "session_date": date(2026, 8, 14),
            "baseline_id": "baseline-uq",
            "status": DivergenceStatus.OK,
            "window_sessions": 30,
            "threshold": 0.20,
            "metric_value": 0.01,
            "created_at": _now(),
        }
        payload.update(overrides)
        return DivergenceDaily(**payload)

    session.add(row())
    session.flush()

    session.add(row())
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()

    # Changing any one leg of the triple is a distinct observation.
    session.add(row(sleeve="quality_value"))
    session.add(row(session_date=date(2026, 8, 13)))
    session.add(row(baseline_id="baseline-uq-2"))
    session.flush()
    assert session.query(DivergenceDaily).count() == 3


def test_gate_epochs_label_is_unique(session):
    session.add(GateEpoch(label="v9", rung=0, manifest={}, started_at=_now()))
    session.flush()

    session.add(GateEpoch(label="v9", rung=1, manifest={}, started_at=_now()))
    with pytest.raises(IntegrityError):
        session.flush()


# --------------------------------------------------------------------------
# Check constraints
# --------------------------------------------------------------------------


def test_divergence_status_check_rejects_an_unknown_status(session):
    session.add(
        DivergenceDaily(
            sleeve="momentum",
            session_date=date(2026, 8, 12),
            status="MAYBE",
            baseline_id="baseline-check",
            window_sessions=30,
            threshold=0.20,
            metric_value=None,
            created_at=_now(),
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_drill_type_check_rejects_an_unknown_drill(session):
    session.add(
        DrillOutcome(
            epoch_id=1,
            drill_type="smoke_test",
            passed=True,
            detail=None,
            occurred_at=_now(),
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_epoch_event_type_is_deliberately_unconstrained(session):
    """The ladder's amendment rule adds event kinds without a migration."""
    session.add(
        GateEpochEvent(
            epoch_id=1,
            event_type="a_kind_nobody_has_invented_yet",
            occurred_at=_now(),
        )
    )
    session.flush()

    assert session.query(GateEpochEvent).one().event_type == (
        "a_kind_nobody_has_invented_yet"
    )


# --------------------------------------------------------------------------
# D15 invariant: observations only, never derived truth
# --------------------------------------------------------------------------


def test_no_derived_column_exists_in_the_evidence_schema(migrated_engine):
    forbidden_substrings = ("streak", "is_blind", "is_clean", "consecutive")
    with migrated_engine.connect() as connection:
        inspector = inspect(connection)
        for table in (
            "divergence_daily",
            "gate_epochs",
            "gate_epoch_events",
            "drill_outcomes",
        ):
            names = {c["name"] for c in inspector.get_columns(table)}
            for name in names:
                assert not any(bad in name for bad in forbidden_substrings), (
                    f"{table}.{name} looks derived; D15 forbids storing it"
                )

        epoch_columns = {c["name"] for c in inspector.get_columns("gate_epochs")}
        assert "status" not in epoch_columns
        assert "ended_at" not in epoch_columns


def test_epoch_state_is_a_python_vocabulary_not_a_column(migrated_engine):
    assert {state.value for state in EpochState} == {
        "RUNNING",
        "CLEAN",
        "BREACHED",
        "EXTENDED",
        "RESTARTED",
        "DISARMED",
    }
    with migrated_engine.connect() as connection:
        assert "status" not in {
            c["name"] for c in inspect(connection).get_columns("gate_epochs")
        }


# --------------------------------------------------------------------------
# validate_manifest
# --------------------------------------------------------------------------


def test_validate_manifest_accepts_the_pinned_schema():
    assert validate_manifest(VALID_MANIFEST) is None


def test_validate_manifest_ignores_unknown_extra_keys():
    manifest = dict(VALID_MANIFEST)
    manifest["amendment_note"] = "added by the ladder's amendment rule"
    manifest["divergence"] = dict(VALID_MANIFEST["divergence"])
    manifest["divergence"]["future_knob"] = 3

    assert validate_manifest(manifest) is None


def test_validate_manifest_names_a_missing_top_level_key():
    with pytest.raises(ValueError) as excinfo:
        validate_manifest(_manifest_without("membership_snapshot"))

    assert "membership_snapshot" in str(excinfo.value)


def test_validate_manifest_reports_the_first_missing_key_alphabetically():
    with pytest.raises(ValueError) as excinfo:
        validate_manifest(_manifest_without("weights", "cost_model"))

    message = str(excinfo.value)
    assert "cost_model" in message
    assert "weights" not in message


def test_validate_manifest_names_a_missing_money_path_entry():
    manifest = _manifest_without()
    del manifest["money_path"]["shared/liquidation.py"]

    with pytest.raises(ValueError) as excinfo:
        validate_manifest(manifest)

    assert "shared/liquidation.py" in str(excinfo.value)
    assert "money_path" in str(excinfo.value)


def test_validate_manifest_rejects_weights_that_do_not_sum_to_one():
    manifest = _manifest_without()
    manifest["weights"]["momentum"] = 0.30

    with pytest.raises(ValueError) as excinfo:
        validate_manifest(manifest)

    assert "weights" in str(excinfo.value)


def test_validate_manifest_accepts_weights_within_the_tolerance():
    manifest = _manifest_without()
    manifest["weights"]["momentum"] = 0.2308 + 0.0009

    assert validate_manifest(manifest) is None


def test_validate_manifest_rejects_wrong_types():
    manifest = _manifest_without()
    manifest["sleeves"] = "momentum"
    with pytest.raises(ValueError) as excinfo:
        validate_manifest(manifest)
    assert "sleeves" in str(excinfo.value)

    manifest = _manifest_without()
    manifest["money_path"]["services/execution"] = "not-a-sha"
    with pytest.raises(ValueError) as excinfo:
        validate_manifest(manifest)
    assert "services/execution" in str(excinfo.value)

    manifest = _manifest_without()
    manifest["weights"]["momentum"] = "0.2308"
    with pytest.raises(ValueError) as excinfo:
        validate_manifest(manifest)
    assert "weights" in str(excinfo.value)


def test_validate_manifest_rejects_a_non_mapping():
    with pytest.raises(ValueError):
        validate_manifest(["not", "a", "manifest"])  # type: ignore[arg-type]
