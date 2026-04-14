from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from shared.models.base import Base
from shared.models.ml_models import ModelVersion
from scripts.paper_state import PaperTradingState
from scripts.retrain_model import (
    load_trades_from_db,
    compare_models,
    run_retraining_pipeline,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


def _seed_trades(session: Session, n: int = 250) -> None:
    """Seed DB with n trades across two portfolios."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 50_000, "mom": 50_000},
        session=session,
    )
    import random
    random.seed(42)
    tickers = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "JPM"]

    for i in range(n):
        portfolio = "mr" if i % 2 == 0 else "mom"
        ticker = tickers[i % len(tickers)]
        entry_price = 100 + random.uniform(-20, 20)
        exit_price = entry_price + random.uniform(-10, 15)
        entry_d = date(2023, 1, 1 + (i % 28))
        exit_d = date(2023, 2, 1 + (i % 28))

        state.record_fill(
            portfolio=portfolio, ticker=ticker, action="buy",
            quantity=10, price=entry_price, fill_date=entry_d,
            entry_signals={"rsi": random.uniform(20, 80), "rank": random.randint(1, 10)},
        )
        state.record_fill(
            portfolio=portfolio, ticker=ticker, action="sell",
            quantity=10, price=exit_price, fill_date=exit_d,
            exit_reason="trailing_stop",
            bar_features={"return_5d": random.uniform(-0.1, 0.1), "vol_20d": random.uniform(0.01, 0.05)},
        )

    session.flush()


def test_load_trades_from_db(db_session: Session):
    """load_trades_from_db should return trade dicts with ML-ready fields."""
    _seed_trades(db_session, n=50)
    trades = load_trades_from_db(db_session)
    assert len(trades) == 50
    for t in trades:
        assert "pnl" in t
        assert "entry_date" in t
        assert "portfolio" in t
        assert "entry_signals" in t


def test_load_trades_filters_null_signals(db_session: Session):
    """Trades without entry_signals should be excluded."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        session=db_session,
    )
    # Buy without entry_signals
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1))
    state.record_fill("mr", "AAPL", "sell", 10, 160.0, date(2024, 1, 15))
    # Buy with entry_signals
    state.record_fill("mr", "MSFT", "buy", 10, 300.0, date(2024, 1, 1),
                       entry_signals={"rsi": 30})
    state.record_fill("mr", "MSFT", "sell", 10, 310.0, date(2024, 1, 15))
    db_session.flush()

    trades = load_trades_from_db(db_session)
    assert len(trades) == 1
    assert trades[0]["ticker"] == "MSFT"


def test_compare_models_promote_when_better():
    """New model should be promoted when metrics improve."""
    current = {"accuracy": 0.55, "filtered_win_rate": 0.60}
    new = {"accuracy": 0.58, "filtered_win_rate": 0.62}
    assert compare_models(current_metrics=current, new_metrics=new) is True


def test_compare_models_skip_when_worse():
    """New model should be skipped when accuracy regresses."""
    current = {"accuracy": 0.60, "filtered_win_rate": 0.65}
    new = {"accuracy": 0.55, "filtered_win_rate": 0.63}
    assert compare_models(current_metrics=current, new_metrics=new) is False


def test_compare_models_promote_when_no_current():
    """First model should always be promoted."""
    assert compare_models(current_metrics=None, new_metrics={"accuracy": 0.5, "filtered_win_rate": 0.5}) is True


def test_pipeline_skips_when_too_few_trades(db_session: Session):
    """Pipeline should skip if trade count < min_training_samples."""
    _seed_trades(db_session, n=10)
    result = run_retraining_pipeline(
        session=db_session, min_samples=200, output_dir="/tmp/test_models",
        dry_run=True,
    )
    assert result["status"] == "skipped"
    assert "insufficient" in result["reason"].lower()


def test_pipeline_trains_and_evaluates(db_session: Session):
    """Pipeline should train model and return metrics when enough trades."""
    _seed_trades(db_session, n=250)
    result = run_retraining_pipeline(
        session=db_session, min_samples=50, output_dir="/tmp/test_models",
        dry_run=True,
    )
    assert result["status"] in ("promoted", "skipped", "dry_run")
    assert "accuracy" in result["metrics"]
    assert "filtered_win_rate" in result["metrics"]
