from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scripts.run_paper as run_paper
from scripts.paper_state import PaperTradingState
from scripts.run_backtest import PortfolioConfig
from services.risk_management.engine import RiskEngine
from shared.models.base import Base


def run_daily(*args, **kwargs):
    """Supply valid funding inputs so these shadow-scoping tests exercise the
    observer path without tripping the settled-cash/reservation gates."""
    kwargs.setdefault("settled_cash_trading", 1_000_000)
    kwargs.setdefault("active_buy_reservations_usd", 0)
    kwargs.setdefault("commission_per_share_usd", 0.005)
    kwargs.setdefault("minimum_commission_usd", 1)
    kwargs.setdefault("minimum_settled_usd_reserve", 0)
    return run_paper.run_daily(*args, **kwargs)


class Observer:
    def __init__(self, *, raises: bool = False, mutates: bool = False) -> None:
        self.calls: list[dict] = []
        self.raises = raises
        self.mutates = mutates

    def observe(self, **kwargs) -> None:
        if self.mutates:
            kwargs["signal"]["quantity"] = 999.0
            kwargs["signal"]["metadata"]["source"] = "research"
        if self.raises:
            raise RuntimeError("research db unavailable")
        self.calls.append(kwargs)


def make_state_and_portfolio(*, entry_limit_pct: float = 100.0):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    state = PaperTradingState.create_new({"momentum": 10_000.0}, session)

    def signal_fn(ticker, bars):
        return {
            "action": "buy",
            "limit_price": 100.0,
            "quantity": 1.0,
            "metadata": {"source": "established"},
        }

    portfolios = {
        "momentum": PortfolioConfig(
            "momentum",
            10_000.0,
            signal_fn,
            RiskEngine(
                position_entry_limit_pct=entry_limit_pct,
                sector_concentration_pct=100.0,
                total_exposure_limit_pct=100.0,
            ),
        )
    }
    bars = {
        "AAPL": [
            {
                "date": datetime(2026, 7, 13, 21, 0),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1000,
            }
        ]
    }
    return session, state, portfolios, bars


def established_paper_state(state: PaperTradingState) -> dict:
    """Return every persisted established-paper output exposed by the state API."""
    portfolio = "momentum"
    return {
        "capital": state.get_capital(portfolio),
        "cash": state.get_cash(portfolio),
        "positions": state.get_positions(portfolio),
        "trades": state.get_trades(portfolio),
        "equity_history": state.get_equity_history(portfolio),
    }


def test_paper_run_observes_raw_buy_candidate_with_final_bar_date():
    session, state, portfolios, bars = make_state_and_portfolio()
    observer = Observer()

    signals = run_daily(
        state, portfolios, bars, candidate_observer=observer
    )

    assert len(signals) == 1
    assert len(observer.calls) == 1
    assert observer.calls[0]["portfolio"] == "momentum"
    assert observer.calls[0]["ticker"] == "AAPL"
    assert observer.calls[0]["as_of"] == date(2026, 7, 13)
    assert observer.calls[0]["risk_approved"] is True
    session.close()


def test_paper_run_observes_candidate_rejected_by_risk():
    session, state, portfolios, bars = make_state_and_portfolio(
        entry_limit_pct=0.000001
    )
    observer = Observer()

    signals = run_daily(
        state, portfolios, bars, candidate_observer=observer
    )

    assert signals == []
    assert len(observer.calls) == 1
    assert observer.calls[0]["risk_approved"] is False
    assert observer.calls[0]["risk_reason"]
    assert state.get_positions("momentum") == {}
    session.close()


def test_observer_failure_leaves_established_fill_and_signal_unchanged():
    baseline_session, baseline_state, portfolios, bars = make_state_and_portfolio()
    expected = run_daily(baseline_state, portfolios, bars)
    expected_state = deepcopy(established_paper_state(baseline_state))

    session, state, portfolios, bars = make_state_and_portfolio()
    actual = run_daily(
        state, portfolios, bars, candidate_observer=Observer(raises=True)
    )

    assert actual == expected
    assert established_paper_state(state) == expected_state
    baseline_session.close()
    session.close()


def test_mutating_observer_cannot_change_established_fill_or_signal():
    session, state, portfolios, bars = make_state_and_portfolio()

    signals = run_daily(
        state, portfolios, bars, candidate_observer=Observer(mutates=True)
    )

    assert signals[0]["quantity"] == 1.0
    assert signals[0]["metadata"] == {"source": "established"}
    # run_daily emits signals only; real IB fills are the sole input that
    # mutates the durable book, so a mutating observer cannot leak a position.
    assert state.get_positions("momentum") == {}
    session.close()


def test_disabled_research_setup_is_a_strict_no_op(monkeypatch):
    monkeypatch.setattr(
        run_paper,
        "make_db_session",
        lambda _: (_ for _ in ()).throw(AssertionError("must not create session")),
    )

    observer, session = run_paper._create_research_shadow(
        enabled=False,
        bars_by_ticker={},
        factor_ids=["missing"],
        db_url="unused",
    )

    assert observer is None
    assert session is None


def test_research_setup_failure_is_isolated_before_session_creation(monkeypatch):
    created = False

    def fail_if_created(_):
        nonlocal created
        created = True
        raise AssertionError("session creation should not be reached")

    monkeypatch.setattr(run_paper, "make_db_session", fail_if_created)

    observer, session = run_paper._create_research_shadow(
        enabled=True,
        bars_by_ticker={},
        factor_ids=["missing"],
        db_url="unused",
    )

    assert observer is None
    assert session is None
    assert created is False


def test_research_recorder_setup_failure_closes_independent_session(monkeypatch):
    class IndependentSession:
        closed = False

        def close(self):
            self.closed = True

    independent_session = IndependentSession()
    monkeypatch.setattr(run_paper, "make_db_session", lambda _: independent_session)

    import research.shadow

    monkeypatch.setattr(
        research.shadow,
        "SQLShadowRecorder",
        lambda *_: (_ for _ in ()).throw(RuntimeError("recorder unavailable")),
    )

    observer, session = run_paper._create_research_shadow(
        enabled=True,
        bars_by_ticker={
            "AAPL": [
                {
                    "date": "2026-07-13",
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100,
                    "volume": 1000,
                }
            ]
        },
        factor_ids=[],
        db_url="sqlite://",
    )

    assert observer is None
    assert session is None
    assert independent_session.closed is True
