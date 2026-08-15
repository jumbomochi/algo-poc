"""KAN-44: the currency-qualified equity columns are written, not left NULL.

D16 pins the ladder's drawdown limit at "max drawdown <=12% at current size,
measured on USD NAV". The paper account has been SGD-base since 2026-07-25, so
until these columns carry a rate, "measured on USD NAV" is not a computable
statement — a ratio taken inside one series is FX-neutral only if no FX move
occurred between its peak and its trough.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from scripts.paper_state import PaperTradingState
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot

CURRENCY_COLUMNS = (
    "base_currency",
    "trading_currency",
    "equity_trading",
    "cash_trading",
    "market_value_trading",
    "fx_base_per_trading",
    "equity_base",
    "valuation_at",
)


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database with all tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


def _state(db_session: Session) -> PaperTradingState:
    return PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000}, session=db_session
    )


def _row(db_session: Session, portfolio: str = "mr") -> EquitySnapshot:
    return db_session.execute(
        select(EquitySnapshot).where(EquitySnapshot.portfolio == portfolio)
    ).scalar_one()


VALUATION_AT = datetime(2024, 1, 15, 21, 5, tzinfo=timezone.utc)


# --- AC1: written with a rate populates all eight; without leaves them NULL ---


def test_snapshot_with_fx_rate_populates_all_eight_columns(db_session: Session):
    state = _state(db_session)
    state.record_equity_snapshot(
        "mr",
        date(2024, 1, 15),
        10_500.0,
        8_500.0,
        2_000.0,
        base_currency="SGD",
        trading_currency="USD",
        fx_base_per_trading=1.35,
        valuation_at=VALUATION_AT,
    )

    row = _row(db_session)
    assert [getattr(row, name) is None for name in CURRENCY_COLUMNS] == [
        False
    ] * 8
    assert row.base_currency == "SGD"
    assert row.trading_currency == "USD"
    assert row.equity_trading == 10_500.0
    assert row.cash_trading == 8_500.0
    assert row.market_value_trading == 2_000.0
    assert row.fx_base_per_trading == 1.35
    assert row.valuation_at is not None
    # The legacy columns are untouched: this story records the currency
    # context of a number that is already computed.
    assert (row.equity, row.cash, row.market_value) == (
        10_500.0,
        8_500.0,
        2_000.0,
    )


def test_snapshot_without_fx_rate_leaves_all_eight_null(db_session: Session):
    """The pre-KAN-44 call signature must behave exactly as it does today."""
    state = _state(db_session)
    state.record_equity_snapshot("mr", date(2024, 1, 15), 10_500.0, 8_500.0, 2_000.0)

    row = _row(db_session)
    assert [getattr(row, name) for name in CURRENCY_COLUMNS] == [None] * 8
    assert (row.equity, row.cash, row.market_value) == (
        10_500.0,
        8_500.0,
        2_000.0,
    )


# --- AC2: derived-value arithmetic ---


def test_equity_base_equals_equity_trading_times_rate(db_session: Session):
    state = _state(db_session)
    state.record_equity_snapshot(
        "mr",
        date(2024, 1, 15),
        10_500.0,
        8_500.0,
        2_000.0,
        base_currency="SGD",
        trading_currency="USD",
        fx_base_per_trading=1.3456,
        valuation_at=VALUATION_AT,
    )

    row = _row(db_session)
    assert row.equity_base == pytest.approx(
        row.equity_trading * row.fx_base_per_trading, abs=0.01
    )
    assert row.equity_base == pytest.approx(14_128.8, abs=0.01)


def test_upsert_rewrites_the_currency_columns(db_session: Session):
    """A re-run must never leave a stale rate beside a fresh equity figure."""
    state = _state(db_session)
    state.record_equity_snapshot(
        "mr",
        date(2024, 1, 15),
        10_500.0,
        8_500.0,
        2_000.0,
        base_currency="SGD",
        trading_currency="USD",
        fx_base_per_trading=1.35,
        valuation_at=VALUATION_AT,
    )
    state.record_equity_snapshot(
        "mr",
        date(2024, 1, 15),
        10_600.0,
        8_600.0,
        2_000.0,
        base_currency="SGD",
        trading_currency="USD",
        fx_base_per_trading=1.30,
        valuation_at=VALUATION_AT,
    )

    row = _row(db_session)
    assert row.equity_trading == 10_600.0
    assert row.fx_base_per_trading == 1.30
    assert row.equity_base == pytest.approx(13_780.0, abs=0.01)


def test_upsert_without_a_rate_clears_stale_currency_columns(db_session: Session):
    """Rewriting equity with no rate must not leave the old rate behind.

    A row claiming 10_600 USD alongside yesterday's rate would be a lie of a
    kind this project's evidence discipline exists to prevent: internally
    inconsistent, and indistinguishable from a correctly-stamped row.
    """
    state = _state(db_session)
    state.record_equity_snapshot(
        "mr",
        date(2024, 1, 15),
        10_500.0,
        8_500.0,
        2_000.0,
        base_currency="SGD",
        trading_currency="USD",
        fx_base_per_trading=1.35,
        valuation_at=VALUATION_AT,
    )
    state.record_equity_snapshot("mr", date(2024, 1, 15), 10_600.0, 8_600.0, 2_000.0)

    row = _row(db_session)
    assert row.equity == 10_600.0
    assert [getattr(row, name) for name in CURRENCY_COLUMNS] == [None] * 8


@pytest.mark.parametrize("rate", [0.0, -1.35, float("nan"), float("inf")])
def test_non_positive_or_non_finite_rate_is_refused(
    db_session: Session, rate: float
):
    """Silently writing NULL for a bad rate would be an unrecorded gap."""
    state = _state(db_session)
    with pytest.raises(ValueError, match="fx_base_per_trading"):
        state.record_equity_snapshot(
            "mr",
            date(2024, 1, 15),
            10_500.0,
            8_500.0,
            2_000.0,
            base_currency="SGD",
            trading_currency="USD",
            fx_base_per_trading=rate,
            valuation_at=VALUATION_AT,
        )


def test_rate_without_currency_labels_is_refused(db_session: Session):
    """A rate is meaningless without the pair it converts between."""
    state = _state(db_session)
    with pytest.raises(ValueError, match="currency"):
        state.record_equity_snapshot(
            "mr",
            date(2024, 1, 15),
            10_500.0,
            8_500.0,
            2_000.0,
            fx_base_per_trading=1.35,
            valuation_at=VALUATION_AT,
        )


# --- AC5: the distinction is real, not cosmetic ---


def _max_drawdown_pct(series: list[float]) -> float:
    """Peak-to-trough drawdown, as a percentage.

    Deliberately local to this test: the production computation belongs to
    ``shared/evidence_store`` (KAN-26) and must not be duplicated in shipped
    code. Here it exists only to compare the two series against each other.
    """
    peak = float("-inf")
    worst = 0.0
    for value in series:
        peak = max(peak, value)
        worst = max(worst, (peak - value) / peak)
    return worst * 100.0


def test_fx_move_between_peak_and_trough_moves_the_drawdown(db_session: Session):
    """The whole justification for this story, as an assertion.

    A 10% FX move between the peak and the trough makes the base-currency
    drawdown materially different from the trading-currency one. If these two
    numbers agreed, the currency columns would be decoration.
    """
    state = _state(db_session)
    # USD NAV falls 10_000 -> 9_500 (a 5% drawdown), while SGD/USD moves
    # 1.35 -> 1.215 (a 10% depreciation of USD against SGD).
    for day, equity, rate in (
        (date(2024, 1, 15), 10_000.0, 1.35),
        (date(2024, 1, 16), 9_500.0, 1.215),
    ):
        state.record_equity_snapshot(
            "mr",
            day,
            equity,
            equity,
            0.0,
            base_currency="SGD",
            trading_currency="USD",
            fx_base_per_trading=rate,
            valuation_at=VALUATION_AT,
        )

    rows = db_session.execute(
        select(EquitySnapshot)
        .where(EquitySnapshot.portfolio == "mr")
        .order_by(EquitySnapshot.date)
    ).scalars().all()

    trading_dd = _max_drawdown_pct([r.equity_trading for r in rows])
    base_dd = _max_drawdown_pct([r.equity_base for r in rows])

    assert trading_dd == pytest.approx(5.0, abs=0.01)
    assert base_dd == pytest.approx(14.5, abs=0.01)
    # Under a 12% bound the two series disagree about whether this epoch
    # breached — which is exactly why the currency has to be recorded.
    assert trading_dd < 12.0 < base_dd


# --- AC3 (partial): get_equity_history exposes the new columns ---


def test_equity_history_exposes_currency_context(db_session: Session):
    state = _state(db_session)
    state.record_equity_snapshot(
        "mr",
        date(2024, 1, 15),
        10_500.0,
        8_500.0,
        2_000.0,
        base_currency="SGD",
        trading_currency="USD",
        fx_base_per_trading=1.35,
        valuation_at=VALUATION_AT,
    )
    state.record_equity_snapshot("mr", date(2024, 1, 16), 10_600.0, 8_600.0, 2_000.0)

    history = state.get_equity_history("mr")
    assert [h["equity"] for h in history] == [10_500.0, 10_600.0]
    assert history[0]["equity_base"] == pytest.approx(14_175.0, abs=0.01)
    assert history[0]["fx_base_per_trading"] == 1.35
    assert history[0]["base_currency"] == "SGD"
    assert history[0]["trading_currency"] == "USD"
    # A legacy row reads back with the context absent rather than guessed.
    assert history[1]["equity_base"] is None
    assert history[1]["fx_base_per_trading"] is None
