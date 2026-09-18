"""The sweep's output, driven through the real projector (KAN-87, C1).

Every other test on this branch stops at one side of the seam: the pure
module proves it *decides* correctly, the projector tests prove they apply a
fill correctly. Neither notices that the message the sweep builds is one the
projector rejects — ``make_fill`` in ``test_projector.py`` back-fills
``commission_trading`` whenever the currency is USD, so those tests exercise
a message the sweep can never produce.

That gap was not merely "the feature does not work". ``FillProjector.apply``
writes the immutable ``execution_fills`` audit row *inside* ``session.begin``
and validates afterwards, deliberately, so a rejected fill still leaves the
row behind — and ``execution_fill_exists`` then reports it as
``already_recorded`` on every later sweep. The first sweep after a missed
fill would consume the only chance to recover it while printing a
healthy-looking "1 recovered".

So this file drives a real ``plan_sweep`` output through a real
``FillProjector`` against a real (sqlite) ledger, and asserts the fill is
actually projected.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from services.execution.execution_sweep import (
    RECOVERY_SOURCE_SWEEP,
    SweptExecution,
    plan_sweep,
)
from services.portfolio_accounting.projector import FillProjector
from shared.models import (
    Base,
    ExecutionFill,
    OrderIntent,
    OrderStatus,
    PortfolioConfig,
    Position,
)
from shared.order_ledger import OrderLedger
from shared.schemas.messages import FillMessage

NOW = datetime(2026, 9, 14, 13, 31, tzinfo=timezone.utc)
ACCOUNT = "DUN551088"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


@pytest.fixture
def projector(session: Session) -> FillProjector:
    return FillProjector(session)


def _intent(
    session: Session,
    *,
    recommendation_id: str,
    action: str,
    quantity: float,
    ib_order_id: str,
) -> OrderIntent:
    intent = OrderIntent(
        recommendation_id=recommendation_id,
        account_id=ACCOUNT,
        mode="paper",
        portfolio="momentum",
        con_id=756733,
        symbol="UNH",
        exchange="SMART",
        currency="USD",
        action=action,
        requested_quantity=quantity,
        limit_price=340.0,
        order_type="LMT",
        reserved_notional=quantity * 340.0 if action == "BUY" else 0,
        filled_quantity=0,
        status=OrderStatus.SUBMITTED.value,
        ib_order_id=ib_order_id,
        created_at=NOW,
        updated_at=NOW,
        submitted_at=NOW,
    )
    session.add(intent)
    session.commit()
    return intent


def _open_the_position(projector: FillProjector, session: Session) -> None:
    """A real BUY through the projector, so the SELL has something to close."""
    session.add(
        PortfolioConfig(
            portfolio="momentum",
            capital=10_000,
            cash=10_000,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    _intent(
        session,
        recommendation_id="rec-buy",
        action="BUY",
        quantity=6.0,
        ib_order_id="147",
    )
    projector.apply(
        FillMessage(
            ticker="UNH",
            timestamp=NOW,
            side="buy",
            quantity=6.0,
            fill_price=330.0,
            commission=1.0,
            commission_currency="USD",
            commission_trading=1.0,
            recommendation_id="rec-buy",
            order_id="147",
            execution_id="exec-buy",
            account_id=ACCOUNT,
            cumulative_quantity=6.0,
            portfolio="momentum",
            con_id=756733,
            exchange="SMART",
            currency="USD",
            order_done=True,
        )
    )


def _execution(**overrides) -> SweptExecution:
    base = dict(
        execution_id="exec-sell",
        account_id=ACCOUNT,
        ib_order_id="148",
        con_id=756733,
        ticker="UNH",
        exchange="SMART",
        currency="USD",
        side="sell",
        quantity=6.0,
        cumulative_quantity=6.0,
        price=341.22,
        commission=1.0,
        commission_currency="USD",
        executed_at=NOW,
    )
    base.update(overrides)
    return SweptExecution(**base)


def _recovered_fill(session: Session) -> ExecutionFill:
    return session.scalar(
        select(ExecutionFill).where(ExecutionFill.execution_id == "exec-sell")
    )


def test_a_swept_fill_is_actually_projected(projector, session) -> None:
    """End to end: plan_sweep -> FillProjector -> book corrected.

    The phantom this branch exists to clear is a position the book still
    holds and IB does not. Recovery means the position is gone, the intent
    is terminal, and the audit row says the sweep is why.
    """
    _open_the_position(projector, session)
    _intent(
        session,
        recommendation_id="rec-sell",
        action="SELL",
        quantity=6.0,
        ib_order_id="148",
    )

    outcome = plan_sweep([_execution()], OrderLedger(session))
    session.commit()

    assert len(outcome.recovered) == 1
    assert projector.apply(outcome.recovered[0]) is True

    fill = _recovered_fill(session)
    assert fill is not None
    assert fill.projection_applied is True
    assert fill.recovery_source == RECOVERY_SOURCE_SWEEP

    intent = session.scalar(
        select(OrderIntent).where(OrderIntent.recommendation_id == "rec-sell")
    )
    assert intent.status == OrderStatus.FILLED.value
    assert intent.terminal_at is not None

    assert session.scalar(
        select(Position).where(
            Position.ticker == "UNH", Position.status == "open"
        )
    ) is None


def test_a_whole_share_rounded_sweep_fill_terminalizes_the_intent(
    projector, session
) -> None:
    """I4: requested 6.4, IB placed and filled 6. Without ``order_done`` the
    intent stays PARTIALLY_FILLED forever — and ``has_active_sell`` stays
    True, which mutes this ticker's stop-loss permanently."""
    _open_the_position(projector, session)
    _intent(
        session,
        recommendation_id="rec-sell",
        action="SELL",
        quantity=6.4,
        ib_order_id="148",
    )

    outcome = plan_sweep([_execution()], OrderLedger(session))
    session.commit()

    assert projector.apply(outcome.recovered[0]) is True

    intent = session.scalar(
        select(OrderIntent).where(OrderIntent.recommendation_id == "rec-sell")
    )
    assert intent.status == OrderStatus.FILLED.value


def test_a_material_partial_sweep_fill_stays_open(projector, session) -> None:
    """The mirror image: 4 of 6 is a real partial and must stay
    PARTIALLY_FILLED so the status path can terminalize it properly."""
    _open_the_position(projector, session)
    _intent(
        session,
        recommendation_id="rec-sell",
        action="SELL",
        quantity=6.0,
        ib_order_id="148",
    )

    outcome = plan_sweep(
        [_execution(quantity=4.0, cumulative_quantity=4.0)],
        OrderLedger(session),
    )
    session.commit()

    assert projector.apply(outcome.recovered[0]) is True

    intent = session.scalar(
        select(OrderIntent).where(OrderIntent.recommendation_id == "rec-sell")
    )
    assert intent.status == OrderStatus.PARTIALLY_FILLED.value
    assert session.scalar(
        select(Position).where(
            Position.ticker == "UNH", Position.status == "open"
        )
    ).quantity == pytest.approx(2.0)


def test_an_sgd_commission_survives_the_projector(projector, session) -> None:
    """C1's other half: a non-USD commission needs the FX rate the edge read,
    and the projector re-derives the conversion to check it."""
    _open_the_position(projector, session)
    _intent(
        session,
        recommendation_id="rec-sell",
        action="SELL",
        quantity=6.0,
        ib_order_id="148",
    )

    outcome = plan_sweep(
        [
            _execution(
                commission=2.60,
                commission_currency="SGD",
                commission_fx_base_per_trading=1.30,
            )
        ],
        OrderLedger(session),
    )
    session.commit()

    assert projector.apply(outcome.recovered[0]) is True
    assert _recovered_fill(session).projection_applied is True
