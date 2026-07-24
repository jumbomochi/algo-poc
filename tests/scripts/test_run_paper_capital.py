from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from scripts.run_paper import _parser, prepare_daily_run
from shared.broker_state import BrokerAccountSnapshot
from shared.config import AppConfig, CapitalConfig, CapitalModeConfig
from shared.models import Base, CapitalSnapshot


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def test_one_million_nav_builds_and_persists_one_million_of_sleeve_budgets(session):
    config = AppConfig(
        mode="paper",
        capital=CapitalConfig(
            paper=CapitalModeConfig(
                deployment_fraction=1.0,
                max_deployable_usd=None,
                entries_enabled=False,
            )
        ),
    )
    snapshot = BrokerAccountSnapshot(
        account_id="DUTEST",
        mode="paper",
        base_currency="SGD",
        trading_currency="USD",
        net_liquidation_base=1_350_000,
        fx_base_per_trading=1.35,
        net_liquidation_trading_equivalent=1_000_000,
        settled_cash_trading=1_000_000,
        fx_source="test",
        fx_captured_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
        captured_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )

    result = prepare_daily_run(
        broker_snapshot=snapshot,
        config=config,
        session=session,
    )

    assert sum(result.capital.sleeve_budgets.values()) == pytest.approx(1_000_000)
    stored = session.scalar(select(CapitalSnapshot))
    assert stored is not None
    assert stored.account_id == "DUTEST"
    assert stored.deployable_capital == pytest.approx(1_000_000)
    assert stored.reconciliation_status == "ok"
    assert result.reconciliation.entries_allowed is True


def test_legacy_unowned_position_keeps_daily_preparation_fail_closed(session):
    from shared.models import Position

    session.add(
        Position(
            account_id=None,
            ticker="AAPL",
            portfolio="momentum",
            con_id=265598,
            quantity=1,
            avg_entry_price=100,
            current_price=100,
            peak_price=100,
            highest_price_since_entry=100,
            opened_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            status="open",
        )
    )
    snapshot = BrokerAccountSnapshot(
        account_id="DUTEST",
        mode="paper",
        base_currency="SGD",
        trading_currency="USD",
        net_liquidation_base=1_350_000,
        fx_base_per_trading=1.35,
        net_liquidation_trading_equivalent=1_000_000,
        settled_cash_trading=1_000_000,
        fx_source="test",
        fx_captured_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )

    result = prepare_daily_run(
        broker_snapshot=snapshot,
        config=AppConfig(mode="paper"),
        session=session,
    )

    assert result.reconciliation.entries_allowed is False
    assert any(
        item["type"] == "db_position_missing_account_id"
        for item in result.reconciliation.discrepancies
    )


def test_entries_are_disabled_by_default_and_require_explicit_rollout_override():
    parser = _parser("sqlite://", "redis://localhost")

    assert parser.parse_args([]).entries_disabled is True
    assert parser.parse_args(["--no-entries-disabled"]).entries_disabled is False
