"""KAN-20 — the 30-minute scan verifies the broker stop instead of emitting.

A stop placed once is not protection. Positions shrink on partial fills, the
trailing high rises, cancel-all reaches further than intended, and a Gateway
restart can drop an order. What makes a resting stop *protection* is something
that keeps checking it is still there, at the right size and the right price.

The three adjustments below are benign by construction — missing, over-covered,
under-levelled — and are corrected silently. Anything else is a stop somebody
or something changed outside this system, and correcting that silently would
hide whatever did it. Those are reported.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from services.execution.broker_stops import BrokerStopManager
from services.execution.runner import ExecutionServiceRunner
from shared.config import AppConfig, ExecutionConfig, IBConfig
from shared.models import Base, OrderStatus, Position
from shared.order_ledger import OrderLedger
from shared.schemas.messages import KillMessage

OPENED_AT = datetime(2026, 8, 16, 13, 30, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def make_position(
    *,
    ticker: str = "AAPL",
    con_id: int = 265598,
    quantity: float = 100,
    highest: float = 220.0,
    account_id: str = "DUN551088",
    portfolio: str = "momentum",
) -> Position:
    return Position(
        account_id=account_id,
        ticker=ticker,
        portfolio=portfolio,
        con_id=con_id,
        exchange="SMART",
        currency="USD",
        quantity=quantity,
        avg_entry_price=200.0,
        current_price=highest,
        peak_price=highest,
        highest_price_since_entry=highest,
        opened_at=OPENED_AT,
        status="open",
    )


def unrelated_order(order_ref: str = "rec-entry-1") -> SimpleNamespace:
    """Some other order of ours, resting. Its presence is what proves the
    order book actually synced — an empty book proves nothing."""
    return SimpleNamespace(
        order_id="1",
        order_ref=order_ref,
        action="BUY",
        ticker="MSFT",
        quantity=10.0,
        account_id="DUN551088",
        order_type="LMT",
        aux_price=None,
        filled_quantity=0.0,
        remaining_quantity=10.0,
    )


def resting(
    *,
    order_id: str = "4242",
    order_ref: str,
    quantity: float,
    aux_price: float,
    filled: float = 0.0,
) -> SimpleNamespace:
    """One stop as ``list_open_broker_orders`` reports it."""
    return SimpleNamespace(
        order_id=order_id,
        order_ref=order_ref,
        action="SELL",
        ticker="AAPL",
        quantity=quantity,
        account_id="DUN551088",
        order_type="STP",
        aux_price=aux_price,
        filled_quantity=filled,
        remaining_quantity=max(0.0, quantity - filled),
    )


def make_manager(
    session,
    *,
    live=(),
    enabled=True,
    on_drift=None,
    broker_held: float = 100.0,
    **kwargs,
):
    order_manager = AsyncMock()
    order_manager.submit_stop = AsyncMock(return_value="9001")
    order_manager.find_stop_order = AsyncMock(return_value=None)
    order_manager.list_open_broker_orders = AsyncMock(return_value=list(live))
    order_manager.cancel_broker_order = AsyncMock(return_value=True)
    # Confirmed-cancel primitive: empty means IB's book agrees it is gone.
    order_manager.cancel_working_orders = AsyncMock(return_value=[])
    # What IB says the account actually holds — the ceiling on any placement.
    order_manager.broker_position = AsyncMock(return_value=broker_held)
    defaults = dict(
        order_manager=order_manager,
        order_ledger=OrderLedger(session),
        mode="paper",
        account_id="DUN551088",
        trailing_pct=15.0,
        enabled=enabled,
        on_drift_detected=on_drift,
    )
    defaults.update(kwargs)
    return BrokerStopManager(**defaults), order_manager


async def seed_resting_stop(
    manager: BrokerStopManager,
    order_manager,
    *,
    quantity: float,
    reference_price: float,
    order_id: str = "4242",
    con_id: int = 265598,
) -> str:
    """Place one stop through the real path so the ledger row is genuine."""
    order_manager.submit_stop = AsyncMock(return_value=order_id)
    await manager.ensure_coverage(
        account_id="DUN551088",
        portfolio="momentum",
        con_id=con_id,
        symbol="AAPL",
        exchange="SMART",
        currency="USD",
        quantity=quantity,
        reference_price=reference_price,
    )
    return order_manager.submit_stop.await_args.kwargs["recommendation_id"]


class TestRecreatesAMissingStop:
    """AC1 / design test #29a."""

    async def test_a_stop_gone_from_ib_is_replaced_within_one_cycle(
        self, session
    ):
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        # The stop was cancelled out of band. The book still lists our other
        # working order, so it demonstrably synced and the absence is real.
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[unrelated_order()]
        )
        order_manager.submit_stop = AsyncMock(return_value="5150")

        report = await manager.verify_coverage()

        assert report.placed == ["5150"]
        kwargs = order_manager.submit_stop.await_args.kwargs
        assert kwargs["quantity"] == pytest.approx(100)
        assert kwargs["stop_price"] == pytest.approx(187.0)

    async def test_the_vanished_intent_is_terminalised_first(self, session):
        """Otherwise it counts as coverage forever and nothing re-places it.

        ``open_stop_quantity`` sums non-terminal stop intents. A stop that no
        longer rests at IB but whose row is still SUBMITTED makes the position
        read as fully protected — the exact state this scan exists to break.
        """
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[unrelated_order()]
        )

        report = await manager.verify_coverage()

        assert report.released_intents == [stop_id]
        ledger = OrderLedger(session)
        assert ledger.get(stop_id).status == OrderStatus.CANCELLED.value

    async def test_a_stop_still_resting_is_left_alone(self, session):
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[resting(order_ref=stop_id, quantity=100, aux_price=187.0)]
        )
        order_manager.submit_stop = AsyncMock(return_value="never")

        report = await manager.verify_coverage()

        assert report.placed == []
        assert report.cancelled_order_ids == []
        order_manager.submit_stop.assert_not_awaited()


class TestResizesAfterAPartialFill:
    """AC2 / design test #29b.

    A 100-share stop against 60 held shares does not merely over-protect: the
    extra 40 open a short the moment it triggers.
    """

    async def test_the_stop_is_re_placed_at_the_remaining_quantity(
        self, session
    ):
        manager, order_manager = make_manager(session, broker_held=60)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        # 40 shares left the position outside this service.
        position = session.scalars(select(Position)).one()
        position.quantity = 60
        session.commit()
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[resting(order_ref=stop_id, quantity=100, aux_price=187.0)]
        )
        order_manager.submit_stop = AsyncMock(return_value="5150")

        report = await manager.verify_coverage()

        assert report.cancelled_order_ids == ["4242"]
        # Confirmed against IB's book, not merely requested — a cancel that
        # does not take would leave the replacement double-covering.
        order_manager.cancel_working_orders.assert_awaited_once_with(
            [("4242", stop_id)]
        )
        assert report.placed == ["5150"]
        assert order_manager.submit_stop.await_args.kwargs[
            "quantity"
        ] == pytest.approx(60)

    async def test_the_over_covering_intent_is_terminalised(self, session):
        """A cancelled stop that still reads as coverage blocks the re-place."""
        manager, order_manager = make_manager(session, broker_held=60)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        position = session.scalars(select(Position)).one()
        position.quantity = 60
        session.commit()
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[resting(order_ref=stop_id, quantity=100, aux_price=187.0)]
        )

        await manager.verify_coverage()

        ledger = OrderLedger(session)
        assert ledger.get(stop_id).status == OrderStatus.CANCELLED.value
        assert ledger.open_stop_quantity("DUN551088", "momentum", 265598) == 60


class TestTrailingLevelAdvances:
    """AC5 — and the half of AC5 that matters: it never moves down."""

    async def test_a_new_high_re_levels_the_stop_upward(self, session):
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        position = session.scalars(select(Position)).one()
        position.highest_price_since_entry = 260.0
        session.commit()
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[resting(order_ref=stop_id, quantity=100, aux_price=187.0)]
        )
        order_manager.submit_stop = AsyncMock(return_value="5150")

        report = await manager.verify_coverage()

        assert report.cancelled_order_ids == ["4242"]
        assert report.placed == ["5150"]
        assert order_manager.submit_stop.await_args.kwargs[
            "stop_price"
        ] == pytest.approx(221.0)

    async def test_a_lower_computed_level_never_loosens_a_resting_stop(
        self, session
    ):
        """The stored high can only rise, but a bad mark must not lower a stop.

        If the recorded high were ever corrected downward — a bad print backed
        out, a manual edit — re-levelling on it would widen live protection.
        The scan is allowed to tighten a stop and never to loosen one.
        """
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=260.0
        )
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[resting(order_ref=stop_id, quantity=100, aux_price=221.0)]
        )
        order_manager.submit_stop = AsyncMock(return_value="never")

        report = await manager.verify_coverage()

        assert report.cancelled_order_ids == []
        assert report.placed == []
        order_manager.cancel_working_orders.assert_not_awaited()


class TestDriftIsReportedNotCorrected:
    """AC3 / design test #29c.

    Drift is the broker disagreeing with the ledger row that describes it —
    someone modified the order outside this system. The three benign cases are
    the *expectation* moving away from a ledger the broker still matches, and
    those are corrected above without a word.
    """

    async def test_a_modified_stop_price_raises_an_alert(self, session):
        drifts = []
        manager, order_manager = make_manager(
            session, on_drift=AsyncMock(side_effect=lambda d: drifts.append(d))
        )
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        # Ledger says 187.00; IB is resting one at 150.00.
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[resting(order_ref=stop_id, quantity=100, aux_price=150.0)]
        )

        report = await manager.verify_coverage()

        assert len(report.drifts) == 1
        assert report.drifts[0].recommendation_id == stop_id
        assert report.drifts[0].broker_price == pytest.approx(150.0)
        assert report.drifts[0].expected_price == pytest.approx(187.0)
        assert len(drifts) == 1

    async def test_a_modified_stop_quantity_raises_an_alert(self, session):
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[resting(order_ref=stop_id, quantity=75, aux_price=187.0)]
        )

        report = await manager.verify_coverage()

        assert len(report.drifts) == 1
        assert report.drifts[0].broker_quantity == pytest.approx(75)

    async def test_a_drifting_stop_is_not_silently_re_placed(self, session):
        """Correcting it would erase the evidence of whatever moved it."""
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[resting(order_ref=stop_id, quantity=100, aux_price=150.0)]
        )
        order_manager.submit_stop = AsyncMock(return_value="never")

        await manager.verify_coverage()

        order_manager.cancel_working_orders.assert_not_awaited()
        order_manager.submit_stop.assert_not_awaited()

    async def test_the_three_benign_cases_do_not_alert(self, session):
        """Missing, over-covered and under-levelled are all handled silently."""
        manager, order_manager = make_manager(session, broker_held=60)
        session.add(make_position(quantity=60, highest=260.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[resting(order_ref=stop_id, quantity=100, aux_price=187.0)]
        )
        order_manager.submit_stop = AsyncMock(return_value="5150")

        report = await manager.verify_coverage()

        assert report.drifts == []
        assert report.placed == ["5150"]

    async def test_a_partially_filled_stop_is_not_drift(self, session):
        """IB reporting 40 of 100 filled is the stop working, not drifting."""
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        ledger = OrderLedger(session)
        ledger.transition(stop_id, OrderStatus.PARTIALLY_FILLED)
        ledger.get(stop_id).filled_quantity = 40
        session.commit()
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[
                resting(
                    order_ref=stop_id, quantity=100, aux_price=187.0, filled=40
                )
            ]
        )

        report = await manager.verify_coverage()

        assert report.drifts == []


class TestInertWithTheFlagOff:
    """AC6 — the verifier does nothing until KAN-19's flag is on."""

    async def test_verify_makes_no_broker_call(self, session):
        manager, order_manager = make_manager(session, enabled=False)
        session.add(make_position())
        session.commit()

        report = await manager.verify_coverage()

        assert report.placed == []
        assert report.drifts == []
        order_manager.list_open_broker_orders.assert_not_awaited()


class TestNeverProtectsSharesTheAccountNoLongerHolds:
    """The ``positions`` row lags every fill until the projector applies it.

    Sizing a top-up off that row places protective sells for shares that are
    already sold — coverage above the holding is a short on trigger, not
    protection. Every placement is capped by what IB itself reports.
    """

    async def test_a_partial_fill_the_projector_has_not_applied_adds_nothing(
        self, session
    ):
        manager, order_manager = make_manager(session, broker_held=60)
        # Position row still says 100; IB has already sold 40 off the stop.
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        ledger = OrderLedger(session)
        ledger.transition(stop_id, OrderStatus.PARTIALLY_FILLED)
        ledger.get(stop_id).filled_quantity = 40
        session.commit()
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[
                resting(
                    order_ref=stop_id, quantity=100, aux_price=187.0, filled=40
                )
            ]
        )
        order_manager.submit_stop = AsyncMock(return_value="never")

        report = await manager.verify_coverage()

        assert report.placed == []
        order_manager.submit_stop.assert_not_awaited()

    async def test_a_stop_that_filled_completely_is_not_re_placed(self, session):
        """ib_insync drops filled orders from openTrades, so a fully-filled
        stop looks exactly like a deleted one. The broker position is what
        tells them apart: nothing is held, so nothing needs protecting."""
        manager, order_manager = make_manager(session, broker_held=0)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[unrelated_order()]
        )
        order_manager.submit_stop = AsyncMock(return_value="never")

        report = await manager.verify_coverage()

        assert report.released_intents == [stop_id]
        assert report.placed == []
        order_manager.submit_stop.assert_not_awaited()

    async def test_an_unreadable_broker_position_places_nothing(self, session):
        """Blind is not clear. Guessing here is what mints the naked short."""
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        order_manager.broker_position = AsyncMock(
            side_effect=RuntimeError("IB disconnected")
        )
        order_manager.submit_stop = AsyncMock(return_value="never")

        report = await manager.verify_coverage()

        assert report.placed == []
        order_manager.submit_stop.assert_not_awaited()

    async def test_two_sleeves_on_one_contract_share_the_broker_position(
        self, session
    ):
        """Sizing both against the full position double-covers the contract."""
        manager, order_manager = make_manager(session, broker_held=100)
        session.add(make_position(quantity=100, highest=220.0, portfolio="momentum"))
        session.add(make_position(quantity=100, highest=220.0, portfolio="value"))
        session.commit()
        placed: list[float] = []

        async def submit_stop(**kwargs):
            placed.append(kwargs["quantity"])
            return f"order-{len(placed)}"

        order_manager.submit_stop = submit_stop
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[unrelated_order()]
        )

        await manager.verify_coverage()

        # The book claims 200 shares across two sleeves while IB holds 100 —
        # a position divergence reconcile_paper reports as `major`. The first
        # sleeve by Position.id covers what is really there and the second
        # gets nothing; total coverage never exceeds the holding, which is the
        # property that matters here. Asserted as a split, not a sum: a
        # 100/100 split also sums to 200 and would be the naked short.
        assert placed == [pytest.approx(100)]


class TestAnEmptyOrderBookIsNotProofOfAbsence:
    """``connectAsync`` gives ``reqOpenOrders`` four seconds and only *logs* a
    timeout, so a slow Gateway leaves an empty book behind a connection that
    reports success. Believed, that terminalises every stop intent in the book
    and places a duplicate for each — and reconciliation reads the still-live
    originals as ``major``, disabling entries for the session."""

    async def test_a_single_empty_read_releases_nothing(self, session):
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        order_manager.list_open_broker_orders = AsyncMock(return_value=[])
        order_manager.submit_stop = AsyncMock(return_value="never")

        report = await manager.verify_coverage()

        assert report.released_intents == []
        assert report.placed == []
        # Its coverage still counts, so no duplicate is placed on top of a
        # stop that is very probably still resting.
        order_manager.submit_stop.assert_not_awaited()
        assert OrderLedger(session).get(stop_id).status == (
            OrderStatus.SUBMITTED.value
        )

    async def test_a_transient_empty_read_heals_without_touching_anything(
        self, session
    ):
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        still_there = resting(order_ref=stop_id, quantity=100, aux_price=187.0)
        order_manager.list_open_broker_orders = AsyncMock(
            side_effect=[[], [still_there]]
        )
        order_manager.submit_stop = AsyncMock(return_value="never")

        await manager.verify_coverage()
        report = await manager.verify_coverage()

        assert report.released_intents == []
        assert report.placed == []
        order_manager.submit_stop.assert_not_awaited()

    async def test_a_persistently_absent_stop_is_released_on_the_second_scan(
        self, session
    ):
        """The genuinely-deleted case still recovers — one cycle later."""
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        order_manager.list_open_broker_orders = AsyncMock(return_value=[])
        order_manager.submit_stop = AsyncMock(return_value="5150")

        first = await manager.verify_coverage()
        second = await manager.verify_coverage()

        assert first.released_intents == []
        assert second.released_intents == [stop_id]
        assert second.placed == ["5150"]


class TestAbsenceFromTheBookIsConfirmedBeforeRelease:
    """``openTrades`` is clientId-scoped. A stop placed under a different
    client id — a changed ``ib.client_id``, a second instance on a fallback —
    is invisible to us while resting perfectly well at IB, on every scan, so
    the two-scan rule alone would only delay the duplicate by 30 minutes."""

    async def test_a_stop_ib_still_knows_about_is_not_released(self, session):
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        # Synced book (another order visible) that simply does not list ours.
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[unrelated_order()]
        )
        order_manager.find_stop_order = AsyncMock(return_value="4242")
        order_manager.submit_stop = AsyncMock(return_value="never")

        report = await manager.verify_coverage()

        assert report.released_intents == []
        assert report.placed == []
        order_manager.submit_stop.assert_not_awaited()
        assert OrderLedger(session).get(stop_id).status == (
            OrderStatus.SUBMITTED.value
        )

    async def test_an_unanswerable_confirmation_leaves_coverage_claimed(
        self, session
    ):
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[unrelated_order()]
        )
        order_manager.find_stop_order = AsyncMock(
            side_effect=RuntimeError("IB went away")
        )
        order_manager.submit_stop = AsyncMock(return_value="never")

        report = await manager.verify_coverage()

        assert report.released_intents == []
        order_manager.submit_stop.assert_not_awaited()


class TestAnUntrustedMissIsNotBankedAsEvidence:
    """The two-scan rule needs two *evaluated* observations. A pass that
    declines to evaluate must not leave evidence behind for the next one."""

    async def test_a_stop_placed_this_pass_banks_no_evidence(self, session):
        """Otherwise `_skip_release` protects for one pass and then hands the
        next pass the single observation it needs to release anyway."""
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        ledger = OrderLedger(session)
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id="stop-DUN551088-momentum-265598-0",
                account_id="DUN551088",
                mode="paper",
                portfolio="momentum",
                con_id=265598,
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
                quantity=100,
                action="SELL",
                limit_price=187.0,
                order_type="stop",
            )
        )
        ledger.transition("stop-DUN551088-momentum-265598-0", OrderStatus.APPROVED)
        session.commit()
        # The book never syncs, on either pass.
        order_manager.list_open_broker_orders = AsyncMock(return_value=[])
        order_manager.submit_stop = AsyncMock(return_value="7000")

        await manager.verify_coverage()
        second = await manager.verify_coverage()

        assert second.released_intents == []
        assert order_manager.submit_stop.await_count == 1
        assert ledger.get("stop-DUN551088-momentum-265598-0").status == (
            OrderStatus.SUBMITTED.value
        )

    async def test_a_drift_frozen_scope_banks_no_evidence_for_its_siblings(
        self, session
    ):
        """A frozen scope never evaluates its missing stop, so that stop must
        not arrive at the next pass already holding one of its two proofs."""
        manager, order_manager = make_manager(session, broker_held=100)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        first = await seed_resting_stop(
            manager, order_manager, quantity=60, reference_price=220.0,
            order_id="4242",
        )
        second_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0,
            order_id="4343",
        )
        drifted = resting(
            order_id="4242", order_ref=first, quantity=60, aux_price=150.0
        )
        # Pass 1: drift freezes the scope. Pass 2: the book shows nothing.
        order_manager.list_open_broker_orders = AsyncMock(
            side_effect=[[drifted], []]
        )

        await manager.verify_coverage()
        report = await manager.verify_coverage()

        assert report.released_intents == []
        assert OrderLedger(session).get(second_id).status == (
            OrderStatus.SUBMITTED.value
        )

    async def test_an_unreadable_book_clears_the_prior_observation(
        self, session
    ):
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        order_manager.list_open_broker_orders = AsyncMock(
            side_effect=[[], RuntimeError("IB went away"), []]
        )
        order_manager.submit_stop = AsyncMock(return_value="never")

        await manager.verify_coverage()
        await manager.verify_coverage()
        report = await manager.verify_coverage()

        # The middle pass observed nothing at all, so the third is only the
        # second real observation and the stop keeps its coverage.
        assert report.released_intents == []
        assert OrderLedger(session).get(stop_id).status == (
            OrderStatus.SUBMITTED.value
        )


class TestADriftFrozenScopeStillClaimsItsShares:
    """A frozen scope places nothing but its stops are still resting. Leaving
    its share unspent lets the next sleeve size against the whole position."""

    async def test_a_second_sleeve_cannot_claim_the_frozen_sleeves_shares(
        self, session
    ):
        manager, order_manager = make_manager(session, broker_held=100)
        session.add(
            make_position(quantity=50, highest=220.0, portfolio="momentum")
        )
        # The book credits `value` with more than is left once the frozen
        # sleeve's resting stop is accounted for — without the reservation it
        # would size against the whole 100 and push coverage to 150.
        session.add(make_position(quantity=100, highest=220.0, portfolio="value"))
        session.commit()
        # momentum's stop drifted; value has none at all.
        await manager.ensure_coverage(
            account_id="DUN551088", portfolio="momentum", con_id=265598,
            symbol="AAPL", exchange="SMART", currency="USD", quantity=50,
            reference_price=220.0,
        )
        drifted_ref = order_manager.submit_stop.await_args.kwargs[
            "recommendation_id"
        ]
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[
                resting(order_ref=drifted_ref, quantity=50, aux_price=150.0)
            ]
        )
        placed: list[float] = []

        async def submit_stop(**kwargs):
            placed.append(kwargs["quantity"])
            return f"order-{len(placed)}"

        order_manager.submit_stop = submit_stop

        await manager.verify_coverage()

        # 100 held, 50 already resting under the frozen sleeve — value may
        # cover its own 50 and no more.
        assert placed == [pytest.approx(50)]

    async def test_a_frozen_scope_that_loses_more_coverage_pages_again(
        self, session
    ):
        """Dedup must not silence a position getting worse."""
        alerts: list = []
        manager, order_manager = make_manager(
            session,
            broker_held=100,
            on_drift=AsyncMock(side_effect=lambda d: alerts.append(d)),
        )
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        first = await seed_resting_stop(
            manager, order_manager, quantity=60, reference_price=220.0,
            order_id="4242",
        )
        await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0,
            order_id="4343",
        )
        drifted = resting(
            order_id="4242", order_ref=first, quantity=60, aux_price=150.0
        )
        sibling = resting(
            order_id="4343",
            order_ref=order_manager.submit_stop.await_args.kwargs[
                "recommendation_id"
            ],
            quantity=40,
            aux_price=187.0,
        )
        # Pass 1: drift, sibling present. Pass 2: same drift, sibling gone.
        order_manager.list_open_broker_orders = AsyncMock(
            side_effect=[[drifted, sibling], [drifted]]
        )

        await manager.verify_coverage()
        await manager.verify_coverage()

        assert len(alerts) == 2


class TestAnUnconfirmedCancelNeverFreesCoverage:
    """``cancelOrder`` is a request. A stop that refuses to go is still live,
    and terminalising its row lets the replacement double-cover the shares."""

    async def test_a_stop_ib_still_lists_is_left_resting(self, session):
        manager, order_manager = make_manager(session, broker_held=60)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        position = session.scalars(select(Position)).one()
        position.quantity = 60
        session.commit()
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[resting(order_ref=stop_id, quantity=100, aux_price=187.0)]
        )
        # IB still lists the ref after the cancel window.
        order_manager.cancel_working_orders = AsyncMock(return_value=[stop_id])
        order_manager.submit_stop = AsyncMock(return_value="never")

        report = await manager.verify_coverage()

        assert report.cancelled_order_ids == []
        assert OrderLedger(session).get(stop_id).status == (
            OrderStatus.SUBMITTED.value
        )
        order_manager.submit_stop.assert_not_awaited()


class TestDriftDoesNotSilentlyDropOtherCoverage:
    """A drift aborts the scope, so the abort must not have already released
    a sibling's coverage on the way past."""

    async def test_a_vanished_sibling_is_not_terminalised_during_a_drift(
        self, session
    ):
        manager, order_manager = make_manager(session, broker_held=100)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        first = await seed_resting_stop(
            manager, order_manager, quantity=60, reference_price=220.0,
            order_id="4242",
        )
        # ensure_coverage brings total coverage up to `quantity`, so this
        # places the remaining 40 as a second stop.
        second = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0,
            order_id="4343",
        )
        # The first drifted; the second is gone from IB entirely.
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[
                resting(
                    order_id="4242", order_ref=first, quantity=60,
                    aux_price=150.0,
                )
            ]
        )

        report = await manager.verify_coverage()

        assert len(report.drifts) == 1
        assert report.released_intents == []
        ledger = OrderLedger(session)
        assert ledger.get(second).status == OrderStatus.SUBMITTED.value

    async def test_an_unresolved_drift_pages_once_not_every_scan(self, session):
        """Paging every 30 minutes forever is how a channel gets muted — and
        this channel also carries 'your position is unprotected'."""
        alerts: list = []
        manager, order_manager = make_manager(
            session, on_drift=AsyncMock(side_effect=lambda d: alerts.append(d))
        )
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=220.0
        )
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[resting(order_ref=stop_id, quantity=100, aux_price=150.0)]
        )

        await manager.verify_coverage()
        await manager.verify_coverage()
        await manager.verify_coverage()

        assert len(alerts) == 1


class TestResizingNeverLoosensAStop:
    """AC5 holds on the resize branch too, not only the re-level branch."""

    async def test_a_shrunken_position_keeps_the_tighter_resting_level(
        self, session
    ):
        manager, order_manager = make_manager(session, broker_held=60)
        # The recorded high was corrected downward, so the IPS level (187) is
        # looser than what is already resting (221).
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        stop_id = await seed_resting_stop(
            manager, order_manager, quantity=100, reference_price=260.0
        )
        position = session.scalars(select(Position)).one()
        position.quantity = 60
        session.commit()
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[resting(order_ref=stop_id, quantity=100, aux_price=221.0)]
        )
        order_manager.submit_stop = AsyncMock(return_value="5150")

        await manager.verify_coverage()

        kwargs = order_manager.submit_stop.await_args.kwargs
        assert kwargs["quantity"] == pytest.approx(60)
        assert kwargs["stop_price"] == pytest.approx(221.0)


class TestApprovedButNeverSubmittedIsSweptOnTheScan:
    """A stop approved and never placed counts as coverage while nothing rests
    at IB. The backfill settles those at startup; a process up for weeks needs
    the same sweep on the scan (otherwise the position reads protected for
    ever)."""

    async def test_the_scan_resumes_an_unsubmitted_stop(self, session):
        manager, order_manager = make_manager(session)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        ledger = OrderLedger(session)
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id="stop-DUN551088-momentum-265598-0",
                account_id="DUN551088",
                mode="paper",
                portfolio="momentum",
                con_id=265598,
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
                quantity=100,
                action="SELL",
                limit_price=187.0,
                order_type="stop",
            )
        )
        ledger.transition("stop-DUN551088-momentum-265598-0", OrderStatus.APPROVED)
        session.commit()
        order_manager.list_open_broker_orders = AsyncMock(return_value=[])
        order_manager.submit_stop = AsyncMock(return_value="7000")

        await manager.verify_coverage()

        # Exactly one placement: the resumed order is not then read as
        # "vanished" because the book it was just placed into does not list it
        # yet. Two calls here would be two live stops for the same 100 shares.
        assert order_manager.submit_stop.await_count == 1
        assert order_manager.submit_stop.await_args.kwargs[
            "recommendation_id"
        ] == "stop-DUN551088-momentum-265598-0"
        row = ledger.get("stop-DUN551088-momentum-265598-0")
        assert row.ib_order_id == "7000"
        assert row.status == OrderStatus.SUBMITTED.value

    async def test_a_resume_for_a_shrunken_position_is_abandoned_not_clamped(
        self, session
    ):
        """Clamping would poison the row it re-drives.

        ``requested_quantity`` is one of the ledger's immutable economic
        fields, so a resume placing 60 against a row that says 100 could never
        record what it did. Every later scan then reads a 40-share gap between
        broker and ledger as drift — freezing all coverage maintenance on the
        position and paging, for ever, about a mismatch this code created.
        """
        manager, order_manager = make_manager(session, broker_held=60)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        ledger = OrderLedger(session)
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id="stop-DUN551088-momentum-265598-0",
                account_id="DUN551088",
                mode="paper",
                portfolio="momentum",
                con_id=265598,
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
                quantity=100,
                action="SELL",
                limit_price=187.0,
                order_type="stop",
            )
        )
        ledger.transition("stop-DUN551088-momentum-265598-0", OrderStatus.APPROVED)
        session.commit()
        placed: list[tuple[str, float]] = []

        async def submit_stop(**kwargs):
            placed.append((kwargs["recommendation_id"], kwargs["quantity"]))
            return f"order-{len(placed)}"

        order_manager.submit_stop = submit_stop
        order_manager.list_open_broker_orders = AsyncMock(
            return_value=[unrelated_order()]
        )

        await manager.verify_coverage()

        # The stale row is terminalised; a fresh intent covers what is held.
        assert ledger.get("stop-DUN551088-momentum-265598-0").status == (
            OrderStatus.SUBMISSION_FAILED.value
        )
        assert [quantity for _, quantity in placed] == [pytest.approx(60)]
        assert placed[0][0] != "stop-DUN551088-momentum-265598-0"
        # And the row the verifier now reads agrees with what actually rests.
        assert ledger.open_stop_quantity(
            "DUN551088", "momentum", 265598
        ) == pytest.approx(60)

    async def test_the_startup_backfill_applies_the_same_ceiling(self, session):
        """The backfill is the other caller, and the one with no safety net.

        Its own loop only walks contracts that still have an open Position
        row, so a stale APPROVED row for a contract the account has since sold
        has no scope — nothing there or in any later scan revisits it. Resumed
        unchecked, it rests as a protective sell against a flat contract for
        as long as the account exists.
        """
        manager, order_manager = make_manager(session, broker_held=0)
        ledger = OrderLedger(session)
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id="stop-DUN551088-momentum-999-0",
                account_id="DUN551088",
                mode="paper",
                portfolio="momentum",
                con_id=999,
                symbol="OLD",
                exchange="SMART",
                currency="USD",
                quantity=100,
                action="SELL",
                limit_price=187.0,
                order_type="stop",
            )
        )
        ledger.transition("stop-DUN551088-momentum-999-0", OrderStatus.APPROVED)
        session.commit()
        order_manager.submit_stop = AsyncMock(return_value="never")

        placed = await manager.backfill_open_positions()

        assert placed == []
        order_manager.submit_stop.assert_not_awaited()
        assert ledger.get("stop-DUN551088-momentum-999-0").status == (
            OrderStatus.SUBMISSION_FAILED.value
        )

    async def test_a_resume_for_a_flat_contract_is_abandoned(self, session):
        """A resume is a placement. The row can be days old, and the position
        it was approved for may have been sold since — in which case the
        'resumed' stop is a naked short waiting for a trigger."""
        manager, order_manager = make_manager(session, broker_held=0)
        session.add(make_position(quantity=100, highest=220.0))
        session.commit()
        ledger = OrderLedger(session)
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id="stop-DUN551088-momentum-265598-0",
                account_id="DUN551088",
                mode="paper",
                portfolio="momentum",
                con_id=265598,
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
                quantity=100,
                action="SELL",
                limit_price=187.0,
                order_type="stop",
            )
        )
        ledger.transition("stop-DUN551088-momentum-265598-0", OrderStatus.APPROVED)
        session.commit()
        order_manager.list_open_broker_orders = AsyncMock(return_value=[])
        order_manager.submit_stop = AsyncMock(return_value="never")

        await manager.verify_coverage()

        order_manager.submit_stop.assert_not_awaited()
        assert ledger.get("stop-DUN551088-momentum-265598-0").status == (
            OrderStatus.SUBMISSION_FAILED.value
        )


def _config() -> AppConfig:
    config = MagicMock(spec=AppConfig)
    config.execution = ExecutionConfig()
    config.ib = IBConfig()
    config.mode = "paper"
    config.risk = MagicMock()
    config.risk.min_viable_fill_pct = 40.0
    config.risk.passive_scan_interval_minutes = 30
    config.risk.stop_loss_trailing_pct = 15.0
    return config


class TestTheScanDrivesTheVerifier:
    """The 30-minute cadence, on the loop that owns the broker connection."""

    def _runner(self, *, enabled: bool):
        config = _config()
        config.execution.broker_stops_enabled = enabled
        config.ib.account_id = "DUN551088"
        order_manager = AsyncMock()
        order_manager.open_orders = {}
        runner = ExecutionServiceRunner(
            config=config,
            redis_client=AsyncMock(),
            order_manager=order_manager,
            order_ledger=MagicMock(),
        )
        return runner

    async def test_runs_on_the_first_call_then_waits_out_the_interval(self):
        runner = self._runner(enabled=True)
        runner._broker_stops.verify_coverage = AsyncMock()

        assert await runner.maybe_run_stop_verification(0.0) is True
        assert await runner.maybe_run_stop_verification(60.0) is False
        assert await runner.maybe_run_stop_verification(1800.0) is True
        assert runner._broker_stops.verify_coverage.await_count == 2

    async def test_a_verifier_failure_never_tears_down_the_loop(self):
        runner = self._runner(enabled=True)
        runner._broker_stops.verify_coverage = AsyncMock(
            side_effect=RuntimeError("IB went away")
        )

        assert await runner.maybe_run_stop_verification(0.0) is True

    async def test_the_scan_is_inert_with_the_flag_off(self):
        """AC6 — nothing runs, so nothing changes about the scan."""
        runner = self._runner(enabled=False)

        assert runner._broker_stops is None
        assert await runner.maybe_run_stop_verification(0.0) is False


class TestKillDoesNotOrphanStopCoverage:
    """AC4 / design test #29d.

    Cancel-all up front strips protection from every position at once, then
    liquidates them one at a time. A sell that fails mid-loop leaves its
    position both un-flattened and unprotected — and so does every position
    after it. Cancelling each stop immediately before its own sell bounds the
    unprotected window to the one position being flattened.
    """

    def _runner_with_two_positions(self, *, first_sell_fails: bool):
        from services.execution.order_manager import OrderManager

        config = _config()
        executor = AsyncMock()
        executor.cancel_order = AsyncMock(return_value=True)
        order_manager = OrderManager(
            executor=executor, redis_client=AsyncMock(), db_session=MagicMock()
        )
        now = datetime.now(timezone.utc)
        for order_id, ticker in (("stop-A", "AAPL"), ("stop-B", "MSFT")):
            order_manager.open_orders[order_id] = {
                "ticker": ticker,
                "quantity": 100,
                "limit_price": None,
                "stop_price": 187.0,
                "placed_at": now,
                "last_repriced_at": now,
                "reprice_count": 0,
                "recommendation_id": f"rec-{order_id}",
                "order_type": "stop",
            }
        order_manager.open_orders["entry-C"] = {
            "ticker": "NVDA",
            "quantity": 10,
            "limit_price": 500.0,
            "placed_at": now,
            "last_repriced_at": now,
            "reprice_count": 0,
            "recommendation_id": "rec-entry-C",
            "order_type": "limit",
        }

        order = []

        async def submit_exit(*, ticker, quantity, recommendation_id):
            order.append(("sell", ticker))
            if first_sell_fails and ticker == "AAPL":
                raise RuntimeError("IB refused the liquidation")
            return f"exit-{ticker}"

        order_manager.submit_exit = submit_exit
        original_cancel = order_manager.cancel_broker_order

        async def cancel_broker_order(order_id):
            order.append(("cancel", order_id))
            return await original_cancel(order_id)

        order_manager.cancel_broker_order = cancel_broker_order

        runner = ExecutionServiceRunner(
            config=config, redis_client=AsyncMock(), order_manager=order_manager
        )
        runner._positions = {"AAPL": 100, "MSFT": 50}
        return runner, order_manager, order

    def _kill(self) -> KillMessage:
        return KillMessage(
            timestamp=datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc),
            reason="drawdown breach",
            triggered_by="risk_management",
        )

    async def test_each_stop_is_cancelled_immediately_before_its_own_sell(self):
        runner, _, sequence = self._runner_with_two_positions(
            first_sell_fails=False
        )

        await runner.process_kill(self._kill())

        assert sequence == [
            ("cancel", "stop-A"),
            ("sell", "AAPL"),
            ("cancel", "stop-B"),
            ("sell", "MSFT"),
        ]

    async def test_a_failed_first_sell_leaves_the_second_stop_resting(self):
        runner, order_manager, sequence = self._runner_with_two_positions(
            first_sell_fails=True
        )

        await runner.process_kill(self._kill())

        # MSFT's stop was cancelled only when its own sell was attempted, and
        # AAPL — whose sell failed — is the only position left unprotected.
        assert sequence.index(("sell", "AAPL")) < sequence.index(
            ("cancel", "stop-B")
        )

    async def test_working_non_stop_orders_still_go_up_front(self):
        """A working entry must not fill into a book the kill is flattening."""
        runner, order_manager, sequence = self._runner_with_two_positions(
            first_sell_fails=False
        )

        await runner.process_kill(self._kill())

        assert "entry-C" not in order_manager.open_orders
        assert sequence[0] == ("cancel", "stop-A")

    async def test_the_stop_sweep_does_not_cancel_the_kills_own_exits(self):
        """The sweep must reach stops only.

        ``submit_exit`` tracks every liquidation in ``open_orders`` so a stuck
        one stays reachable — which means a blanket ``cancel_all_orders()``
        after the loop cancels the very sells the kill just ordered, reports
        ``positions_liquidated`` anyway, and leaves the book open *and*
        unprotected. Built with the real OrderManager: an AsyncMock stub never
        touches ``open_orders`` and cannot see this.
        """
        from services.execution.order_manager import OrderManager

        config = _config()
        executor = AsyncMock()
        executor.find_order_by_ref = AsyncMock(return_value=None)
        executor.submit_market_order = AsyncMock(
            side_effect=lambda ticker, quantity, **kw: f"exit-{ticker}"
        )
        executor.cancel_order = AsyncMock(return_value=True)
        executor.cancel_broker_order = AsyncMock(return_value=True)
        order_manager = OrderManager(
            executor=executor, redis_client=AsyncMock(), db_session=MagicMock()
        )
        now = datetime.now(timezone.utc)
        order_manager.open_orders["stop-A"] = {
            "ticker": "AAPL",
            "quantity": 100,
            "limit_price": None,
            "stop_price": 187.0,
            "placed_at": now,
            "last_repriced_at": now,
            "reprice_count": 0,
            "recommendation_id": "rec-stop-A",
            "order_type": "stop",
        }
        runner = ExecutionServiceRunner(
            config=config, redis_client=AsyncMock(), order_manager=order_manager
        )
        runner._positions = {"AAPL": 100}

        await runner.process_kill(self._kill())

        assert executor.submit_market_order.await_count == 1
        # The exit is still working at IB — nothing cancelled it.
        assert [c.args[0] for c in executor.cancel_order.await_args_list] == []
        assert "exit-AAPL" in order_manager.open_orders
        assert "stop-A" not in order_manager.open_orders

    async def test_a_stop_on_a_position_with_no_shares_is_still_cancelled(self):
        """Otherwise it rests against a flat book and sells short on trigger.

        MSFT is not in the book, so the per-position path never reaches its
        stop. The sweep after the liquidation loop is what catches it.
        """
        runner, order_manager, sequence = self._runner_with_two_positions(
            first_sell_fails=False
        )
        runner._positions = {"AAPL": 100}

        await runner.process_kill(self._kill())

        assert sequence == [
            ("cancel", "stop-A"),
            ("sell", "AAPL"),
            ("cancel", "stop-B"),
        ]
        assert order_manager.open_orders == {}
