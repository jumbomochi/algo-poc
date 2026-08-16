from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from shared.models import Base, OrderIntent, OrderStatus
from shared.order_ledger import (
    ConflictingOrderIntent,
    InvalidOrderTransition,
    OrderLedger,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def make_proposal(
    recommendation_id: str,
    *,
    quantity: float = 5,
    price: float | None = 100,
    action: str = "BUY",
):
    return SimpleNamespace(
        recommendation_id=recommendation_id,
        account_id="DU12345",
        mode="paper",
        portfolio="momentum",
        con_id=265598,
        symbol="AAPL",
        exchange="SMART",
        currency="USD",
        action=action,
        quantity=quantity,
        limit_price=price,
        order_type="LMT",
    )


def test_terminal_intent_cannot_transition(session):
    ledger = OrderLedger(session)
    ledger.create_intent(make_proposal("rec-1"))
    ledger.transition("rec-1", OrderStatus.RISK_REJECTED, reason="sector")
    with pytest.raises(InvalidOrderTransition):
        ledger.transition("rec-1", OrderStatus.APPROVED)


def test_active_buy_reservation_uses_unfilled_notional(session):
    ledger = OrderLedger(session)
    ledger.create_intent(make_proposal("rec-1", quantity=10, price=100))
    ledger.transition("rec-1", OrderStatus.APPROVED)
    intent = ledger.get("rec-1")
    intent.filled_quantity = 4
    session.flush()
    assert ledger.active_reservations("momentum") == pytest.approx(600.0)


def test_active_reservations_can_exclude_retried_recommendation(session):
    ledger = OrderLedger(session)
    for recommendation_id in ("rec-current", "rec-other"):
        ledger.create_intent(make_proposal(recommendation_id, quantity=10, price=100))
        ledger.transition(recommendation_id, OrderStatus.APPROVED)

    assert ledger.active_reservations(
        "momentum", exclude_recommendation_id="rec-current"
    ) == pytest.approx(1_000)


def test_create_intent_is_idempotent(session):
    ledger = OrderLedger(session)
    first = ledger.create_intent(make_proposal("rec-1"))
    second = ledger.create_intent(make_proposal("rec-1"))
    assert first.id == second.id


def test_create_intent_rejects_conflicting_recommendation_reuse(session):
    ledger = OrderLedger(session)
    ledger.create_intent(make_proposal("rec-1", quantity=5))

    with pytest.raises(ConflictingOrderIntent):
        ledger.create_intent(make_proposal("rec-1", quantity=6))


def test_create_intent_recovers_matching_concurrent_insert(session, monkeypatch):
    first = OrderLedger(session).create_intent(make_proposal("rec-1"))
    ledger = OrderLedger(session)
    original_locked = ledger._locked
    calls = 0

    def miss_once(recommendation_id, *, required):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return original_locked(recommendation_id, required=required)

    monkeypatch.setattr(ledger, "_locked", miss_once)
    replay = ledger.create_intent(make_proposal("rec-1"))

    assert replay.id == first.id
    assert session.in_transaction()


def test_create_intent_is_rolled_back_by_caller(session):
    ledger = OrderLedger(session)
    ledger.create_intent(make_proposal("rec-1"))

    session.rollback()

    assert (
        session.scalar(
            select(OrderIntent).where(OrderIntent.recommendation_id == "rec-1")
        )
        is None
    )


def test_submission_pending_load_and_publication_lifecycle(session):
    ledger = OrderLedger(session)
    intent = ledger.create_intent(make_proposal("rec-1"))
    ledger.mark_published("rec-1")
    ledger.transition("rec-1", OrderStatus.APPROVED)
    ledger.record_submission("rec-1", "42")

    assert intent.published_at is not None
    assert intent.ib_order_id == "42"
    assert intent.status == OrderStatus.SUBMITTED.value
    assert ledger.load_pending_orders() == [intent]


def test_approved_intent_is_loaded_as_pending_before_submission(session):
    ledger = OrderLedger(session)
    intent = ledger.create_intent(make_proposal("rec-approved"))
    ledger.transition("rec-approved", OrderStatus.APPROVED)

    assert ledger.load_pending_orders() == [intent]


def test_repository_flushes_without_committing(session, monkeypatch):
    ledger = OrderLedger(session)
    monkeypatch.setattr(session, "commit", lambda: pytest.fail("unexpected commit"))

    ledger.create_intent(make_proposal("rec-1"))
    ledger.transition("rec-1", OrderStatus.APPROVED)
    ledger.mark_published("rec-1")
    ledger.record_submission("rec-1", "42")


def test_transition_sets_lifecycle_timestamps(session):
    ledger = OrderLedger(session)
    intent = ledger.create_intent(make_proposal("rec-1"))
    ledger.transition("rec-1", OrderStatus.APPROVED)
    assert intent.approved_at is not None

    ledger.record_submission("rec-1", "42")
    assert intent.submitted_at is not None

    ledger.transition("rec-1", OrderStatus.FILLED)
    assert intent.terminal_at is not None
    assert intent.updated_at >= intent.created_at


def test_mark_published_accepts_explicit_timestamp(session):
    ledger = OrderLedger(session)
    intent = ledger.create_intent(make_proposal("rec-1"))
    published_at = datetime(2026, 7, 19, tzinfo=timezone.utc)

    ledger.mark_published("rec-1", published_at=published_at)

    assert intent.published_at == published_at


class TestExitSuppressionQueries:
    """KAN-7: the two queries the recurring exit paths depend on."""

    def test_nonterminal_sell_is_scoped_to_the_identity_not_the_ticker(
        self, session
    ):
        """One sleeve's working sell must not strand another sleeve's exit, and
        a filled sell must not block the next breach forever."""
        ledger = OrderLedger(session)
        ledger.create_intent(make_proposal("sell-momentum", action="SELL"))
        scope = {"account_id": "DU12345", "con_id": 265598}

        assert ledger.nonterminal_sell_exists(portfolio="momentum", **scope)
        assert not ledger.nonterminal_sell_exists(portfolio="quality", **scope)

        ledger.transition("sell-momentum", OrderStatus.RISK_REJECTED, reason="x")
        assert not ledger.nonterminal_sell_exists(portfolio="momentum", **scope)

    def test_a_proposed_sell_counts_as_outstanding(self, session):
        """PENDING_ORDER_STATUSES omits PROPOSED; suppression must not, or an
        exit created-but-not-yet-published gets emitted a second time."""
        ledger = OrderLedger(session)
        intent = ledger.create_intent(make_proposal("sell-1", action="SELL"))

        assert intent.status == OrderStatus.PROPOSED.value
        assert ledger.nonterminal_sell_exists(
            account_id="DU12345", portfolio="momentum", con_id=265598
        )

    def test_a_buy_never_suppresses_an_exit(self, session):
        ledger = OrderLedger(session)
        ledger.create_intent(make_proposal("buy-1", action="BUY"))

        assert not ledger.nonterminal_sell_exists(
            account_id="DU12345", portfolio="momentum", con_id=265598
        )

    def test_prefix_count_escapes_like_wildcards(self, session):
        """Sleeve names contain underscores (`thematic_momentum`), and `_` is a
        single-character wildcard in LIKE. Unescaped, one sleeve's exits would
        be counted against another's sequence."""
        ledger = OrderLedger(session)
        ledger.create_intent(make_proposal("stop-loss-DU-thematic_momentum-1-x-0"))
        ledger.create_intent(make_proposal("stop-loss-DU-thematicXmomentum-1-x-0"))

        assert ledger.count_intents_with_id_prefix(
            "stop-loss-DU-thematic_momentum-1-x-"
        ) == 1
        assert ledger.count_intents_with_id_prefix("stop-loss-DU-") == 2

    def test_latest_in_a_family_is_the_most_recent_attempt(self, session):
        """KAN-9: the re-fire decision is made against the *last* attempt on the
        scope — an earlier, already-terminal one says nothing about whether a
        sell is working now."""
        ledger = OrderLedger(session)
        ledger.create_intent(make_proposal("stop-loss-DU-momentum-1-x-0"))
        ledger.create_intent(make_proposal("stop-loss-DU-momentum-1-x-1"))
        ledger.create_intent(make_proposal("stop-loss-DU-quality-1-x-0"))

        latest = ledger.latest_intent_with_id_prefix("stop-loss-DU-momentum-1-x-")

        assert latest.recommendation_id == "stop-loss-DU-momentum-1-x-1"

    def test_latest_in_an_empty_family_is_none(self, session):
        """No prior attempt is the ordinary case — a first breach."""
        ledger = OrderLedger(session)
        ledger.create_intent(make_proposal("stop-loss-DU-momentum-1-x-0"))

        assert (
            ledger.latest_intent_with_id_prefix("stop-loss-DU-quality-1-x-") is None
        )

    def test_latest_can_be_scoped_to_one_mode(self, session):
        """The re-publish sweep only ever sees intents in the running mode, so
        the re-fire decision must be made over the same set — otherwise a row
        the sweep cannot act on decides whether an exit is in flight."""
        ledger = OrderLedger(session)
        ledger.create_intent(make_proposal("stop-loss-DU-momentum-1-x-0"))
        live = ledger.create_intent(make_proposal("stop-loss-DU-momentum-1-x-1"))
        live.mode = "live"
        session.flush()

        latest = ledger.latest_intent_with_id_prefix(
            "stop-loss-DU-momentum-1-x-", mode="paper"
        )

        assert latest.recommendation_id == "stop-loss-DU-momentum-1-x-0"

    def test_latest_escapes_like_wildcards(self, session):
        """Same trap as the count: `_` is a LIKE wildcard and sleeve names carry
        one, so `thematic_momentum` must not adopt `thematicXmomentum`'s row."""
        ledger = OrderLedger(session)
        ledger.create_intent(make_proposal("stop-loss-DU-thematic_momentum-1-x-0"))
        ledger.create_intent(make_proposal("stop-loss-DU-thematicXmomentum-1-x-1"))

        latest = ledger.latest_intent_with_id_prefix(
            "stop-loss-DU-thematic_momentum-1-x-"
        )

        assert latest.recommendation_id == "stop-loss-DU-thematic_momentum-1-x-0"
