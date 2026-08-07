from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


ROOT = Path(__file__).resolve().parents[2]


def _config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_upgrade_creates_system_halt_table(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'halt.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)

    command.upgrade(config, "head")

    with create_engine(database_url).connect() as connection:
        inspector = inspect(connection)
        assert "system_halt" in inspector.get_table_names()
        columns = {c["name"] for c in inspector.get_columns("system_halt")}
        assert {
            "id",
            "mode",
            "active",
            "source",
            "reason",
            "triggered_by",
            "activated_at",
            "cleared_at",
            "cleared_by",
        } <= columns
        indexes = {i["name"] for i in inspector.get_indexes("system_halt")}
        assert "ix_system_halt_mode_active" in indexes


def test_system_halt_migration_downgrades_cleanly(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'halt_down.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, "52cd3dc99a3f")

    with create_engine(database_url).connect() as connection:
        assert "system_halt" not in inspect(connection).get_table_names()
