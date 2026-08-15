from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[2]

EVIDENCE_TABLES = {
    "divergence_daily",
    "gate_epochs",
    "gate_epoch_events",
    "drill_outcomes",
}

PREVIOUS_REVISION = "82623f87013d"


def _config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_upgrade_creates_the_four_evidence_tables(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'evidence.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)

    command.upgrade(config, "head")

    with create_engine(database_url).connect() as connection:
        inspector = inspect(connection)
        assert EVIDENCE_TABLES <= set(inspector.get_table_names())

        assert {
            "id",
            "sleeve",
            "session_date",
            "status",
            "baseline_id",
            "window_sessions",
            "threshold",
            "metric_value",
            "created_at",
        } <= {c["name"] for c in inspector.get_columns("divergence_daily")}
        assert {"id", "label", "rung", "manifest", "started_at"} <= {
            c["name"] for c in inspector.get_columns("gate_epochs")
        }
        assert {
            "id",
            "epoch_id",
            "event_type",
            "rung_after",
            "incident_id",
            "reason",
            "detail",
            "occurred_at",
        } <= {c["name"] for c in inspector.get_columns("gate_epoch_events")}
        assert {
            "id",
            "epoch_id",
            "drill_type",
            "passed",
            "detail",
            "occurred_at",
        } <= {c["name"] for c in inspector.get_columns("drill_outcomes")}

        assert {
            "ix_divergence_daily_session_date",
            "ix_divergence_daily_sleeve_date",
        } <= {i["name"] for i in inspector.get_indexes("divergence_daily")}
        assert "ix_gate_epochs_rung_started" in {
            i["name"] for i in inspector.get_indexes("gate_epochs")
        }
        assert "ix_gate_epoch_events_epoch_occurred" in {
            i["name"] for i in inspector.get_indexes("gate_epoch_events")
        }
        assert "ix_drill_outcomes_epoch_type" in {
            i["name"] for i in inspector.get_indexes("drill_outcomes")
        }


def test_session_date_is_a_date_not_a_timestamp(monkeypatch, tmp_path):
    """AC 2b — the monitor's run time must not decide which session a verdict describes."""
    database_url = f"sqlite:///{tmp_path / 'evidence_date.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)

    command.upgrade(config, "head")

    with create_engine(database_url).connect() as connection:
        columns = {
            c["name"]: c["type"]
            for c in inspect(connection).get_columns("divergence_daily")
        }
        assert str(columns["session_date"]).upper() == "DATE"


def test_evidence_migration_downgrades_cleanly(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'evidence_down.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, PREVIOUS_REVISION)

    with create_engine(database_url).connect() as connection:
        remaining = set(inspect(connection).get_table_names())
        assert not (EVIDENCE_TABLES & remaining)


def test_evidence_migration_round_trips(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'evidence_round.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)

    command.upgrade(config, "head")
    command.downgrade(config, PREVIOUS_REVISION)
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert EVIDENCE_TABLES <= set(inspect(connection).get_table_names())
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == ScriptDirectory.from_config(config).get_heads()[0]
        )


def test_evidence_revision_keeps_a_single_head(monkeypatch, tmp_path):
    """AC 3 — the new revision extends the chain linearly instead of forking it."""
    database_url = f"sqlite:///{tmp_path / 'evidence_head.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)

    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()

    assert len(heads) == 1
    assert script.get_revision(heads[0]).down_revision == PREVIOUS_REVISION
