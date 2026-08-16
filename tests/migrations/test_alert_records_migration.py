from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect


ROOT = Path(__file__).resolve().parents[2]

PREVIOUS_REVISION = "d4b8e1f5a207"


def _config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_upgrade_creates_alert_records(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'alerts.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)

    command.upgrade(config, "head")

    with create_engine(database_url).connect() as connection:
        inspector = inspect(connection)
        assert "alert_records" in inspector.get_table_names()
        assert {
            "id",
            "message_id",
            "event_type",
            "priority",
            "message",
            "context",
            "raised_at",
            "resolved_at",
            "resolved_by",
            "recorded_at",
        } <= {c["name"] for c in inspector.get_columns("alert_records")}

        indexes = {i["name"] for i in inspector.get_indexes("alert_records")}
        assert "ix_alert_records_priority_raised" in indexes
        # At-least-once delivery replays pending alerts on restart; the stream
        # message id is what stops a replay becoming a second incident.
        assert "uq_alert_records_message_id" in {
            u["name"] for u in inspector.get_unique_constraints("alert_records")
        }


def test_alert_records_extends_the_chain_without_forking_it(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'alerts_head.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)

    script = ScriptDirectory.from_config(_config(database_url))

    assert len(script.get_heads()) == 1
    assert (
        script.get_revision("a5f3c81d0e72").down_revision == PREVIOUS_REVISION
    )


def test_downgrade_removes_alert_records(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'alerts_down.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)

    command.upgrade(config, "head")
    command.downgrade(config, PREVIOUS_REVISION)

    with create_engine(database_url).connect() as connection:
        assert "alert_records" not in inspect(connection).get_table_names()
