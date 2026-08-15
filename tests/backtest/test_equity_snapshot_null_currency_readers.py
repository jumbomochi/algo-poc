"""KAN-44 AC3: every existing reader of ``equity_snapshots`` still works.

The eight currency columns are NULL on every row written before this story,
and stay NULL for any caller that supplies no FX rate. These tests pin that
the four readers named in the spec are indifferent to that — a mixed table
(some rows stamped, some not) reads exactly as it did before.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from scripts.divergence_monitor import load_live_equity_series
from scripts.paper_state import PaperTradingState
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.position_loader import load_portfolio_state

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parents[2]


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
        portfolio_capitals={"momentum": 10_000}, session=session
    )


def _legacy_row(session: Session, day: date, equity: float, portfolio="momentum"):
    """A row exactly as written before KAN-44: all eight columns NULL."""
    session.add(
        EquitySnapshot(
            portfolio=portfolio,
            date=day,
            equity=equity,
            cash=equity,
            market_value=0.0,
            created_at=NOW,
        )
    )
    session.flush()


def test_peak_nav_reads_a_mixed_table_unchanged(
    session: Session, state: PaperTradingState
):
    """position_loader sums ``equity``; a stamped row must not shift it."""
    _legacy_row(session, date(2026, 6, 1), 12_000.0)
    _legacy_row(session, date(2026, 6, 2), 15_000.0)
    # A stamped row whose base-currency value is far higher than its USD one.
    state.record_equity_snapshot(
        "momentum",
        date(2026, 6, 3),
        11_000.0,
        11_000.0,
        0.0,
        base_currency="SGD",
        trading_currency="USD",
        fx_base_per_trading=1.35,
        valuation_at=NOW,
    )

    # 15_000 still wins: peak_nav is computed on ``equity``, and the stamped
    # row's 14_850 SGD equivalent never enters the comparison.
    assert load_portfolio_state(session)["peak_nav"] == pytest.approx(15_000.0)


def test_get_equity_history_reads_legacy_rows(
    session: Session, state: PaperTradingState
):
    _legacy_row(session, date(2026, 6, 1), 12_000.0)
    _legacy_row(session, date(2026, 6, 2), 15_000.0)

    history = state.get_equity_history("momentum")
    assert [h["date"] for h in history] == ["2026-06-01", "2026-06-02"]
    assert [h["equity"] for h in history] == [12_000.0, 15_000.0]
    assert [h["cash"] for h in history] == [12_000.0, 15_000.0]
    assert [h["market_value"] for h in history] == [0.0, 0.0]
    assert all(h["fx_base_per_trading"] is None for h in history)


def test_divergence_monitor_series_unaffected_by_null_columns(
    session: Session, state: PaperTradingState
):
    _legacy_row(session, date(2026, 6, 1), 12_000.0)
    state.record_equity_snapshot(
        "momentum",
        date(2026, 6, 2),
        15_000.0,
        15_000.0,
        0.0,
        base_currency="SGD",
        trading_currency="USD",
        fx_base_per_trading=1.35,
        valuation_at=NOW,
    )

    series = load_live_equity_series(state, "momentum")
    assert series == {
        date(2026, 6, 1): 12_000.0,
        date(2026, 6, 2): 15_000.0,
    }


def test_pipeline_report_query_still_reads_the_legacy_column():
    """The launchd report's SQL is a string; nothing type-checks it.

    It must keep reading ``equity`` (present on every row ever written) rather
    than a currency-qualified column that is NULL for the entire pre-cutover
    history, and must keep excluding ``_``-prefixed portfolios.
    """
    script = (REPO_ROOT / "deploy/launchd/run_pipeline_report.sh").read_text()
    query = next(
        line for line in script.splitlines() if "FROM equity_snapshots" in line
    )
    assert "SUM(equity)" in query
    assert "equity_base" not in query
    assert r"NOT LIKE '\_%'" in query
