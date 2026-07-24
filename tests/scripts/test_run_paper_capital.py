from __future__ import annotations

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from scripts.run_paper import _parser, prepare_daily_run, read_broker_snapshot
from shared.broker_state import BrokerAccountSnapshot
from shared.config import AppConfig, CapitalConfig, CapitalModeConfig
from shared.models import Base, CapitalSnapshot, ReconciliationReport


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def test_sgd_nav_builds_and_persists_usd_sleeve_budgets(session):
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
    captured_at = datetime.now(UTC)
    snapshot = BrokerAccountSnapshot(
        account_id="DUTEST",
        mode="paper",
        base_currency="SGD",
        trading_currency="USD",
        net_liquidation_base=1_001_757.23,
        fx_base_per_trading=1.2928304,
        net_liquidation_trading_equivalent=774_855.87,
        settled_cash_trading=25_000,
        fx_source="test",
        fx_captured_at=captured_at,
        captured_at=captured_at,
    )

    result = prepare_daily_run(
        broker_snapshot=snapshot,
        config=config,
        session=session,
    )

    assert sum(result.capital.sleeve_budgets.values()) == pytest.approx(774_855.87)
    stored = session.scalar(select(CapitalSnapshot))
    assert stored is not None
    assert stored.account_id == "DUTEST"
    assert stored.base_currency == "SGD"
    assert stored.trading_currency == "USD"
    assert stored.net_liquidation_base == pytest.approx(1_001_757.23)
    assert stored.net_liquidation_trading_equivalent == pytest.approx(774_855.87)
    assert stored.fx_base_per_trading == pytest.approx(1.2928304)
    assert stored.settled_cash_trading == pytest.approx(25_000)
    assert stored.net_liquidation == pytest.approx(774_855.87)
    assert stored.deployable_capital == pytest.approx(774_855.87)
    assert stored.reconciliation_status == "ok"
    assert result.reconciliation.entries_allowed is True


def test_legacy_unowned_position_keeps_daily_preparation_fail_closed(session):
    from shared.models import Position

    captured_at = datetime.now(UTC)
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
            opened_at=datetime(2026, 7, 1, tzinfo=UTC),
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
        fx_captured_at=captured_at,
        captured_at=captured_at,
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


@pytest.mark.parametrize(
    ("snapshot_change", "error"),
    [
        ({"fx_base_per_trading": 0}, "FX rate must be positive"),
        (
            {"fx_captured_at": datetime.now(UTC) - timedelta(seconds=301)},
            "FX quote is stale",
        ),
    ],
)
def test_invalid_or_stale_currency_data_does_not_persist_daily_state(
    session, snapshot_change, error
):
    captured_at = datetime.now(UTC)
    snapshot = BrokerAccountSnapshot(
        account_id="DUTEST",
        mode="paper",
        base_currency="SGD",
        trading_currency="USD",
        net_liquidation_base=1_001_757.23,
        fx_base_per_trading=1.2928304,
        net_liquidation_trading_equivalent=774_855.87,
        settled_cash_trading=25_000,
        fx_source="test",
        fx_captured_at=captured_at,
        captured_at=captured_at,
    )

    with pytest.raises(ValueError, match=error):
        prepare_daily_run(
            broker_snapshot=replace(snapshot, **snapshot_change),
            config=AppConfig(mode="paper"),
            session=session,
        )

    assert session.scalar(select(CapitalSnapshot)) is None
    assert session.scalar(select(ReconciliationReport)) is None


def test_entries_are_disabled_by_default_and_require_explicit_rollout_override():
    parser = _parser("sqlite://", "redis://localhost")

    assert parser.parse_args([]).entries_disabled is True
    assert parser.parse_args(["--no-entries-disabled"]).entries_disabled is False


@pytest.mark.asyncio
async def test_read_broker_snapshot_passes_explicit_currency_configuration(monkeypatch):
    ib = MagicMock()
    ib.connectAsync = AsyncMock()
    ib.isConnected.return_value = True
    monkeypatch.setitem(sys.modules, "ib_insync", SimpleNamespace(IB=lambda: ib))
    snapshot = object()
    reader = MagicMock()
    reader.snapshot = AsyncMock(return_value=snapshot)

    with patch("scripts.run_paper.IBAccountReader", return_value=reader) as reader_class:
        result = await read_broker_snapshot(
            host="127.0.0.1",
            port=7497,
            client_id=54,
            mode="paper",
            expected_base_currency="SGD",
            trading_currency="USD",
        )

    assert result is snapshot
    reader_class.assert_called_once_with(
        ib,
        expected_mode="paper",
        expected_base_currency="SGD",
        trading_currency="USD",
    )
    reader.snapshot.assert_awaited_once_with()
    ib.disconnect.assert_called_once_with()
