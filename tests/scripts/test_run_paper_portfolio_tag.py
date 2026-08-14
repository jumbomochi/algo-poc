"""`--portfolio-tag`: a drill run must not touch the graded book (KAN-24).

One drill per epoch places a real paper order and takes a real fill. Those fills
land in the tables the go-live gate reads, so the drill books into a synthetic
sleeve the graded readers exclude. See
docs/operations/drill-evidence-isolation.md for the full exclusion contract.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scripts.paper_state import PaperTradingState
from scripts.run_backtest import PortfolioConfig
from scripts.run_paper import (
    CAPITAL_ALLOCATIONS,
    DRILL_BASE_SLEEVE,
    _parser,
    build_drill_portfolio,
    build_portfolio_contexts,
    ensure_tagged_portfolio,
    run_daily as _run_daily,
    validate_portfolio_tag,
)
from services.risk_management.engine import RiskEngine
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.universe import DRILL_PORTFOLIO, UNIVERSE_REGISTRY

GRADED_SLEEVE = "momentum"


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture
def state(session):
    """A graded book with one funded sleeve, as a real run would find it."""
    return PaperTradingState.create_new({GRADED_SLEEVE: 10_000.0}, session)


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


def run_daily(*args, **kwargs):
    """Supply funding inputs so the test focuses on portfolio routing."""
    kwargs.setdefault("settled_cash_trading", 1_000_000)
    kwargs.setdefault("active_buy_reservations_usd", 0)
    kwargs.setdefault("commission_per_share_usd", 0.005)
    kwargs.setdefault("minimum_commission_usd", 1)
    kwargs.setdefault("minimum_settled_usd_reserve", 0)
    return _run_daily(*args, **kwargs)


def tagged_portfolio(tag: str, signals_fn) -> dict[str, PortfolioConfig]:
    return {
        tag: PortfolioConfig(
            name=tag,
            capital=500.0,
            signals_fn=signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=100.0,
                sector_concentration_pct=100.0,
                total_exposure_limit_pct=100.0,
                max_lots_per_ticker=1,
            ),
        )
    }


# ---------------------------------------------------------------------------
# Validation — the direction of this check is the whole point
# ---------------------------------------------------------------------------


class TestTagValidation:
    def test_excluded_names_are_accepted(self):
        assert validate_portfolio_tag(DRILL_PORTFOLIO, 500.0) == DRILL_PORTFOLIO
        assert validate_portfolio_tag("_smoke", 100.0) == "_smoke"

    def test_no_tag_is_the_default_and_needs_no_capital(self):
        assert validate_portfolio_tag(None, None) is None

    def test_a_graded_sleeve_name_is_refused(self):
        """The failure mode this guards: real drill fills inside graded evidence."""
        with pytest.raises(ValueError, match="Refusing --portfolio-tag 'momentum'"):
            validate_portfolio_tag("momentum", 500.0)

    @pytest.mark.parametrize("sleeve", sorted(CAPITAL_ALLOCATIONS))
    def test_every_graded_sleeve_is_refused(self, sleeve):
        with pytest.raises(ValueError):
            validate_portfolio_tag(sleeve, 500.0)

    def test_a_typo_without_the_prefix_is_refused(self):
        with pytest.raises(ValueError, match="drill-evidence-isolation"):
            validate_portfolio_tag("drill", 500.0)

    @pytest.mark.parametrize("capital", [None, 0.0, -1.0])
    def test_capital_is_required_and_must_be_positive(self, capital):
        with pytest.raises(ValueError, match="portfolio-tag-capital"):
            validate_portfolio_tag(DRILL_PORTFOLIO, capital)

    def test_flags_default_to_off(self):
        args = _parser("sqlite://", "redis://localhost").parse_args([])
        assert args.portfolio_tag is None
        assert args.portfolio_tag_capital is None


def test_main_exits_nonzero_on_an_invalid_tag_without_opening_the_database(
    monkeypatch, capsys
):
    """AC4: a refused tag writes nothing — the DB is never even opened."""
    from scripts import run_paper

    def fail(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("database opened despite an invalid --portfolio-tag")

    monkeypatch.setattr(run_paper, "make_db_session", fail)
    monkeypatch.setattr(run_paper.sys, "argv", [
        "run_paper.py", "--portfolio-tag", "momentum", "--portfolio-tag-capital", "500",
    ])

    with pytest.raises(SystemExit) as exc:
        run_paper.main()

    assert exc.value.code == 2
    assert "Refusing --portfolio-tag 'momentum'" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Funding and sleeve construction
# ---------------------------------------------------------------------------


class TestTaggedSleeveSetup:
    def test_tag_is_funded_from_its_own_argument(self, state, session):
        assert ensure_tagged_portfolio(state, session, DRILL_PORTFOLIO, 500.0) is True
        session.flush()
        assert state.get_cash(DRILL_PORTFOLIO) == pytest.approx(500.0)
        assert state.get_capital(DRILL_PORTFOLIO) == pytest.approx(500.0)

    def test_funding_does_not_shrink_the_graded_split(self, state, session):
        ensure_tagged_portfolio(state, session, DRILL_PORTFOLIO, 500.0)
        session.flush()
        assert state.get_cash(GRADED_SLEEVE) == pytest.approx(10_000.0)

    def test_rerunning_a_drill_does_not_top_its_cash_back_up(self, state, session):
        ensure_tagged_portfolio(state, session, DRILL_PORTFOLIO, 500.0)
        session.flush()
        state._update_cash(DRILL_PORTFOLIO, -200.0)
        session.flush()
        assert ensure_tagged_portfolio(state, session, DRILL_PORTFOLIO, 500.0) is False
        assert state.get_cash(DRILL_PORTFOLIO) == pytest.approx(300.0)

    def test_contexts_hydrate_the_tag_alone(self, state, session):
        ensure_tagged_portfolio(state, session, DRILL_PORTFOLIO, 500.0)
        session.flush()
        contexts = build_portfolio_contexts(
            state,
            session,
            sleeve_budgets={DRILL_PORTFOLIO: 500.0},
            portfolio_names=[DRILL_PORTFOLIO],
        )
        assert set(contexts) == {DRILL_PORTFOLIO}
        assert contexts[DRILL_PORTFOLIO].sleeve_budget == pytest.approx(500.0)

    def test_default_contexts_still_cover_the_six_graded_sleeves(self, session):
        state = PaperTradingState.create_new(
            {name: 1_000.0 for name in CAPITAL_ALLOCATIONS}, session
        )
        session.flush()
        contexts = build_portfolio_contexts(state, session, capital=100_000.0)
        assert set(contexts) == set(CAPITAL_ALLOCATIONS)

    def test_drill_sleeve_is_named_for_the_tag_not_the_base_sleeve(self):
        """Otherwise its positions and cash would be a graded sleeve's."""
        drill = build_drill_portfolio(
            DRILL_PORTFOLIO, 500.0, {"AAPL": make_bars()}, None
        )
        assert drill.name == DRILL_PORTFOLIO
        assert drill.capital == pytest.approx(500.0)

    def test_base_sleeve_is_a_real_sleeve_with_a_universe(self):
        assert DRILL_BASE_SLEEVE in CAPITAL_ALLOCATIONS
        assert UNIVERSE_REGISTRY[DRILL_BASE_SLEEVE]


# ---------------------------------------------------------------------------
# A tagged run leaves the graded book alone
# ---------------------------------------------------------------------------


class TestTaggedRunIsolation:
    def _drill_state(self, session):
        state = PaperTradingState.create_new({GRADED_SLEEVE: 10_000.0}, session)
        ensure_tagged_portfolio(state, session, DRILL_PORTFOLIO, 500.0)
        session.flush()
        return state

    def test_signals_are_booked_to_the_tag_only(self, session):
        state = self._drill_state(session)

        def buy_fn(ticker, bars):
            return {"action": "buy", "limit_price": 100.0, "quantity": 2.0}

        signals = run_daily(
            state,
            tagged_portfolio(DRILL_PORTFOLIO, buy_fn),
            {"AAPL": make_bars()},
            record_aggregate=False,
        )

        assert signals
        assert {s["portfolio"] for s in signals} == {DRILL_PORTFOLIO}

    def test_graded_sleeve_books_are_unchanged(self, session):
        state = self._drill_state(session)

        def buy_fn(ticker, bars):
            return {"action": "buy", "limit_price": 100.0, "quantity": 2.0}

        run_daily(
            state,
            tagged_portfolio(DRILL_PORTFOLIO, buy_fn),
            {"AAPL": make_bars()},
            record_aggregate=False,
        )

        assert state.get_cash(GRADED_SLEEVE) == pytest.approx(10_000.0)
        assert state.get_positions(GRADED_SLEEVE) == {}
        assert state.get_trades(GRADED_SLEEVE) == []
        snapshots = {
            row.portfolio
            for row in session.query(EquitySnapshot).all()
        }
        assert snapshots == {DRILL_PORTFOLIO}

    def test_a_tagged_run_writes_no_aggregate_rollup_row(self, session):
        """The drill universe cannot price the graded book, so no rollup is honest.

        compute_equity falls back to avg_entry_price for tickers absent from the
        run's bars, so writing the row would record an equity figure that was
        never true.
        """
        state = self._drill_state(session)
        run_daily(
            state,
            tagged_portfolio(DRILL_PORTFOLIO, lambda ticker, bars: None),
            {"AAPL": make_bars()},
            record_aggregate=False,
        )
        assert session.query(EquitySnapshot).filter_by(portfolio="_aggregate").all() == []

    def test_an_untagged_run_still_writes_the_aggregate_rollup(self, session):
        state = PaperTradingState.create_new({GRADED_SLEEVE: 10_000.0}, session)
        session.flush()
        run_daily(
            state,
            {
                GRADED_SLEEVE: PortfolioConfig(
                    name=GRADED_SLEEVE,
                    capital=10_000.0,
                    signals_fn=lambda ticker, bars: None,
                    risk_engine=RiskEngine(
                        position_entry_limit_pct=10.0,
                        sector_concentration_pct=100.0,
                        total_exposure_limit_pct=100.0,
                        max_lots_per_ticker=1,
                    ),
                )
            },
            {"AAPL": make_bars()},
        )
        rollup = session.query(EquitySnapshot).filter_by(portfolio="_aggregate").one()
        assert rollup.equity == pytest.approx(10_000.0)

    def test_a_funded_drill_does_not_inflate_the_aggregate_rollup(self, session):
        """The rollup is the graded book's NAV; drill funding is not capital at risk."""
        state = self._drill_state(session)
        run_daily(
            state,
            {
                GRADED_SLEEVE: PortfolioConfig(
                    name=GRADED_SLEEVE,
                    capital=10_000.0,
                    signals_fn=lambda ticker, bars: None,
                    risk_engine=RiskEngine(
                        position_entry_limit_pct=10.0,
                        sector_concentration_pct=100.0,
                        total_exposure_limit_pct=100.0,
                        max_lots_per_ticker=1,
                    ),
                )
            },
            {"AAPL": make_bars()},
        )
        rollup = session.query(EquitySnapshot).filter_by(portfolio="_aggregate").one()
        assert rollup.equity == pytest.approx(10_000.0)  # not 10_500
        assert rollup.cash == pytest.approx(10_000.0)


# ---------------------------------------------------------------------------
# The launchd report query (AC5)
# ---------------------------------------------------------------------------

REPORT_SCRIPT = "deploy/launchd/run_pipeline_report.sh"


def test_report_equity_query_filters_synthetic_portfolios():
    """AC5: the daily equity table must not show a drill row."""
    from pathlib import Path

    body = Path(REPORT_SCRIPT).read_text()
    assert "FROM equity_snapshots WHERE portfolio NOT LIKE '\\_%'" in body


def test_report_equity_predicate_excludes_a_drill_snapshot(session):
    """The predicate's *semantics*, asserted against real rows.

    The shell script's SQL runs in Postgres, so this exercises the equivalent
    SQLAlchemy predicate — the same escaped-underscore prefix test — over a
    fixture that contains a __drill__ snapshot alongside a graded one.
    """
    from sqlalchemy import func, select

    from shared.universe import EXCLUDED_PORTFOLIO_PREFIX

    now = datetime.now(timezone.utc)
    d = date(2026, 7, 7)
    for portfolio, equity in (
        (GRADED_SLEEVE, 10_000.0),
        (DRILL_PORTFOLIO, 500.0),
        ("_aggregate", 10_000.0),
    ):
        session.add(EquitySnapshot(
            portfolio=portfolio, date=d, equity=equity,
            cash=equity, market_value=0.0, created_at=now,
        ))
    session.flush()

    total = session.execute(
        select(func.sum(EquitySnapshot.equity)).where(
            ~EquitySnapshot.portfolio.startswith(
                EXCLUDED_PORTFOLIO_PREFIX, autoescape=True
            )
        )
    ).scalar_one()
    assert total == pytest.approx(10_000.0)
