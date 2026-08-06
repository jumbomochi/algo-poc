"""Tests for shared/position_loader.py — DB-to-memory position loading."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.portfolio import Position
from shared.models.portfolio_config import PortfolioConfig
from shared.position_loader import load_open_positions, load_portfolio_state

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def add_position(session, ticker="AAPL", portfolio="momentum", qty=10.0,
                 entry=100.0, current=110.0, highest=115.0, sector="Tech",
                 status="open"):
    session.add(Position(
        ticker=ticker, portfolio=portfolio, quantity=qty,
        avg_entry_price=entry, current_price=current, peak_price=highest,
        highest_price_since_entry=highest, sector=sector,
        opened_at=NOW, status=status,
    ))
    session.flush()


class TestLoadOpenPositions:
    def test_loads_open_positions_keyed_by_ticker(self, session):
        add_position(session, ticker="AAPL")
        add_position(session, ticker="MSFT", qty=5.0)
        positions = load_open_positions(session)
        assert set(positions) == {"AAPL", "MSFT"}
        assert positions["AAPL"]["quantity"] == 10.0
        assert positions["AAPL"]["sector"] == "Tech"

    def test_closed_positions_excluded(self, session):
        add_position(session, ticker="AAPL", status="closed")
        assert load_open_positions(session) == {}

    def test_same_ticker_across_portfolios_aggregates(self, session):
        add_position(session, ticker="AAPL", portfolio="momentum", qty=10.0, entry=100.0, highest=115.0)
        add_position(session, ticker="AAPL", portfolio="quality_value", qty=30.0, entry=120.0, highest=125.0)
        positions = load_open_positions(session)
        agg = positions["AAPL"]
        assert agg["quantity"] == 40.0
        assert agg["avg_entry_price"] == pytest.approx(115.0)  # (10*100+30*120)/40
        assert agg["highest_price_since_entry"] == 125.0

    def test_missing_sector_resolves_from_universe_map(self, session):
        """NULL sector rows (written by the sector-blind fill projector,
        2026-07-19 to 2026-08-07) must resolve via shared.universe instead of
        collapsing into one 'Unknown' bucket that trips the concentration
        limit."""
        add_position(session, sector=None)  # AAPL
        assert load_open_positions(session)["AAPL"]["sector"] == "Technology"

    def test_stored_sector_wins_over_universe_map(self, session):
        add_position(session, sector="Tech")  # AAPL, explicit row value
        assert load_open_positions(session)["AAPL"]["sector"] == "Tech"

    def test_missing_sector_unmapped_ticker_defaults_to_unknown(self, session):
        add_position(session, ticker="ZZZTEST", sector=None)
        assert load_open_positions(session)["ZZZTEST"]["sector"] == "Unknown"


class TestLoadPortfolioState:
    def test_nav_is_cash_plus_market_value(self, session):
        session.add(PortfolioConfig(portfolio="momentum", capital=10_000.0,
                                     cash=9_000.0, created_at=NOW, updated_at=NOW))
        add_position(session, qty=10.0, current=110.0)  # market value 1100
        state = load_portfolio_state(session)
        assert state["cash"] == 9_000.0
        assert state["nav"] == pytest.approx(10_100.0)

    def test_peak_nav_from_snapshots(self, session):
        session.add(PortfolioConfig(portfolio="momentum", capital=10_000.0,
                                     cash=10_000.0, created_at=NOW, updated_at=NOW))
        for d, equity in [(date(2026, 6, 1), 12_000.0), (date(2026, 6, 2), 15_000.0), (date(2026, 6, 3), 11_000.0)]:
            session.add(EquitySnapshot(portfolio="momentum", date=d, equity=equity,
                                       cash=equity, market_value=0.0, created_at=NOW))
        session.flush()
        state = load_portfolio_state(session)
        assert state["peak_nav"] == pytest.approx(15_000.0)

    def test_peak_nav_falls_back_to_nav_on_fresh_db(self, session):
        session.add(PortfolioConfig(portfolio="momentum", capital=10_000.0,
                                     cash=10_000.0, created_at=NOW, updated_at=NOW))
        session.flush()
        state = load_portfolio_state(session)
        assert state["peak_nav"] == pytest.approx(10_000.0)

    def test_sector_exposure_percentages(self, session):
        session.add(PortfolioConfig(portfolio="momentum", capital=10_000.0,
                                     cash=7_800.0, created_at=NOW, updated_at=NOW))
        add_position(session, ticker="AAPL", qty=10.0, current=110.0, sector="Tech")  # 1100
        add_position(session, ticker="XLE", qty=10.0, current=110.0, sector="Energy")  # 1100
        state = load_portfolio_state(session)  # nav = 7800 + 2200 = 10000
        assert state["sector_exposure"]["Tech"] == pytest.approx(11.0)
        assert state["sector_exposure"]["Energy"] == pytest.approx(11.0)


class TestAggregateRowExclusion:
    """Synthetic '_aggregate' rollup rows must not double-count NAV/peak.

    Regression: peak_nav summed the _aggregate snapshot alongside the sleeve
    rows, reading 2x NAV — the risk service saw a phantom 50% drawdown and
    circuit-breakered every buy.
    """

    def test_peak_nav_excludes_aggregate_row(self, session):
        session.add(PortfolioConfig(portfolio="momentum", capital=10_000.0,
                                     cash=10_000.0, created_at=NOW, updated_at=NOW))
        d = date(2026, 7, 7)
        session.add(EquitySnapshot(portfolio="momentum", date=d, equity=10_000.0,
                                   cash=10_000.0, market_value=0.0, created_at=NOW))
        session.add(EquitySnapshot(portfolio="_aggregate", date=d, equity=10_000.0,
                                   cash=10_000.0, market_value=0.0, created_at=NOW))
        session.flush()
        state = load_portfolio_state(session)
        assert state["peak_nav"] == pytest.approx(10_000.0)  # not 20_000

    def test_cash_excludes_aggregate_row(self, session):
        session.add(PortfolioConfig(portfolio="momentum", capital=10_000.0,
                                     cash=10_000.0, created_at=NOW, updated_at=NOW))
        session.add(PortfolioConfig(portfolio="_aggregate", capital=10_000.0,
                                     cash=10_000.0, created_at=NOW, updated_at=NOW))
        session.flush()
        assert load_portfolio_state(session)["cash"] == pytest.approx(10_000.0)
