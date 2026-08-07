from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_HEAD = "52cd3dc99a3f"
CONTENT_HASH_REVISION = "e7a1c4d92f3b"


def _config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_upgrade_adds_nullable_content_hash_without_touching_existing_row(
    monkeypatch, tmp_path
):
    database_url = f"sqlite:///{tmp_path / 'content_hash.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)
    command.upgrade(config, PREVIOUS_HEAD)

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO model_versions "
                "(version, training_window_start, training_window_end, "
                "metrics, model_path, is_active, created_at) VALUES "
                "('v1.0.0', '2024-01-01', '2024-06-30', '{}', "
                "'models/v1.0.0.joblib', 1, '2024-07-01 00:00:00')"
            )
        )

    command.upgrade(config, CONTENT_HASH_REVISION)

    with engine.connect() as connection:
        columns = {
            item["name"]: item
            for item in inspect(connection).get_columns("model_versions")
        }
        assert "content_hash" in columns
        assert columns["content_hash"]["nullable"] is True

        row = connection.execute(
            text(
                "SELECT version, model_path, content_hash "
                "FROM model_versions WHERE version = 'v1.0.0'"
            )
        ).mappings().one()
        assert row["model_path"] == "models/v1.0.0.joblib"
        assert row["content_hash"] is None

        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == CONTENT_HASH_REVISION


def test_downgrade_drops_content_hash_column(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'downgrade.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)
    command.upgrade(config, CONTENT_HASH_REVISION)

    command.downgrade(config, PREVIOUS_HEAD)

    engine = create_engine(database_url)
    with engine.connect() as connection:
        columns = {
            item["name"] for item in inspect(connection).get_columns("model_versions")
        }
        assert "content_hash" not in columns


def test_fresh_upgrade_reaches_single_head(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'fresh_head.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)

    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == heads[0]
        assert "content_hash" in {
            item["name"] for item in inspect(connection).get_columns("model_versions")
        }
