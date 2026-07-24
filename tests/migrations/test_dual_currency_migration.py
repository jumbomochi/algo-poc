from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from shared import models

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_HEAD = "d8f10a4b72c3"
DUAL_CURRENCY_REVISION = "f6c2d9a84b31"

CAPITAL_COLUMNS = {
    "base_currency",
    "trading_currency",
    "net_liquidation_base",
    "net_liquidation_trading_equivalent",
    "fx_base_per_trading",
    "fx_captured_at",
    "fractional_base",
    "settled_cash_trading",
}
EQUITY_COLUMNS = {
    "base_currency",
    "trading_currency",
    "equity_trading",
    "cash_trading",
    "market_value_trading",
    "fx_base_per_trading",
    "equity_base",
    "valuation_at",
}
CURRENCY_CONVERSION_COLUMNS = {
    "id",
    "account_id",
    "source_currency",
    "source_amount",
    "target_currency",
    "target_amount",
    "fx_base_per_trading",
    "fee_amount",
    "fee_currency",
    "source",
    "operator",
    "executed_at",
}


def _config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _column_names(inspector, table_name: str) -> set[str]:
    return {
        column["name"] for column in inspector.get_columns(table_name)
    }


def test_orm_models_expose_explicit_dual_currency_fields():
    assert hasattr(models, "CurrencyConversion")
    assert CAPITAL_COLUMNS <= set(
        models.CapitalSnapshot.__table__.columns.keys()
    )
    assert EQUITY_COLUMNS <= set(
        models.EquitySnapshot.__table__.columns.keys()
    )
    assert {"commission_currency", "commission_trading"} <= set(
        models.ExecutionFill.__table__.columns.keys()
    )
    assert (
        models.PortfolioConfig.__table__.columns["currency"].default.arg
        == "USD"
    )
    assert set(models.CurrencyConversion.__table__.columns.keys()) == (
        CURRENCY_CONVERSION_COLUMNS
    )


def test_upgrade_preserves_existing_portfolio_and_adds_currency_schema(
    monkeypatch, tmp_path
):
    database_url = f"sqlite:///{tmp_path / 'dual_currency.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)
    command.upgrade(config, PREVIOUS_HEAD)

    original = {
        "portfolio": "momentum",
        "capital": 600_000.0,
        "cash": 575_000.0,
        "created_at": "2026-07-01 00:00:00",
        "updated_at": "2026-07-23 00:00:00",
    }
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO portfolio_config "
                "(portfolio, capital, cash, created_at, updated_at) "
                "VALUES (:portfolio, :capital, :cash, :created_at, :updated_at)"
            ),
            original,
        )
        before_row = connection.execute(
            text(
                "SELECT portfolio, capital, cash, created_at, updated_at "
                "FROM portfolio_config WHERE portfolio = 'momentum'"
            )
        ).mappings().one()

    command.upgrade(config, DUAL_CURRENCY_REVISION)

    with engine.connect() as connection:
        inspector = inspect(connection)
        assert CAPITAL_COLUMNS <= _column_names(
            inspector, "capital_snapshots"
        )
        assert EQUITY_COLUMNS <= _column_names(inspector, "equity_snapshots")
        assert {"commission_currency", "commission_trading"} <= _column_names(
            inspector, "execution_fills"
        )
        assert "currency" in _column_names(inspector, "portfolio_config")
        assert "currency_conversions" in inspector.get_table_names()
        assert _column_names(
            inspector, "currency_conversions"
        ) == CURRENCY_CONVERSION_COLUMNS

        row = connection.execute(
            text(
                "SELECT portfolio, capital, cash, created_at, updated_at, "
                "currency FROM portfolio_config WHERE portfolio = 'momentum'"
            )
        ).mappings().one()
        assert {
            key: row[key] for key in before_row
        } == dict(before_row)
        assert row["currency"] == "USD"

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == DUAL_CURRENCY_REVISION
