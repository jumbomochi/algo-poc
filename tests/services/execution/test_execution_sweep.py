"""Tests for the pure sweep decision (KAN-87 Task 3).

No IB, no Redis: every behaviour is driven through ``OrderLedger`` against
an in-memory sqlite session, following the pattern in
``test_broker_stop_verifier.py`` (a local ``session`` fixture, ``OrderLedger``
constructed inline in each test — there is no shared ``ledger`` fixture for
``tests/services/``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.execution.execution_sweep import (
    RECOVERY_SOURCE_SWEEP,
    SweptExecution,
    executions_from_ib_fills,
    plan_sweep,
)
from shared.models import Base, ExecutionFill, OrderIntent, OrderStatus
from shared.order_ledger import ABSENT_AT_IB_REASON, OrderLedger


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _proposal(
    recommendation_id: str,
    *,
    account_id: str = "DUN551088",
    portfolio: str = "momentum",
    con_id: int = 756733,
    symbol: str = "UNH",
    quantity: float = 6.0,
    action: str = "SELL",
) -> SimpleNamespace:
    return SimpleNamespace(
        recommendation_id=recommendation_id,
        account_id=account_id,
        mode="paper",
        portfolio=portfolio,
        con_id=con_id,
        symbol=symbol,
        exchange="SMART",
        currency="USD",
        action=action,
        quantity=quantity,
        limit_price=340.0,
        order_type="LMT",
    )


def _submitted_intent(
    session: Session,
    ledger: OrderLedger,
    recommendation_id: str,
    *,
    ib_order_id: str,
    account_id: str = "DUN551088",
    portfolio: str = "momentum",
    quantity: float = 6.0,
) -> OrderIntent:
    """Drive a fresh intent to SUBMITTED via the real ledger transitions."""
    ledger.create_intent(
        _proposal(
            recommendation_id,
            account_id=account_id,
            portfolio=portfolio,
            quantity=quantity,
        )
    )
    ledger.transition(recommendation_id, OrderStatus.APPROVED)
    intent = ledger.record_submission(recommendation_id, ib_order_id)
    session.flush()
    return intent


def _record_execution_fill(
    session: Session,
    *,
    account_id: str,
    execution_id: str,
    ib_order_id: str = "148",
) -> ExecutionFill:
    fill = ExecutionFill(
        account_id=account_id,
        execution_id=execution_id,
        ib_order_id=ib_order_id,
        recommendation_id="rec-unh",
        portfolio="momentum",
        con_id=756733,
        symbol="UNH",
        exchange="SMART",
        currency="USD",
        side="SELL",
        quantity=6.0,
        price=341.22,
        commission=1.0,
        executed_at=datetime(2026, 9, 14, 13, 31, tzinfo=timezone.utc),
        projection_applied=True,
    )
    session.add(fill)
    session.flush()
    return fill


def _execution(**overrides) -> SweptExecution:
    base = dict(
        execution_id="exec-1",
        account_id="DUN551088",
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
        executed_at=datetime(2026, 9, 14, 13, 31, tzinfo=UTC),
    )
    base.update(overrides)
    return SweptExecution(**base)


def test_an_unrecorded_execution_is_published(session) -> None:
    """AC1: the fill the callback missed reaches stream:fills."""
    ledger = OrderLedger(session)
    intent = _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")

    outcome = plan_sweep([_execution()], ledger)

    assert len(outcome.recovered) == 1
    fill = outcome.recovered[0]
    assert fill.execution_id == "exec-1"
    assert fill.recommendation_id == intent.recommendation_id
    assert fill.portfolio == intent.portfolio
    assert fill.recovery_source == RECOVERY_SOURCE_SWEEP
    assert outcome.already_recorded == 0


def test_an_already_recorded_execution_is_not_republished(session) -> None:
    """AC2: no double-count."""
    ledger = OrderLedger(session)
    _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")
    _record_execution_fill(session, account_id="DUN551088", execution_id="exec-1")

    outcome = plan_sweep([_execution()], ledger)

    assert outcome.recovered == ()
    assert outcome.already_recorded == 1


def test_a_double_sweep_does_not_double_count(session) -> None:
    """AC2: running the same plan twice over a recorded fill stays empty."""
    ledger = OrderLedger(session)
    _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")
    _record_execution_fill(session, account_id="DUN551088", execution_id="exec-1")

    first = plan_sweep([_execution()], ledger)
    second = plan_sweep([_execution()], ledger)

    assert first.recovered == () and second.recovered == ()
    assert second.already_recorded == 1


def test_an_untracked_order_is_reported_never_projected(session) -> None:
    """AC3: someone else's order is named, not booked."""
    ledger = OrderLedger(session)

    outcome = plan_sweep([_execution(ib_order_id="999")], ledger)

    assert outcome.recovered == ()
    assert outcome.untracked == ("999",)


def test_an_absence_expired_intent_is_corrected(session) -> None:
    """AC4: EXPIRED-on-absence becomes fillable again, and is reported."""
    ledger = OrderLedger(session)
    intent = _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")
    ledger.transition(
        intent.recommendation_id,
        OrderStatus.EXPIRED,
        reason=ABSENT_AT_IB_REASON,
    )

    outcome = plan_sweep([_execution()], ledger)

    assert outcome.corrected == (intent.recommendation_id,)
    assert len(outcome.recovered) == 1
    assert (
        session.get(type(intent), intent.id).status
        == OrderStatus.SUBMITTED.value
    )


def test_a_genuinely_expired_intent_is_left_alone(session) -> None:
    """The correction is evidence-gated, not a blanket un-expire."""
    ledger = OrderLedger(session)
    intent = _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")
    ledger.transition(
        intent.recommendation_id,
        OrderStatus.EXPIRED,
        reason="IB reported the order Expired",
    )

    outcome = plan_sweep([_execution()], ledger)

    assert outcome.corrected == ()
    assert outcome.recovered == ()
    assert outcome.untracked == ("148",)


def test_a_corrected_intent_is_still_publishable_on_a_later_sweep(session) -> None:
    """A publish that failed after the correction committed must retry.

    ``plan_sweep`` never writes ``execution_fills`` itself — the projector
    does, on consuming ``stream:fills``. So if Task 4's publish step fails
    after this call returns, the AC4 correction (EXPIRED -> SUBMITTED) is
    still committed but no fill is recorded. The next run's sweep must not
    treat that as "nothing left to do": ``execution_fill_exists`` is still
    False, so it re-decides the same execution, finds the intent already
    SUBMITTED (nothing left to correct -- ``restore_absent_terminalization``
    raises ``InvalidOrderTransition``), and republishes on the normal
    ``_FILLABLE_STATUSES`` branch.
    """
    ledger = OrderLedger(session)
    intent = _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")
    ledger.transition(
        intent.recommendation_id,
        OrderStatus.EXPIRED,
        reason=ABSENT_AT_IB_REASON,
    )

    first = plan_sweep([_execution()], ledger)  # corrects, publish "fails"

    assert first.corrected == (intent.recommendation_id,)
    assert len(first.recovered) == 1

    second = plan_sweep([_execution()], ledger)  # the next run

    assert second.corrected == ()  # nothing left to correct
    assert len(second.recovered) == 1  # still publishable


# ---------------------------------------------------------------------------
# C1/C2 — the message must be one the projector can actually accept
#
# ``FillProjector._validate`` rejects a fill whose ``commission_trading`` is
# None, and writes the immutable ``execution_fills`` audit row *before* it
# validates. So an unprojectable message is not merely "not applied": it
# makes ``execution_fill_exists`` True forever, which the next sweep reads as
# ``already_recorded``. The first sweep after a missed fill would consume the
# only chance to recover it. Hence: never emit a message the projector is
# guaranteed to reject.
# ---------------------------------------------------------------------------


def test_a_usd_commission_is_translated_one_to_one(session) -> None:
    """C1: the live callback always sets commission_trading; so must this."""
    ledger = OrderLedger(session)
    _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")

    outcome = plan_sweep([_execution(commission=1.25)], ledger)

    [fill] = outcome.recovered
    assert fill.commission_trading == pytest.approx(1.25)
    assert fill.commission_fx_base_per_trading is None


def test_a_zero_usd_commission_is_still_translated(session) -> None:
    """The projector's None check fires even at 0.0 — a falsy commission is
    not an absent one."""
    ledger = OrderLedger(session)
    _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")

    outcome = plan_sweep([_execution(commission=0.0)], ledger)

    [fill] = outcome.recovered
    assert fill.commission_trading == 0.0


def test_an_sgd_commission_is_converted_with_the_carried_fx_rate(session) -> None:
    """Mirrors ``_on_commission_report``: amount / (base per USD)."""
    ledger = OrderLedger(session)
    _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")

    outcome = plan_sweep(
        [
            _execution(
                commission=2.60,
                commission_currency="SGD",
                commission_fx_base_per_trading=1.30,
            )
        ],
        ledger,
    )

    [fill] = outcome.recovered
    assert fill.commission_trading == pytest.approx(2.0)
    assert fill.commission_fx_base_per_trading == pytest.approx(1.30)


def test_an_untranslatable_commission_is_deferred_not_published(session) -> None:
    """C1/C2: an SGD commission with no FX rate cannot be projected. Emitting
    it would burn the execution permanently, so it waits for the next run."""
    ledger = OrderLedger(session)
    _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")

    outcome = plan_sweep(
        [
            _execution(
                commission=2.60,
                commission_currency="SGD",
                commission_fx_base_per_trading=None,
            )
        ],
        ledger,
    )

    assert outcome.recovered == ()
    assert outcome.deferred == ("exec-1",)


def test_an_unsupported_commission_currency_is_deferred(session) -> None:
    """``_validate`` allows only USD and SGD; anything else is a guaranteed
    rejection, so it must never be emitted."""
    ledger = OrderLedger(session)
    _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")

    outcome = plan_sweep([_execution(commission_currency="EUR")], ledger)

    assert outcome.recovered == ()
    assert outcome.deferred == ("exec-1",)


def test_a_missing_commission_currency_is_deferred(session) -> None:
    """C2: ``reqExecutionsAsync`` resolves on ``execDetailsEnd``, which TWS
    sends before the commission reports are guaranteed to have landed."""
    ledger = OrderLedger(session)
    _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")

    outcome = plan_sweep([_execution(commission_currency=None)], ledger)

    assert outcome.recovered == ()
    assert outcome.deferred == ("exec-1",)


def test_a_deferred_execution_does_not_disturb_the_intent(session) -> None:
    """A deferral must leave the ledger exactly as it found it — in
    particular it must not un-expire an intent it is not going to recover."""
    ledger = OrderLedger(session)
    intent = _submitted_intent(session, ledger, "rec-unh", ib_order_id="148")
    ledger.transition(
        intent.recommendation_id,
        OrderStatus.EXPIRED,
        reason=ABSENT_AT_IB_REASON,
    )

    outcome = plan_sweep(
        [_execution(commission_currency="SGD")], ledger
    )

    assert outcome.deferred == ("exec-1",)
    assert outcome.corrected == ()
    assert (
        session.get(type(intent), intent.id).status == OrderStatus.EXPIRED.value
    )


def test_an_untracked_execution_is_reported_untracked_not_deferred(
    session,
) -> None:
    """Classification order: an order the book never had is an AC3 untracked
    even when its commission is also unreadable."""
    ledger = OrderLedger(session)

    outcome = plan_sweep(
        [_execution(ib_order_id="999", commission_currency=None)], ledger
    )

    assert outcome.untracked == ("999",)
    assert outcome.deferred == ()


# ---------------------------------------------------------------------------
# I4 — whole-share rounding must terminalize the intent
# ---------------------------------------------------------------------------


def test_a_whole_share_rounded_fill_reports_the_order_done(session) -> None:
    """``fractional_orders: false`` truncates 10.4 to 10 at placement while
    ``requested_quantity`` keeps the fraction. Without this the intent sits
    PARTIALLY_FILLED forever: a permanent reservation leak on a BUY, and a
    permanently muted stop-loss on a SELL."""
    ledger = OrderLedger(session)
    _submitted_intent(
        session, ledger, "rec-unh", ib_order_id="148", quantity=10.4
    )

    outcome = plan_sweep(
        [_execution(quantity=10.0, cumulative_quantity=10.0)], ledger
    )

    [fill] = outcome.recovered
    assert fill.order_done is True


def test_a_materially_partial_fill_does_not_report_the_order_done(
    session,
) -> None:
    """The mirror of the projector's ``rounding_complete`` guard: only a
    sub-one-share shortfall is rounding. 4 of 10 is a real partial."""
    ledger = OrderLedger(session)
    _submitted_intent(
        session, ledger, "rec-unh", ib_order_id="148", quantity=10.0
    )

    outcome = plan_sweep(
        [_execution(quantity=4.0, cumulative_quantity=4.0)], ledger
    )

    [fill] = outcome.recovered
    assert fill.order_done is False


def test_a_fully_filled_order_still_reports_the_order_done(session) -> None:
    ledger = OrderLedger(session)
    _submitted_intent(
        session, ledger, "rec-unh", ib_order_id="148", quantity=6.0
    )

    outcome = plan_sweep([_execution(cumulative_quantity=6.0)], ledger)

    [fill] = outcome.recovered
    assert fill.order_done is True


# ---------------------------------------------------------------------------
# C2 — the adapter must not hand plan_sweep a currency TWS never sent
# ---------------------------------------------------------------------------


def test_a_blank_report_currency_falls_back_to_the_contract_currency() -> None:
    """``CommissionReport.currency`` defaults to ``''``. The contract's own
    currency is the sweep's best available answer and is right for every US
    equity this book trades."""
    fill = SimpleNamespace(
        execution=SimpleNamespace(
            execId="e1",
            acctNumber="DUN551088",
            orderId=148,
            side="SLD",
            shares=6.0,
            cumQty=6.0,
            price=341.22,
            time=datetime(2026, 9, 14, 13, 31, tzinfo=UTC),
        ),
        contract=SimpleNamespace(
            conId=756733, symbol="UNH", exchange="SMART", currency="USD"
        ),
        commissionReport=SimpleNamespace(commission=0.0, currency=""),
    )

    [swept] = executions_from_ib_fills([fill])

    assert swept.commission_currency == "USD"


def test_the_fx_rate_read_at_the_edge_is_carried_onto_the_execution() -> None:
    """The ``ExchangeRate`` account value can only be read where IB is — the
    pure module has to be handed it."""
    fill = SimpleNamespace(
        execution=SimpleNamespace(
            execId="e1",
            acctNumber="DUN551088",
            orderId=148,
            side="SLD",
            shares=6.0,
            cumQty=6.0,
            price=341.22,
            time=datetime(2026, 9, 14, 13, 31, tzinfo=UTC),
        ),
        contract=SimpleNamespace(
            conId=756733, symbol="UNH", exchange="SGX", currency="SGD"
        ),
        commissionReport=SimpleNamespace(commission=2.6, currency="SGD"),
    )

    [swept] = executions_from_ib_fills([fill], fx_base_per_trading=1.30)

    assert swept.commission_currency == "SGD"
    assert swept.commission_fx_base_per_trading == pytest.approx(1.30)


def test_a_usd_execution_carries_no_fx_rate() -> None:
    """A USD commission needs no translation; recording a rate against it
    would put a number in the audit row that was never used."""
    fill = SimpleNamespace(
        execution=SimpleNamespace(
            execId="e1",
            acctNumber="DUN551088",
            orderId=148,
            side="SLD",
            shares=6.0,
            cumQty=6.0,
            price=341.22,
            time=datetime(2026, 9, 14, 13, 31, tzinfo=UTC),
        ),
        contract=SimpleNamespace(
            conId=756733, symbol="UNH", exchange="SMART", currency="USD"
        ),
        commissionReport=SimpleNamespace(commission=1.0, currency="USD"),
    )

    [swept] = executions_from_ib_fills([fill], fx_base_per_trading=1.30)

    assert swept.commission_fx_base_per_trading is None
