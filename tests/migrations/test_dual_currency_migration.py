from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from shared import models

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_HEAD = "d8f10a4b72c3"
DUAL_CURRENCY_REVISION = "f6c2d9a84b31"
COMMISSION_FX_REVISION = "b17c8e4a6d92"
# Research candidates migration sits on top of commission_fx and is the head.
CURRENT_HEAD_REVISION = "9b3d1c7e4a20"

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
BASE_EXECUTION_COMMISSION_COLUMNS = {
    "commission_currency",
    "commission_trading",
}
EXECUTION_COMMISSION_COLUMNS = {
    *BASE_EXECUTION_COMMISSION_COLUMNS,
    "commission_fx_base_per_trading",
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
    assert EXECUTION_COMMISSION_COLUMNS <= set(
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
        execution_columns = _column_names(
            inspector, "execution_fills"
        )
        assert BASE_EXECUTION_COMMISSION_COLUMNS <= execution_columns
        assert "commission_fx_base_per_trading" not in execution_columns
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


def test_commission_fx_upgrade_preserves_existing_execution_fill(
    monkeypatch, tmp_path
):
    database_url = f"sqlite:///{tmp_path / 'commission_fx.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)
    command.upgrade(config, DUAL_CURRENCY_REVISION)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO execution_fills "
            "(account_id, execution_id, ib_order_id, recommendation_id, "
            "portfolio, con_id, symbol, exchange, currency, side, quantity, "
            "price, commission, commission_currency, commission_trading, "
            "cumulative_quantity, executed_at, projection_applied) VALUES "
            "('DU12345', 'exec-1', '9', 'rec-1', 'momentum', 265598, "
            "'AAPL', 'SMART', 'USD', 'BUY', 2, 100, 1.25, 'USD', 1.25, "
            "2, '2026-07-24 00:00:00', 1)"
        ))
        before = connection.execute(text(
            "SELECT account_id, execution_id, commission, "
            "commission_currency, commission_trading "
            "FROM execution_fills WHERE execution_id = 'exec-1'"
        )).mappings().one()

    command.upgrade(config, "head")

    with engine.connect() as connection:
        assert "commission_fx_base_per_trading" in _column_names(
            inspect(connection), "execution_fills"
        )
        after = connection.execute(text(
            "SELECT account_id, execution_id, commission, "
            "commission_currency, commission_trading, "
            "commission_fx_base_per_trading "
            "FROM execution_fills WHERE execution_id = 'exec-1'"
        )).mappings().one()
        assert {key: after[key] for key in before} == dict(before)
        assert after["commission_fx_base_per_trading"] is None
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == CURRENT_HEAD_REVISION


def test_fresh_upgrade_reaches_single_head(
    monkeypatch, tmp_path
):
    database_url = f"sqlite:///{tmp_path / 'fresh_head.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)

    assert ScriptDirectory.from_config(config).get_heads() == [
        CURRENT_HEAD_REVISION
    ]
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == CURRENT_HEAD_REVISION
        # commission_fx sits in the applied chain below the head.
        assert "commission_fx_base_per_trading" in _column_names(
            inspect(connection), "execution_fills"
        )
