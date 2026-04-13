from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from shared.models.base import Base
from scripts.paper_state import PaperTradingState


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database with all tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


def test_create_new_initializes_portfolio_configs(db_session: Session):
    """create_new should insert portfolio_config rows."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"momentum": 20_000, "mr": 14_000},
        session=db_session,
    )
    portfolios = state.get_portfolio_names()
    assert set(portfolios) == {"momentum", "mr"}
    assert state.get_cash("momentum") == 20_000
    assert state.get_cash("mr") == 14_000
    assert state.get_capital("momentum") == 20_000


def test_load_reads_existing_state(db_session: Session):
    """load should read portfolio_config rows created by create_new."""
    PaperTradingState.create_new(
        portfolio_capitals={"momentum": 20_000},
        session=db_session,
    )
    loaded = PaperTradingState.load(db_session)
    assert "momentum" in loaded.get_portfolio_names()
    assert loaded.get_cash("momentum") == 20_000


def test_load_raises_if_no_state(db_session: Session):
    """load should raise if no portfolio_config rows exist."""
    with pytest.raises(ValueError, match="No paper trading state"):
        PaperTradingState.load(db_session)


def test_record_buy_creates_position(db_session: Session):
    """Recording a buy should create an open position."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        session=db_session,
    )
    state.record_fill(
        portfolio="mr", ticker="AAPL", action="buy",
        quantity=10, price=150.0, fill_date=date(2024, 1, 15),
    )
    positions = state.get_positions("mr")
    assert "AAPL" in positions
    assert positions["AAPL"]["quantity"] == 10
    assert positions["AAPL"]["avg_entry_price"] == 150.0
    assert positions["AAPL"]["peak_price"] == 150.0
    assert state.get_cash("mr") == 10_000 - (150.0 * 10)


def test_record_buy_averages_into_existing(db_session: Session):
    """Buying more of same ticker should average the entry price."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 50_000},
        session=db_session,
    )
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1))
    state.record_fill("mr", "AAPL", "buy", 10, 160.0, date(2024, 1, 2))

    positions = state.get_positions("mr")
    assert positions["AAPL"]["quantity"] == 20
    assert positions["AAPL"]["avg_entry_price"] == 155.0  # (150*10 + 160*10) / 20


def test_record_sell_creates_trade(db_session: Session):
    """Selling should remove position and create a trade record."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        session=db_session,
    )
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1))
    state.record_fill(
        "mr", "AAPL", "sell", 10, 160.0, date(2024, 1, 15),
        exit_reason="trailing_stop",
    )

    positions = state.get_positions("mr")
    assert "AAPL" not in positions

    trades = state.get_trades("mr")
    assert len(trades) == 1
    assert trades[0]["ticker"] == "AAPL"
    assert trades[0]["pnl"] == 100.0  # (160 - 150) * 10
    assert trades[0]["exit_reason"] == "trailing_stop"
    assert state.get_cash("mr") == 10_100  # bought 1500, sold 1600, net +100


def test_update_peak_prices(db_session: Session):
    """update_peak_prices should update peak for held positions."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        session=db_session,
    )
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1))
    state.update_peak_prices("mr", {"AAPL": 170.0})

    positions = state.get_positions("mr")
    assert positions["AAPL"]["peak_price"] == 170.0


def test_compute_equity(db_session: Session):
    """compute_equity should return cash + market value."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        session=db_session,
    )
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1))
    # cash = 10000 - 1500 = 8500
    # market value at 160 = 1600
    equity = state.compute_equity("mr", {"AAPL": 160.0})
    assert equity == 8500.0 + 1600.0


def test_record_equity_snapshot(db_session: Session):
    """record_equity_snapshot should create a snapshot row."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        session=db_session,
    )
    state.record_equity_snapshot(
        portfolio="mr", snap_date=date(2024, 1, 15),
        equity=10_500.0, cash=8_500.0, market_value=2_000.0,
    )
    snapshots = state.get_equity_history("mr")
    assert len(snapshots) == 1
    assert snapshots[0]["equity"] == 10_500.0


def test_record_equity_snapshot_upserts(db_session: Session):
    """Recording snapshot for same portfolio+date should update, not duplicate."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        session=db_session,
    )
    state.record_equity_snapshot("mr", date(2024, 1, 15), 10_500.0, 8_500.0, 2_000.0)
    state.record_equity_snapshot("mr", date(2024, 1, 15), 10_600.0, 8_600.0, 2_000.0)

    snapshots = state.get_equity_history("mr")
    assert len(snapshots) == 1
    assert snapshots[0]["equity"] == 10_600.0


def test_record_fill_with_entry_signals(db_session: Session):
    """entry_signals should be stored on position and carried to trade."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        session=db_session,
    )
    signals = {"rsi": 28.5, "support_proximity": 0.02}
    state.record_fill(
        "mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1),
        entry_signals=signals,
    )
    positions = state.get_positions("mr")
    assert positions["AAPL"]["entry_signals"] == signals

    state.record_fill("mr", "AAPL", "sell", 10, 160.0, date(2024, 1, 15))
    trades = state.get_trades("mr")
    assert trades[0]["entry_signals"] == signals


def test_get_all_trades_for_training(db_session: Session):
    """get_all_trades should return trades across all portfolios as dicts."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000, "mom": 20_000},
        session=db_session,
    )
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1),
                       entry_signals={"rsi": 28})
    state.record_fill("mr", "AAPL", "sell", 10, 160.0, date(2024, 1, 15))
    state.record_fill("mom", "MSFT", "buy", 5, 300.0, date(2024, 1, 1))
    state.record_fill("mom", "MSFT", "sell", 5, 320.0, date(2024, 1, 15))

    all_trades = state.get_all_trades()
    assert len(all_trades) == 2
    tickers = {t["ticker"] for t in all_trades}
    assert tickers == {"AAPL", "MSFT"}
    # Each trade has the fields needed for ML training
    for t in all_trades:
        assert "pnl" in t
        assert "portfolio" in t
        assert "entry_date" in t
