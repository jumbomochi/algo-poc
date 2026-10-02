"""KAN-96: an order expired "absent from IB" must not erase who owns the fill.

2026-09-25: IB filled XLC order 219 (intent #223, sector_rotation) while
execution was disconnected. On 2026-09-30 the container's startup restore found
the order in neither open nor completed orders and expired the intent. The
book then saw only ``missing_in_db XLC`` with ``sleeve_mapping_required`` — the
expired intent that named the sleeve was never consulted, and nothing paged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scripts.reconcile_paper import _parse_unresolved, recent_absent_intents
from services.execution.reconciliation import (
    PositionReconciler,
    UnresolvedRepair,
    build_repair_plan,
)
from services.execution.runner import ExecutionServiceRunner
from shared.config import AppConfig, ExecutionConfig, IBConfig
from shared.models import Base, OrderStatus, Position
from shared.order_ledger import ABSENT_AT_IB_REASON, OrderLedger
from shared.schemas.messages import AlertMessage

ACCOUNT = "DUN551088"
XLC_CON_ID = 322317077
XLC_REC = "sleeve-2026-09-25-DUN551088-paper-sector_rotation-XLC-buy"
NOW = datetime(2026, 10, 2, 0, 35, tzinfo=timezone.utc)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed_submitted(
    ledger: OrderLedger,
    *,
    rec: str = XLC_REC,
    con_id: int = XLC_CON_ID,
    symbol: str = "XLC",
    portfolio: str = "sector_rotation",
    action: str = "BUY",
    ib_order_id: str = "219",
):
    proposal = SimpleNamespace(
        recommendation_id=rec,
        account_id=ACCOUNT,
        mode="paper",
        portfolio=portfolio,
        con_id=con_id,
        symbol=symbol,
        exchange="SMART",
        currency="USD",
        action=action,
        quantity=26.9848,
        limit_price=113.99,
        order_type="LMT",
    )
    ledger.create_intent(proposal)
    ledger.transition(rec, OrderStatus.APPROVED)
    ledger.record_submission(rec, ib_order_id)
    ledger.session.commit()


def _runner(session, *, broker_qty=26.0, broker_error=None):
    config = MagicMock(spec=AppConfig)
    config.execution = ExecutionConfig()
    config.ib = IBConfig()
    config.mode = "paper"
    config.risk = MagicMock()
    order_manager = AsyncMock()
    order_manager.open_orders = {}
    if broker_error is not None:
        order_manager.broker_position = AsyncMock(side_effect=broker_error)
    else:
        order_manager.broker_position = AsyncMock(return_value=broker_qty)
    redis = AsyncMock()
    ledger = OrderLedger(session)
    runner = ExecutionServiceRunner(
        config=config,
        redis_client=redis,
        order_manager=order_manager,
        order_ledger=ledger,
    )
    return runner, ledger, redis, order_manager


def _alerts(redis) -> list[AlertMessage]:
    return [
        AlertMessage.from_stream_dict(c.args[1])
        for c in redis.publish.await_args_list
        if c.args[0] == "stream:alerts"
    ]


async def _expire_on_absence(runner, order_id="219"):
    await runner.handle_ib_order_status({
        "order_id": order_id,
        "status": "Expired",
        "reason": ABSENT_AT_IB_REASON,
        "order_absent_at_ib": True,
    })


class TestExpiryOnAbsenceChecksTheBroker:
    async def test_ib_holding_shares_the_book_does_not_pages_a_probable_missed_fill(
        self, session
    ):
        runner, ledger, redis, order_manager = _runner(session, broker_qty=26.0)
        _seed_submitted(ledger)

        await _expire_on_absence(runner)

        assert ledger.get(XLC_REC).status == OrderStatus.EXPIRED.value
        order_manager.broker_position.assert_awaited_once_with(XLC_CON_ID)
        alerts = _alerts(redis)
        assert [a.event_type for a in alerts] == ["probable_missed_fill"]
        alert = alerts[0]
        assert alert.priority == "high"
        assert XLC_REC in alert.message
        assert "sector_rotation" in alert.message
        assert "restore_missed_entries.py" in alert.message
        assert alert.context["broker_qty"] == "26.0"
        assert alert.context["book_qty"] == "0.0"

    async def test_broker_matching_the_book_publishes_nothing(self, session):
        runner, ledger, redis, _ = _runner(session, broker_qty=10.0)
        _seed_submitted(ledger)
        session.add(Position(
            ticker="XLC", portfolio="sector_rotation", quantity=10.0,
            avg_entry_price=110.0, current_price=110.0, peak_price=110.0,
            highest_price_since_entry=110.0, opened_at=NOW, status="open",
            con_id=XLC_CON_ID, account_id=ACCOUNT,
        ))
        session.commit()

        await _expire_on_absence(runner)

        assert ledger.get(XLC_REC).status == OrderStatus.EXPIRED.value
        assert _alerts(redis) == []

    async def test_a_broker_error_leaves_the_intent_expired(self, session):
        runner, ledger, redis, _ = _runner(
            session, broker_error=RuntimeError("IB went away")
        )
        _seed_submitted(ledger)

        await _expire_on_absence(runner)

        assert ledger.get(XLC_REC).status == OrderStatus.EXPIRED.value
        assert _alerts(redis) == []

    async def test_an_absent_sell_is_not_checked(self, session):
        runner, ledger, redis, order_manager = _runner(session, broker_qty=26.0)
        _seed_submitted(ledger, action="SELL")

        await _expire_on_absence(runner)

        order_manager.broker_position.assert_not_awaited()
        assert _alerts(redis) == []


def _missing_xlc_result():
    return PositionReconciler(account_id=ACCOUNT).reconcile(
        broker_positions={XLC_CON_ID: 26.0},
        db_positions={},
        broker_orders={},
        db_orders={},
    )


def _absent(rec=XLC_REC, con_id=XLC_CON_ID, portfolio="sector_rotation"):
    return SimpleNamespace(
        recommendation_id=rec, con_id=con_id, portfolio=portfolio,
        account_id=ACCOUNT,
    )


class TestTheRepairPlanNamesTheCandidate:
    def test_one_absent_intent_on_the_contract_is_named(self):
        plan = build_repair_plan(
            _missing_xlc_result(), absent_intents=[_absent()]
        )

        assert plan.actions == []
        (entry,) = plan.unresolved
        assert entry.reason == "sleeve_mapping_required"
        assert entry.con_id == XLC_CON_ID
        assert entry.candidate_recommendation_id == XLC_REC
        assert entry.candidate_portfolio == "sector_rotation"

    def test_no_absent_intent_names_nothing(self):
        (entry,) = build_repair_plan(_missing_xlc_result()).unresolved

        assert entry.candidate_recommendation_id is None
        assert entry.candidate_portfolio is None

    def test_two_absent_intents_on_the_contract_name_nothing(self):
        plan = build_repair_plan(
            _missing_xlc_result(),
            absent_intents=[_absent(), _absent(rec="other", portfolio="momentum")],
        )

        (entry,) = plan.unresolved
        assert entry.candidate_recommendation_id is None
        assert entry.candidate_portfolio is None

    def test_an_absent_intent_on_another_contract_is_ignored(self):
        plan = build_repair_plan(
            _missing_xlc_result(), absent_intents=[_absent(con_id=4391)]
        )

        assert plan.unresolved[0].candidate_recommendation_id is None

    def test_the_plan_round_trips_through_the_apply_parser(self):
        plan = build_repair_plan(
            _missing_xlc_result(), absent_intents=[_absent()]
        )
        encoded = plan.to_dict()["unresolved"][0]

        assert _parse_unresolved(encoded) == UnresolvedRepair(
            reason="sleeve_mapping_required",
            con_id=XLC_CON_ID,
            candidate_recommendation_id=XLC_REC,
            candidate_portfolio="sector_rotation",
        )


class TestRecentAbsentIntents:
    def _expire(self, ledger, rec, *, reason, at):
        ledger.transition(rec, OrderStatus.EXPIRED, reason=reason)
        intent = ledger.get(rec)
        intent.terminal_at = at
        ledger.session.commit()

    def test_only_recent_absent_expiries_on_this_account_are_returned(
        self, session
    ):
        ledger = OrderLedger(session)
        _seed_submitted(ledger)
        self._expire(ledger, XLC_REC, reason=ABSENT_AT_IB_REASON,
                     at=NOW - timedelta(days=1))
        _seed_submitted(ledger, rec="old", ib_order_id="1")
        self._expire(ledger, "old", reason=ABSENT_AT_IB_REASON,
                     at=NOW - timedelta(days=11))
        _seed_submitted(ledger, rec="ib-said-expired", ib_order_id="2")
        self._expire(ledger, "ib-said-expired", reason=None,
                     at=NOW - timedelta(days=1))

        found = recent_absent_intents(session, account_id=ACCOUNT, now=NOW)

        assert [i.recommendation_id for i in found] == [XLC_REC]


class TestReviewHardening:
    async def test_a_redis_failure_while_paging_cannot_fail_startup(self, session):
        runner, ledger, redis, _ = _runner(session, broker_qty=26.0)
        redis.publish = AsyncMock(side_effect=ConnectionError("redis down"))
        _seed_submitted(ledger)

        await _expire_on_absence(runner)  # must not raise

        assert ledger.get(XLC_REC).status == OrderStatus.EXPIRED.value

    def test_a_held_contract_with_a_missed_fill_is_not_handed_to_the_holder(self):
        """Another sleeve already holds XLC, so reconciliation reports a
        quantity_mismatch, not missing_in_db. set_position_quantity would give
        the missed shares to the holder at no cost basis — the harm this
        ticket exists to prevent."""
        result = PositionReconciler(account_id=ACCOUNT).reconcile(
            broker_positions={XLC_CON_ID: 56.0},
            db_positions={XLC_CON_ID: SimpleNamespace(
                account_id=ACCOUNT, quantity=30.0, portfolio="momentum"
            )},
            broker_orders={},
            db_orders={},
        )

        plan = build_repair_plan(result, absent_intents=[_absent()])

        assert plan.actions == []
        (entry,) = plan.unresolved
        assert entry.reason == "probable_missed_fill"
        assert entry.candidate_recommendation_id == XLC_REC
        assert entry.candidate_portfolio == "sector_rotation"

    def test_a_held_contract_without_a_candidate_keeps_its_repair_action(self):
        result = PositionReconciler(account_id=ACCOUNT).reconcile(
            broker_positions={XLC_CON_ID: 56.0},
            db_positions={XLC_CON_ID: SimpleNamespace(
                account_id=ACCOUNT, quantity=30.0, portfolio="momentum"
            )},
            broker_orders={},
            db_orders={},
        )

        plan = build_repair_plan(result)

        assert [a.action for a in plan.actions] == ["set_position_quantity"]
        assert plan.unresolved == []

    def test_an_absent_sell_is_never_a_candidate(self, session):
        ledger = OrderLedger(session)
        _seed_submitted(ledger, action="SELL")
        ledger.transition(XLC_REC, OrderStatus.EXPIRED, reason=ABSENT_AT_IB_REASON)
        ledger.get(XLC_REC).terminal_at = NOW - timedelta(days=1)
        session.commit()

        assert recent_absent_intents(session, account_id=ACCOUNT, now=NOW) == []
