"""KAN-44: run_daily stamps the currency context onto every snapshot it writes.

The rate comes from the ``CapitalBudget`` the funding check already consumes,
so this adds no IB call. Without a budget the columns stay NULL — which is
what every row written before this story looks like.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from scripts.paper_state import PaperTradingState
from scripts.run_backtest import PortfolioConfig
from scripts.run_paper import run_daily as _run_daily
from services.risk_management.engine import RiskEngine
from shared.capital import CapitalBudget
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot

FX_CAPTURED_AT = datetime(2026, 7, 1, 20, 55, tzinfo=timezone.utc)


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


def make_bars(close: float = 100.0, n: int = 5) -> list[dict]:
    return [
        {
            "date": f"2026-07-0{i + 1}",
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1000,
        }
        for i in range(n)
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


def budget(fx: float = 1.35) -> CapitalBudget:
    return CapitalBudget(
        base_currency="SGD",
        trading_currency="USD",
        net_liquidation_base=13_500.0,
        net_liquidation_trading_equivalent=10_000.0,
        fx_base_per_trading=fx,
        fx_captured_at=FX_CAPTURED_AT,
        fractional_base=13_500.0,
        deployment_fraction=1.0,
        max_deployable_usd=None,
        settled_cash_trading=10_000.0,
        deployable_capital=10_000.0,
        sleeve_budgets={"test_sleeve": 10_000.0},
    )


def run_daily(state: PaperTradingState, **kwargs):
    return _run_daily(
        state,
        portfolios(),
        {"AAPL": make_bars()},
        settled_cash_trading=1_000_000,
        active_buy_reservations_usd=0,
        commission_per_share_usd=0.005,
        minimum_commission_usd=1,
        minimum_settled_usd_reserve=0,
        **kwargs,
    )


def _snapshots(session: Session) -> dict[str, EquitySnapshot]:
    rows = session.execute(select(EquitySnapshot)).scalars().all()
    return {row.portfolio: row for row in rows}


def test_run_daily_stamps_the_currency_context_on_every_snapshot(
    session: Session, state: PaperTradingState
):
    run_daily(state, capital=budget())

    rows = _snapshots(session)
    assert set(rows) == {"test_sleeve", "_aggregate"}
    for row in rows.values():
        assert row.base_currency == "SGD"
        assert row.trading_currency == "USD"
        assert row.fx_base_per_trading == 1.35
        assert row.equity_trading == pytest.approx(row.equity)
        assert row.equity_base == pytest.approx(row.equity * 1.35)
        assert row.valuation_at is not None


def test_run_daily_without_a_budget_writes_legacy_shaped_rows(
    session: Session, state: PaperTradingState
):
    run_daily(state)

    for row in _snapshots(session).values():
        assert row.equity == pytest.approx(10_000.0)
        assert row.fx_base_per_trading is None
        assert row.equity_base is None
        assert row.base_currency is None
        assert row.valuation_at is None


def test_valuation_at_is_the_rate_capture_instant(
    session: Session, state: PaperTradingState
):
    """A stamped row must say when its rate was true, not when it was stored."""
    run_daily(state, capital=budget())

    row = _snapshots(session)["test_sleeve"]
    stored = row.valuation_at
    if stored.tzinfo is None:  # SQLite drops the offset
        stored = stored.replace(tzinfo=timezone.utc)
    assert stored == FX_CAPTURED_AT


def test_a_second_run_the_same_day_rewrites_the_rate(
    session: Session, state: PaperTradingState
):
    """Upsert path: the catch-up run's rate replaces the aborted run's."""
    run_daily(state, capital=budget(fx=1.35))
    run_daily(state, capital=budget(fx=1.30))

    rows = session.execute(
        select(EquitySnapshot).where(EquitySnapshot.date == date.today())
    ).scalars().all()
    assert len(rows) == 2  # one sleeve + the rollup, not four
    for row in rows:
        assert row.fx_base_per_trading == 1.30
        assert row.equity_base == pytest.approx(row.equity * 1.30)
