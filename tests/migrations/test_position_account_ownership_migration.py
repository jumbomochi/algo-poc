from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[2]


def _config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_upgrade_adds_nullable_account_owner_without_rewriting_legacy_row(
    monkeypatch, tmp_path
):
    database_url = f"sqlite:///{tmp_path / 'ownership.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)
    command.upgrade(config, "c3a947f26510")
    with create_engine(database_url).begin() as connection:
        connection.execute(text(
            "INSERT INTO positions "
            "(ticker, portfolio, con_id, exchange, currency, quantity, "
            "avg_entry_price, current_price, peak_price, "
            "highest_price_since_entry, opened_at, status) VALUES "
            "('AAPL', 'momentum', 265598, 'SMART', 'USD', 1, 100, 100, "
            "100, 100, '2026-07-22 00:00:00', 'open')"
        ))

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    with engine.connect() as connection:
        columns = {
            item["name"]: item
            for item in inspect(connection).get_columns("positions")
        }
        assert columns["account_id"]["nullable"] is True
        row = connection.execute(text(
            "SELECT account_id, quantity FROM positions WHERE ticker='AAPL'"
        )).one()
        assert row.account_id is None
        assert row.quantity == 1
        indexes = {
            item["name"]
            for item in inspect(connection).get_indexes("positions")
        }
        assert "ix_positions_account_contract" in indexes


def test_position_account_migration_downgrades_cleanly(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'downgrade.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "c3a947f26510")

    with create_engine(database_url).connect() as connection:
        columns = {
            item["name"]
            for item in inspect(connection).get_columns("positions")
        }
        assert "account_id" not in columns
