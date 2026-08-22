"""Guards on the migration that widens the over-narrow ``trades`` columns (KAN-61).

``trades.recommendation_id`` was ``varchar(50)`` while the same identifier is
``varchar(255)`` on both ``order_intents`` and ``execution_fills``. Account-scoped
ids outgrew 50 characters when account identity entered the format, so every
projection raised ``StringDataRightTruncation`` and the projector crash-looped
without ever writing a row.

``trades.exit_reason`` is the same defect one field over, and it is worse: it is
fed from ``OrderIntent.reason``, which is ``Text`` and therefore unbounded, so no
``varchar`` bound is defensible. Reasons already in the codebase exceed 50
characters.

The width is asserted against the ORM rather than against a literal, so the
migration and ``shared/models/portfolio.py`` cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from shared.models.portfolio import Position, Trade


ROOT = Path(__file__).resolve().parents[2]

REVISION = "f2c9a6d81b74"
PREVIOUS_REVISION = "a5f3c81d0e72"

# The pre-migration state, and what a downgrade must restore.
NARROW_WIDTH = 50


def _config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _columns(database_url: str, table: str) -> dict[str, object]:
    with create_engine(database_url).connect() as connection:
        return {c["name"]: c["type"] for c in inspect(connection).get_columns(table)}


def _orm_length(model: type, attribute: str) -> int | None:
    return model.__table__.c[attribute].type.length


def test_upgrade_widens_recommendation_id_to_the_orm_width(monkeypatch, tmp_path):
    """The column must end up as wide as the ORM says, not as wide as a literal."""
    database_url = f"sqlite:///{tmp_path / 'widen.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)

    command.upgrade(_config(database_url), "head")

    orm_width = _orm_length(Trade, "recommendation_id")
    assert orm_width == 255, (
        "shared/models/portfolio.py must declare recommendation_id as "
        f"String(255) to match order_intents; it declares String({orm_width})"
    )
    assert _columns(database_url, "trades")["recommendation_id"].length == orm_width


def test_upgrade_makes_exit_reason_unbounded(monkeypatch, tmp_path):
    """exit_reason is fed from OrderIntent.reason, which is Text and unbounded.

    A varchar bound here is guesswork that the next long reason string defeats,
    which is the exact shape of the bug this migration exists to fix.
    """
    database_url = f"sqlite:///{tmp_path / 'widen_reason.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)

    command.upgrade(_config(database_url), "head")

    assert _orm_length(Trade, "exit_reason") is None, (
        "trades.exit_reason must be Text (unbounded) in the ORM, because its "
        "source column OrderIntent.reason is Text"
    )
    assert _columns(database_url, "trades")["exit_reason"].length is None


def test_migration_extends_the_chain_without_forking_it(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'widen_head.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)

    script = ScriptDirectory.from_config(_config(database_url))

    assert len(script.get_heads()) == 1
    assert script.get_revision(REVISION).down_revision == PREVIOUS_REVISION


def test_downgrade_restores_the_narrow_columns(monkeypatch, tmp_path):
    """Round-trip. See the revision docstring on why this is dev-only in practice."""
    database_url = f"sqlite:///{tmp_path / 'widen_down.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)
    config = _config(database_url)

    command.upgrade(config, "head")
    command.downgrade(config, PREVIOUS_REVISION)

    columns = _columns(database_url, "trades")
    assert columns["recommendation_id"].length == NARROW_WIDTH
    assert columns["exit_reason"].length == NARROW_WIDTH


def test_untouched_columns_survive_the_batch_rebuild(monkeypatch, tmp_path):
    """sqlite runs batch mode as copy-and-rename, so the rest of the table is at risk.

    Postgres does a plain ALTER and would never lose anything, but the tests run
    on sqlite -- so a mistake in the batch block shows up here rather than in
    production.
    """
    database_url = f"sqlite:///{tmp_path / 'widen_intact.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", database_url)

    command.upgrade(_config(database_url), "head")

    columns = _columns(database_url, "trades")
    assert set(columns) == {c.name for c in Trade.__table__.columns}

    with create_engine(database_url).connect() as connection:
        indexes = {i["name"] for i in inspect(connection).get_indexes("trades")}
    assert {
        "ix_trade_ticker_executed",
        "ix_trade_recommendation",
        "ix_trade_portfolio",
    } <= indexes, f"batch rebuild dropped an index: {indexes}"


def test_documented_sibling_columns_still_fit_their_formats():
    """AC6: the String(50) siblings left alone must stay comfortably bounded.

    Each was measured from code rather than assumed (KAN-61):

      trades.portfolio      longest sleeve name is 'thematic_momentum' (17);
                            synthetic tags '__liquidation__' (15), '__drill__' (9)
      positions.sector      longest GICS name is 'Consumer Discretionary' (22)
      positions.account_id  IB account ids are 9 characters (e.g. DU/U + digits)

    These stay at String(50). If a format outgrows its column the way
    recommendation_id did, this test is where that shows up first.
    """
    assert _orm_length(Trade, "portfolio") == 50
    assert _orm_length(Position, "sector") == 50
    assert _orm_length(Position, "account_id") == 50

    longest_known = {
        "trades.portfolio": len("thematic_momentum"),
        "positions.sector": len("Consumer Discretionary"),
        "positions.account_id": len("DU1234567"),
    }
    # Two-times headroom. recommendation_id died at 1.16x its bound, so "it
    # fits today" is not the standard being applied here.
    for name, longest in longest_known.items():
        assert longest * 2 <= 50, f"{name} no longer has 2x headroom: {longest}"
