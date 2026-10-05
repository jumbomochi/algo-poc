"""KAN-103: run_daily stamps the US session its marks are the close of.

``date`` stays the SGT run date (``date.today()``); ``session_date`` comes from
the bars actually priced, cross-checked against ``valuation_at``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from scripts.paper_state import PaperTradingState
from scripts.run_backtest import PortfolioConfig
from scripts.run_paper import _marks_session
from scripts.run_paper import run_daily as _run_daily
from services.risk_management.engine import RiskEngine
from shared.capital import CapitalBudget
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot

# Tue 2026-09-29 04:15 SGT: Monday 09-28 closed at 04:00 SGT.
TUESDAY_0415_SGT = datetime(2026, 9, 28, 20, 15, tzinfo=timezone.utc)
# Tue 2026-11-03 04:15 SGT: Monday 11-02 closes at 05:00 SGT (EST).
WINTER_0415_SGT = datetime(2026, 11, 2, 20, 15, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture
def state(session: Session) -> PaperTradingState:
    return PaperTradingState.create_new(
        portfolio_capitals={"test_sleeve": 10_000.0}, session=session
    )


def bars_ending(*days: str) -> list[dict]:
    return [
        {"date": d, "open": 100.0, "high": 100.0, "low": 100.0,
         "close": 100.0, "volume": 1000}
        for d in days
    ]


def portfolios() -> dict[str, PortfolioConfig]:
    return {
        "test_sleeve": PortfolioConfig(
            name="test_sleeve",
            capital=10_000.0,
            signals_fn=lambda *_args, **_kwargs: None,
            risk_engine=RiskEngine(
                position_entry_limit_pct=10.0,
                sector_concentration_pct=100.0,
                total_exposure_limit_pct=100.0,
                max_lots_per_ticker=1,
            ),
        )
    }


def budget(captured_at: datetime) -> CapitalBudget:
    return CapitalBudget(
        base_currency="SGD",
        trading_currency="USD",
        net_liquidation_base=13_500.0,
        net_liquidation_trading_equivalent=10_000.0,
        fx_base_per_trading=1.35,
        fx_captured_at=captured_at,
        fractional_base=13_500.0,
        deployment_fraction=1.0,
        max_deployable_usd=None,
        settled_cash_trading=10_000.0,
        deployable_capital=10_000.0,
        sleeve_budgets={"test_sleeve": 10_000.0},
    )


def run_daily(state, bars_by_ticker, **kwargs):
    return _run_daily(
        state,
        portfolios(),
        bars_by_ticker,
        settled_cash_trading=1_000_000,
        active_buy_reservations_usd=0,
        commission_per_share_usd=0.005,
        minimum_commission_usd=1,
        minimum_settled_usd_reserve=0,
        **kwargs,
    )


def _rows(session: Session) -> dict[str, EquitySnapshot]:
    return {
        r.portfolio: r
        for r in session.execute(select(EquitySnapshot)).scalars().all()
    }


def test_a_tuesday_run_stamps_mondays_session_and_keeps_the_run_date(
    session, state
):
    run_daily(
        state,
        {"AAPL": bars_ending("2026-09-25", "2026-09-28")},
        capital=budget(TUESDAY_0415_SGT),
    )

    rows = _rows(session)
    assert set(rows) == {"test_sleeve", "_aggregate"}
    for row in rows.values():
        assert row.session_date == date(2026, 9, 28)
        assert row.date == date.today()


def test_a_partial_bar_writes_a_null_session(session, state):
    run_daily(
        state,
        {"AAPL": bars_ending("2026-10-30", "2026-11-02")},
        capital=budget(WINTER_0415_SGT),
    )

    for row in _rows(session).values():
        assert row.session_date is None
        assert row.equity == pytest.approx(10_000.0)  # the row is still written


def test_a_rerun_that_cannot_name_a_session_clears_the_earlier_stamp(
    session, state
):
    run_daily(
        state,
        {"AAPL": bars_ending("2026-09-25", "2026-09-28")},
        capital=budget(TUESDAY_0415_SGT),
    )
    run_daily(
        state,
        {"AAPL": bars_ending("2026-10-30", "2026-11-02")},
        capital=budget(WINTER_0415_SGT),
    )

    rows = _rows(session)
    assert len(rows) == 2  # upsert on the run date, not new rows
    assert all(row.session_date is None for row in rows.values())


def test_the_newest_bar_across_tickers_is_the_marks_session():
    bars = {
        "AAPL": bars_ending("2026-09-25", "2026-09-28"),
        "LAGGY": bars_ending("2026-09-24", "2026-09-25"),
        "EMPTY": [],
    }
    assert _marks_session(bars, TUESDAY_0415_SGT) == date(2026, 9, 28)


def test_no_bars_means_no_session():
    assert _marks_session({"AAPL": []}, TUESDAY_0415_SGT) is None


def test_datetime_bar_dates_are_accepted():
    bars = {"AAPL": [{"date": datetime(2026, 9, 28), "close": 1.0}]}
    assert _marks_session(bars, TUESDAY_0415_SGT) == date(2026, 9, 28)


def test_without_a_budget_the_valuation_instant_is_now(session, state):
    """A harness run has no valuation_at; a long-closed session still stamps."""
    run_daily(state, {"AAPL": bars_ending("2026-09-25", "2026-09-28")})

    for row in _rows(session).values():
        assert row.session_date == date(2026, 9, 28)
        assert row.valuation_at is None


# ---------------------------------------------------------------- paper_state


def test_paper_state_round_trips_session_date(session, state):
    state.record_equity_snapshot(
        "test_sleeve", date(2026, 9, 29), 10_100.0, 5_000.0, 5_100.0,
        session_date=date(2026, 9, 28),
    )
    history = state.get_equity_history("test_sleeve")
    assert history[0]["date"] == "2026-09-29"
    assert history[0]["session_date"] == "2026-09-28"

    state.record_equity_snapshot(
        "test_sleeve", date(2026, 9, 29), 10_200.0, 5_000.0, 5_200.0,
    )
    history = state.get_equity_history("test_sleeve")
    assert len(history) == 1
    assert history[0]["session_date"] is None
    assert history[0]["equity"] == 10_200.0


def test_paper_state_defaults_session_date_to_null(session, state):
    state.record_equity_snapshot(
        "test_sleeve", date(2026, 9, 29), 10_000.0, 10_000.0, 0.0,
    )
    (row,) = session.execute(select(EquitySnapshot)).scalars().all()
    assert row.session_date is None



# ---------------------------------------------------------------- schema guard


def test_load_refuses_a_database_behind_the_migration(tmp_path, monkeypatch):
    """A hand-run ops script fails with the fix, not a raw UndefinedColumn."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from scripts.paper_state import SchemaOutOfDateError

    url = f"sqlite:///{tmp_path / 'behind.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", url)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "007388445941")

    with Session(create_engine(url)) as s:
        with pytest.raises(SchemaOutOfDateError, match="alembic upgrade head"):
            PaperTradingState.load(s)

    command.upgrade(config, "head")
    with Session(create_engine(url)) as s:
        with pytest.raises(ValueError, match="No paper trading state"):
            PaperTradingState.load(s)  # past the schema check: an empty book
