from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from migrations.versions import c3a947f26510_track_fill_projection_outcome as migration


ROOT = Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def migration_op(*, has_execution_fills: bool) -> MagicMock:
    operation = MagicMock()
    operation.get_bind.return_value.scalar.return_value = has_execution_fills
    return operation


def test_upgrade_adds_projection_marker_when_execution_fill_table_is_empty(
    monkeypatch,
):
    operation = migration_op(has_execution_fills=False)
    monkeypatch.setattr(migration, "op", operation)

    migration.upgrade()

    operation.add_column.assert_called_once()
    operation.alter_column.assert_not_called()


def test_upgrade_refuses_to_classify_preexisting_execution_fills(monkeypatch):
    operation = migration_op(has_execution_fills=True)
    monkeypatch.setattr(migration, "op", operation)

    with pytest.raises(RuntimeError, match="execution_fills must be empty"):
        migration.upgrade()

    operation.add_column.assert_not_called()
    operation.alter_column.assert_not_called()


def test_downgrade_removes_projection_marker(monkeypatch):
    operation = MagicMock()
    monkeypatch.setattr(migration, "op", operation)

    migration.downgrade()

    operation.drop_column.assert_called_once_with(
        "execution_fills", "projection_applied"
    )


def test_real_sqlite_memory_upgrade_reaches_head(monkeypatch):
    monkeypatch.setenv("ALGO_DATABASE_URL", "sqlite:///:memory:")

    command.upgrade(alembic_config(), "head")


def test_real_sqlite_upgrade_and_downgrade_projection_marker(monkeypatch, tmp_path):
    database = tmp_path / "migration.db"
    database_url = f"sqlite:///{database}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = alembic_config()

    command.upgrade(config, "head")
    with create_engine(database_url).connect() as connection:
        assert "projection_applied" in {
            column["name"]
            for column in inspect(connection).get_columns("execution_fills")
        }

    command.downgrade(config, "8b6f2c1d4a90")
    with create_engine(database_url).connect() as connection:
        assert "projection_applied" not in {
            column["name"]
            for column in inspect(connection).get_columns("execution_fills")
        }


def test_offline_sql_fails_clearly_when_empty_precondition_cannot_be_checked(
    monkeypatch,
):
    monkeypatch.setenv(
        "ALGO_DATABASE_URL", "postgresql://algo:algo@localhost/algo_poc"
    )

    with pytest.raises(RuntimeError, match="offline.*empty precondition"):
        command.upgrade(alembic_config(), "head", sql=True)
