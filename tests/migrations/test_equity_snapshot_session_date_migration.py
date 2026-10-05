"""KAN-103: equity_snapshots.session_date is additive and leaves history NULL."""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from shared import models

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_REVISION = "007388445941"
REVISION = "b3d5f7a9c1e2"


def _config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_the_model_carries_session_date_beside_date():
    columns = models.EquitySnapshot.__table__.columns
    assert columns["session_date"].nullable is True
    assert columns["date"].nullable is False


def test_session_date_extends_the_chain_without_forking_it(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'head.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    script = ScriptDirectory.from_config(_config(database_url))

    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == PREVIOUS_REVISION


def test_upgrade_adds_a_nullable_column_and_keeps_existing_rows(
    monkeypatch, tmp_path
):
    database_url = f"sqlite:///{tmp_path / 'session_date.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)
    command.upgrade(config, PREVIOUS_REVISION)

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO equity_snapshots "
            "(portfolio, date, equity, cash, market_value, created_at) "
            "VALUES ('momentum', '2026-09-29', 10000.0, 5000.0, 5000.0, "
            "'2026-09-28 20:15:00')"
        ))

    command.upgrade(config, REVISION)

    with engine.connect() as connection:
        inspector = inspect(connection)
        column = {
            c["name"]: c for c in inspector.get_columns("equity_snapshots")
        }["session_date"]
        assert column["nullable"] is True
        indexes = {
            i["name"]: i for i in inspector.get_indexes("equity_snapshots")
        }
        session_index = indexes["ix_equity_portfolio_session_date"]
        assert session_index["column_names"] == ["portfolio", "session_date"]
        # Holiday and catch-up duplicates are legitimate.
        assert not session_index["unique"]
        # The run-date key is untouched.
        assert indexes["ix_equity_portfolio_date"]["unique"]
        rows = connection.execute(text(
            "SELECT portfolio, date, equity, session_date FROM equity_snapshots"
        )).all()
    assert rows == [("momentum", "2026-09-29", 10000.0, None)]


def test_downgrade_removes_the_column(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'session_date_down.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)

    command.upgrade(config, "head")
    command.downgrade(config, PREVIOUS_REVISION)

    with create_engine(database_url).connect() as connection:
        inspector = inspect(connection)
        names = {c["name"] for c in inspector.get_columns("equity_snapshots")}
        indexes = {i["name"] for i in inspector.get_indexes("equity_snapshots")}
    assert "session_date" not in names
    assert "ix_equity_portfolio_session_date" not in indexes
