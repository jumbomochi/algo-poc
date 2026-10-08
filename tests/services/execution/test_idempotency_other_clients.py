"""KAN-112: never submit a second order for a recommendation another client has.

Every submit path asks ``IBExecutor.find_order_by_ref`` before placing. It used
to look for a WORKING order only among this client's trades (client 1) — the
KAN-106 scoping, deliberate for every other reader of ``openTrades()`` — so an
order an ops or repair tool (``broker_stop_spike.py``, ``convert_paper_fx.py``)
had working under our ``recommendation_id`` was invisible, and after a restart
execution would place a second one.

The probe now asks IB for every client's open orders (``reqAllOpenOrders``,
bounded, through KAN-106's ``_open_orders_request``):

* ours → adopted and bound, as before; a completed order → adopted, as before;
* another client's → not submitted, not bound, paged once, intent left APPROVED;
* no answer → not submitted this pass; retried until IB answers. The kill
  switch's own liquidation is the one exception: it submits anyway.

The executor here runs on a real ib_insync ``IB`` whose socket is faked at the
``Client``: IB's answers arrive through the wrapper's own ``openOrder`` /
``openOrderEnd`` and ``completedOrder`` / ``completedOrdersEnd`` callbacks, a
loop turn later, and a placement goes through the real ``IB.placeOrder`` down
to a recorded ``Client.placeOrder``. The ledger is a real (sqlite) one.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.execution.ib_executor import (
    BrokerStateUnavailableError,
    IBExecutor,
    OrderPlacedElsewhere,
    WrongAccountTypeError,
)
from services.execution.order_manager import (
    KillProbe,
    OrderManager,
    OrderPlacedElsewhereError,
    SubmissionDeferredError,
)
from services.execution.runner import (
    APPROVED_ORDERS_STREAM,
    CONSUMER_GROUP,
    DEFERRED_PAST_SESSION_REASON,
    ExecutionServiceRunner,
    HaltStateUnavailable,
)
from shared.market_calendar import MarketCalendar
from shared.config import AppConfig, ExecutionConfig, IBConfig
from shared.liquidation import liquidation_exit_id
from shared.models import Base, OrderStatus, PortfolioConfig, Position
from shared.order_ledger import OrderLedger
from shared.schemas.messages import AlertMessage, ApprovedOrderMessage, KillMessage

ACCOUNT = "DUN551088"
OUR_CLIENT = 1
REPAIR_CLIENT = 58  # an ops/repair tool's client id
PORTFOLIO = "momentum"
AAPL = ("AAPL", 265598)
MSFT = ("MSFT", 272093)
BUY_REC = "sleeve-2026-10-08-DUN551088-paper-momentum-AAPL-buy"
SELL_REC = "sleeve-2026-10-08-DUN551088-paper-momentum-MSFT-sell"
STOP_REC = f"stop-{ACCOUNT}-{PORTFOLIO}-{AAPL[1]}-0"  # the first stop id minted
SEEDED_AT = datetime(2026, 10, 8, 8, 0, tzinfo=timezone.utc)
SHORT_BOUND = 0.01  # seconds; the probe's request bounds, shortened
# SEEDED_AT is 04:00 ET on Thursday 2026-10-08: the BUYs here are sized for
# that day's session, which closes 16:00 ET (20:00 UTC).
SESSION_CLOSE = datetime(2026, 10, 8, 20, 0, tzinfo=timezone.utc)
_CALENDAR: list = []


def _calendar() -> MarketCalendar:
    if not _CALENDAR:
        _CALENDAR.append(MarketCalendar())
    return _CALENDAR[0]


# --------------------------------------------------------------------------
# A real ib_insync IB behind a faked socket
# --------------------------------------------------------------------------


def _contract(symbol_con_id):
    from ib_insync import Contract

    symbol, con_id = symbol_con_id
    return Contract(conId=con_id, symbol=symbol, secType="STK",
                    exchange="SMART", currency="USD")


def _order(order_id, client_id, ref, *, action="BUY", quantity=10.0,
           order_type="LMT"):
    from ib_insync import Order

    return Order(
        orderId=order_id, clientId=client_id,
        permId=1_000_000 + client_id * 10_000 + order_id,
        action=action, totalQuantity=quantity, orderType=order_type,
        lmtPrice=100.0 if order_type == "LMT" else 0.0, orderRef=ref,
        account=ACCOUNT,
    )


class Gateway:
    """IB as the wrapper sees it. ``open_orders`` / ``completed`` are what IB
    holds account-wide; ``answers_*`` False means the request goes out and
    nothing ever comes back (the executor's bound decides)."""

    def __init__(self) -> None:
        from ib_insync import IB

        self.ib = IB()
        self.ib.isConnected = lambda: True
        self.ib.managedAccounts = lambda: [ACCOUNT]
        self.ib.wrapper.clientId = OUR_CLIENT
        self.open_orders: list[tuple] = []  # (contract, order, status)
        self.completed: list[tuple] = []
        self.answers_open = True
        self.answers_completed = True
        self.open_requests = 0
        self.completed_requests = 0
        self.placed: list = []  # (orderId, contract, order) sent to IB
        self._next_id = 500
        client = self.ib.client
        client.reqAllOpenOrders = self._req_all_open_orders
        client.reqCompletedOrders = self._req_completed_orders
        client.getReqId = self._get_req_id
        client.placeOrder = self._place_order

    def working(self, symbol_con_id, order_id, client_id, ref, *,
                status="Submitted", **kw) -> None:
        self.open_orders.append(
            (_contract(symbol_con_id), _order(order_id, client_id, ref, **kw),
             status)
        )

    def done(self, symbol_con_id, order_id, client_id, ref, status="Filled",
             **kw) -> None:
        self.completed.append(
            (_contract(symbol_con_id), _order(order_id, client_id, ref, **kw),
             status)
        )

    def cache_own(self, symbol_con_id, order_id, ref, **kw) -> None:
        """An order of ours ib_insync cached at connect (its own open-order
        sync), with IB itself now not answering."""
        from ib_insync import OrderState

        self.ib.wrapper.openOrder(
            order_id, _contract(symbol_con_id),
            _order(order_id, OUR_CLIENT, ref, **kw), OrderState(status="Submitted"),
        )

    def go_down(self, executor: IBExecutor) -> None:
        """The socket drops and every reconnect is refused."""
        self.ib.isConnected = lambda: False
        executor.connect = AsyncMock(
            side_effect=ConnectionRefusedError("gateway down")
        )

    def come_back(self) -> None:
        self.ib.isConnected = lambda: True

    def _req_all_open_orders(self) -> None:
        from ib_insync import OrderState

        self.open_requests += 1
        if not self.answers_open:
            return
        wrapper = self.ib.wrapper

        def answer() -> None:
            for contract, order, status in self.open_orders:
                wrapper.openOrder(
                    order.orderId, contract, copy.copy(order),
                    OrderState(status=status),
                )
            wrapper.openOrderEnd()

        asyncio.get_running_loop().call_soon(answer)

    def _req_completed_orders(self, api_only) -> None:
        from ib_insync import OrderState

        self.completed_requests += 1
        if not self.answers_completed:
            return
        wrapper = self.ib.wrapper

        def answer() -> None:
            for contract, order, status in self.completed:
                wrapper.completedOrder(
                    contract, copy.copy(order), OrderState(status=status)
                )
            wrapper.completedOrdersEnd()

        asyncio.get_running_loop().call_soon(answer)

    def _get_req_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _place_order(self, order_id, contract, order) -> None:
        self.placed.append((order_id, contract, order))
        # From now on IB lists it, as it would.
        self.open_orders.append((contract, copy.copy(order), "Submitted"))

    def foreign_trade(self, order_id):
        """The wrapper's Trade for another client's order, once IB listed it."""
        return self.ib.wrapper.trades[(REPAIR_CLIENT, order_id)]


def _executor(gw: Gateway) -> IBExecutor:
    executor = IBExecutor("h", 7497, OUR_CLIENT, account_id=ACCOUNT)
    executor._logger = MagicMock()
    executor._ib = gw.ib
    return executor


def _short_bounds():
    return (
        patch("services.execution.ib_executor.REQ_OPEN_ORDERS_TIMEOUT_SECONDS",
              SHORT_BOUND),
        patch(
            "services.execution.ib_executor.REQ_COMPLETED_ORDERS_TIMEOUT_SECONDS",
            SHORT_BOUND,
        ),
    )


# --------------------------------------------------------------------------
# The probe itself
# --------------------------------------------------------------------------


class TestTheProbe:
    async def test_another_clients_working_order_is_reported_not_bound(self):
        gw = Gateway()
        gw.working(AAPL, 77, REPAIR_CLIENT, BUY_REC)
        executor = _executor(gw)

        found = await executor.find_order_by_ref(BUY_REC)

        assert found == OrderPlacedElsewhere(
            recommendation_id=BUY_REC, order_id="77", client_id=REPAIR_CLIENT,
            ticker="AAPL", action="BUY", status="Submitted", quantity=10.0,
        )
        assert executor._trades == {} and executor._trade_meta == {}
        theirs = gw.foreign_trade(77)
        assert len(theirs.fillEvent) == 0
        assert len(theirs.commissionReportEvent) == 0
        # An open answer settles it: completed history is not asked.
        assert gw.completed_requests == 0

    async def test_the_answer_is_neither_a_bindable_id_nor_nothing(self):
        gw = Gateway()
        gw.working(AAPL, 77, REPAIR_CLIENT, BUY_REC)

        found = await _executor(gw).find_order_by_ref(BUY_REC)

        assert not isinstance(found, (str, int))
        assert found is not None and bool(found) is True

    async def test_our_own_working_order_is_bound_once_and_returned(self):
        gw = Gateway()
        gw.working(AAPL, 241, OUR_CLIENT, BUY_REC)
        executor = _executor(gw)

        assert await executor.find_order_by_ref(BUY_REC) == "241"
        assert await executor.find_order_by_ref(BUY_REC) == "241"

        ours = gw.ib.wrapper.trades[(OUR_CLIENT, 241)]
        assert executor._trades["241"] is ours
        assert executor._trade_meta["241"] == ("AAPL", "buy")
        assert len(ours.commissionReportEvent) == 1  # never bound twice

    async def test_ours_wins_when_another_client_has_one_too(self):
        gw = Gateway()
        gw.working(AAPL, 77, REPAIR_CLIENT, BUY_REC)
        gw.working(AAPL, 241, OUR_CLIENT, BUY_REC)
        executor = _executor(gw)

        assert await executor.find_order_by_ref(BUY_REC) == "241"
        assert set(executor._trades) == {"241"}

    async def test_a_completed_order_under_any_client_is_returned_as_before(self):
        gw = Gateway()
        gw.done(AAPL, 77, REPAIR_CLIENT, BUY_REC, status="Cancelled")
        executor = _executor(gw)

        assert await executor.find_order_by_ref(BUY_REC) == "77"
        assert gw.open_requests == 1 and gw.completed_requests == 1

    async def test_nothing_anywhere_is_none(self):
        gw = Gateway()
        gw.working(AAPL, 77, REPAIR_CLIENT, "some-other-ref")

        assert await _executor(gw).find_order_by_ref(BUY_REC) is None

    async def test_an_unanswered_open_orders_request_raises(self):
        gw = Gateway()
        gw.answers_open = False
        gw.done(AAPL, 77, OUR_CLIENT, BUY_REC)
        open_bound, completed_bound = _short_bounds()

        with open_bound, completed_bound:
            with pytest.raises(BrokerStateUnavailableError):
                await _executor(gw).find_order_by_ref(BUY_REC)

        # Completed history alone cannot say nothing is working.
        assert gw.completed_requests == 0

    async def test_an_unanswered_completed_orders_request_raises(self):
        gw = Gateway()
        gw.answers_completed = False
        open_bound, completed_bound = _short_bounds()

        with open_bound, completed_bound:
            with pytest.raises(BrokerStateUnavailableError):
                await _executor(gw).find_order_by_ref(BUY_REC)

    async def test_kan106_scoping_survives_the_probe(self):
        """The probe leaves another client's order in ib_insync's trade cache,
        exactly as the KAN-106 resolution does. The halt sweep's list and the
        cancel fallback still see only ours."""
        gw = Gateway()
        gw.working(AAPL, 241, OUR_CLIENT, BUY_REC)
        gw.working(MSFT, 77, REPAIR_CLIENT, SELL_REC, action="SELL")
        executor = _executor(gw)
        gw.ib.cancelOrder = MagicMock()

        assert isinstance(
            await executor.find_order_by_ref(SELL_REC), OrderPlacedElsewhere
        )
        listed = await executor.list_open_orders()

        assert [o.order_id for o in listed] == ["241"]
        assert await executor.cancel_broker_order("77") is False
        gw.ib.cancelOrder.assert_not_called()


# --------------------------------------------------------------------------
# The production wiring: runner -> order manager -> executor -> ledger
# --------------------------------------------------------------------------


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


class Harness:
    def __init__(self, session: Session, *, broker_stops: bool = False) -> None:
        session.add(PortfolioConfig(
            portfolio=PORTFOLIO, capital=100_000, cash=100_000,
            created_at=SEEDED_AT, updated_at=SEEDED_AT,
        ))
        session.commit()
        self.session = session
        self.gw = Gateway()
        self.executor = _executor(self.gw)
        self.redis = AsyncMock()
        self.redis.read_group = AsyncMock(return_value=[])
        self.redis.drain_pending = AsyncMock(return_value=[])
        self.order_manager = OrderManager(
            executor=self.executor, redis_client=self.redis,
            db_session=MagicMock(),
        )
        self.order_manager._logger = MagicMock()
        config = MagicMock(spec=AppConfig)
        config.execution = ExecutionConfig(broker_stops_enabled=broker_stops)
        config.ib = IBConfig(account_id=ACCOUNT)
        config.mode = "paper"
        config.risk = MagicMock()
        config.risk.stop_loss_trailing_pct = 15.0
        config.risk.passive_scan_interval_minutes = 30
        self.ledger = OrderLedger(session)
        self.runner = ExecutionServiceRunner(
            config=config, redis_client=self.redis,
            order_manager=self.order_manager, order_ledger=self.ledger,
        )
        self.executor.set_order_status_handler(
            self.runner.handle_ib_order_status
        )
        # Inside the session the BUYs were sized for, unless a test moves it.
        self.now = SEEDED_AT + timedelta(hours=1)
        self.runner._utcnow = lambda: self.now
        self.runner._deferral_calendar = _calendar()
        self._next_message = 0

    def due(self) -> float:
        """A loop time at which every deferred message is due."""
        return max(r.due_at for r in self.runner._deferred_messages.values())

    async def retry_when_due(self) -> bool:
        return await self.runner.maybe_retry_deferred_orders(self.due())

    def approve(self, rec: str, symbol_con_id, action: str,
                quantity: float = 10.0) -> ApprovedOrderMessage:
        symbol, con_id = symbol_con_id
        self.ledger.create_intent(SimpleNamespace(
            recommendation_id=rec, account_id=ACCOUNT, mode="paper",
            portfolio=PORTFOLIO, con_id=con_id, symbol=symbol,
            exchange="SMART", currency="USD", action=action.upper(),
            quantity=quantity, limit_price=100.0 if action == "buy" else None,
            order_type="LMT" if action == "buy" else "MKT",
        ))
        self.ledger.transition(rec, OrderStatus.APPROVED)
        self.session.commit()
        return ApprovedOrderMessage(
            ticker=symbol, timestamp=SEEDED_AT, action=action,
            quantity=quantity,
            order_type="limit" if action == "buy" else "market",
            limit_price=100.0 if action == "buy" else None,
            recommendation_id=rec, portfolio=PORTFOLIO,
        )

    def message(self, order: ApprovedOrderMessage):
        self._next_message += 1
        return SimpleNamespace(
            message_id=f"{self._next_message}-0", data=order.to_stream_dict()
        )

    async def deliver(self, order: ApprovedOrderMessage):
        """One approved order through the loop's own consume path."""
        msg = self.message(order)
        self.redis.read_group = AsyncMock(return_value=[msg])
        await self.runner._consume_and_process(
            APPROVED_ORDERS_STREAM,
            ApprovedOrderMessage.from_stream_dict,
            self.runner.process_approved_order,
            count=10,
            block_ms=0,
        )
        return msg

    def status(self, rec: str) -> str:
        status = self.ledger.get(rec).status
        self.session.rollback()
        return status

    def ib_order_id(self, rec: str) -> str | None:
        order_id = self.ledger.get(rec).ib_order_id
        self.session.rollback()
        return order_id

    def acked(self, msg) -> bool:
        return any(
            c.args == (APPROVED_ORDERS_STREAM, CONSUMER_GROUP, msg.message_id)
            for c in self.redis.ack.await_args_list
        )

    def alerts(self, event_type: str | None = None) -> list[AlertMessage]:
        alerts = [
            AlertMessage.from_stream_dict(c.args[1])
            for c in self.redis.publish.await_args_list
            if c.args[0] == "stream:alerts"
        ]
        if event_type is None:
            return alerts
        return [a for a in alerts if a.event_type == event_type]

    def placed_refs(self) -> list[str]:
        return [order.orderRef for _id, _contract, order in self.gw.placed]


class TestEntryAndExitPaths:
    @pytest.mark.parametrize(
        ("rec", "symbol_con_id", "action", "path"),
        [
            (BUY_REC, AAPL, "buy", "entry"),
            (SELL_REC, MSFT, "sell", "exit"),
        ],
    )
    async def test_another_clients_working_order_blocks_it_and_pages_once(
        self, session, rec, symbol_con_id, action, path
    ):
        h = Harness(session)
        order = h.approve(rec, symbol_con_id, action)
        h.gw.working(symbol_con_id, 77, REPAIR_CLIENT, rec,
                     action=action.upper())

        first = await h.deliver(order)
        redelivered = await h.deliver(order)  # e.g. a PEL replay

        assert h.gw.placed == []
        assert h.status(rec) == OrderStatus.APPROVED.value
        assert h.ib_order_id(rec) is None
        assert h.executor._trades == {}
        assert h.order_manager.tracked_order_id(rec) is None
        assert h.runner._pending_orders == {}
        theirs = h.gw.foreign_trade(77)
        assert len(theirs.commissionReportEvent) == 0
        (page,) = h.alerts("order_submitted_elsewhere")
        assert page.priority == "high"
        assert page.context["recommendation_id"] == rec
        assert page.context["other_order_id"] == "77"
        assert page.context["other_client_id"] == str(REPAIR_CLIENT)
        assert page.context["path"] == path
        # Not acked and not dead-lettered: held for the slow re-probe.
        assert not h.acked(first) and not h.acked(redelivered)
        h.redis.send_to_dead_letter.assert_not_awaited()
        assert {r.kind for r in h.runner._deferred_messages.values()} == {
            "elsewhere"
        }

    @pytest.mark.parametrize(
        ("rec", "symbol_con_id", "action"),
        [(BUY_REC, AAPL, "buy"), (SELL_REC, MSFT, "sell")],
    )
    async def test_it_goes_out_once_the_other_clients_order_is_gone(
        self, session, rec, symbol_con_id, action
    ):
        """An APPROVED exit left behind would mute the position's next exit
        forever, and an APPROVED entry would hold its reservation forever."""
        h = Harness(session)
        order = h.approve(rec, symbol_con_id, action)
        h.gw.working(symbol_con_id, 77, REPAIR_CLIENT, rec,
                     action=action.upper())
        msg = await h.deliver(order)
        (record,) = h.runner._deferred_messages.values()
        slow = h.runner._elsewhere_retry_interval_seconds
        fast = h.runner._deferred_retry_interval_seconds
        assert record.due_at - asyncio.get_running_loop().time() > fast

        # Due, still there: still held, still one page.
        await h.retry_when_due()
        assert h.gw.placed == []
        assert len(h.alerts("order_submitted_elsewhere")) == 1
        assert record.attempts == 2
        assert record.due_at >= asyncio.get_running_loop().time() + slow - 1

        # A human cancelled theirs before IB's session rolled? Then it is in
        # completed history and is adopted (unchanged completed-order
        # behaviour). Here it is gone from both: ours goes out.
        h.gw.open_orders.clear()
        await h.retry_when_due()

        assert h.placed_refs() == [rec]
        assert h.status(rec) == OrderStatus.SUBMITTED.value
        assert h.acked(msg)
        assert h.runner._deferred_messages == {}
        assert rec not in h.order_manager._submitted_elsewhere_paged

    async def test_our_own_working_entry_is_adopted_as_before(self, session):
        h = Harness(session)
        order = h.approve(BUY_REC, AAPL, "buy")
        h.gw.working(AAPL, 241, OUR_CLIENT, BUY_REC)

        msg = await h.deliver(order)

        assert h.gw.placed == []
        assert h.status(BUY_REC) == OrderStatus.SUBMITTED.value
        assert h.ib_order_id(BUY_REC) == "241"
        assert h.executor._trades["241"] is h.gw.ib.wrapper.trades[
            (OUR_CLIENT, 241)
        ]
        assert h.acked(msg)
        assert h.alerts() == []

    async def test_a_completed_entry_is_adopted_as_before(self, session):
        h = Harness(session)
        order = h.approve(BUY_REC, AAPL, "buy")
        h.gw.done(AAPL, 241, OUR_CLIENT, BUY_REC, status="Filled")

        await h.deliver(order)

        assert h.gw.placed == []
        assert h.ib_order_id(BUY_REC) == "241"
        assert h.alerts("order_submitted_elsewhere") == []

    async def test_nothing_at_ib_submits_once(self, session):
        h = Harness(session)
        order = h.approve(BUY_REC, AAPL, "buy")

        msg = await h.deliver(order)

        assert h.placed_refs() == [BUY_REC]
        assert h.status(BUY_REC) == OrderStatus.SUBMITTED.value
        assert h.acked(msg)

    async def test_an_unanswered_probe_submits_nothing_and_retries(
        self, session
    ):
        h = Harness(session)
        order = h.approve(BUY_REC, AAPL, "buy")
        h.gw.answers_open = False
        open_bound, completed_bound = _short_bounds()
        interval = h.runner._deferred_retry_interval_seconds

        with open_bound, completed_bound:
            before = asyncio.get_running_loop().time()
            msg = await h.deliver(order)

            # Scheduled from when the attempt FINISHED: the probe's bound is
            # not taken out of the interval.
            (record,) = h.runner._deferred_messages.values()
            assert record.due_at >= before + SHORT_BOUND + interval
            assert h.gw.placed == []
            assert h.status(BUY_REC) == OrderStatus.APPROVED.value
            assert not h.acked(msg)
            h.redis.send_to_dead_letter.assert_not_awaited()
            assert list(h.runner._deferred_messages) == [msg.message_id]
            due = h.due()

            # Not before its interval.
            assert await h.runner.maybe_retry_deferred_orders(due - 1) is False
            # IB still silent: still nothing placed, still queued.
            assert await h.runner.maybe_retry_deferred_orders(due) is True
            assert h.gw.placed == []
            assert list(h.runner._deferred_messages) == [msg.message_id]
            assert not h.acked(msg)

            h.gw.answers_open = True
            await h.retry_when_due()

        assert h.placed_refs() == [BUY_REC]
        assert h.status(BUY_REC) == OrderStatus.SUBMITTED.value
        assert h.acked(msg)
        assert h.runner._deferred_messages == {}
        # An entry waits with a warning; only exits page.
        assert h.alerts() == []

    async def test_an_unanswered_exit_pages_once_and_is_retried(self, session):
        h = Harness(session)
        order = h.approve(SELL_REC, MSFT, "sell")
        h.gw.answers_completed = False
        open_bound, completed_bound = _short_bounds()

        with open_bound, completed_bound:
            msg = await h.deliver(order)
            await h.retry_when_due()

            assert h.gw.placed == []
            assert h.status(SELL_REC) == OrderStatus.APPROVED.value
            (page,) = h.alerts("exit_submission_deferred")
            assert page.priority == "high"
            assert page.context["recommendation_id"] == SELL_REC

            h.gw.answers_completed = True
            await h.retry_when_due()

        assert h.placed_refs() == [SELL_REC]
        assert h.status(SELL_REC) == OrderStatus.SUBMITTED.value
        assert h.acked(msg)
        assert len(h.alerts("exit_submission_deferred")) == 1

    async def test_a_retry_pass_stops_at_the_first_order_ib_still_ignores(
        self, session
    ):
        h = Harness(session)
        first = h.approve(BUY_REC, AAPL, "buy")
        second = h.approve(SELL_REC, MSFT, "sell")
        h.gw.answers_open = False
        open_bound, completed_bound = _short_bounds()

        with open_bound, completed_bound:
            await h.deliver(first)
            await h.deliver(second)
            requests = h.gw.open_requests
            await h.retry_when_due()

        assert h.gw.open_requests == requests + 1
        assert len(h.runner._deferred_messages) == 2

    async def test_a_startup_replay_is_deferred_not_dead_lettered(self, session):
        h = Harness(session)
        order = h.approve(BUY_REC, AAPL, "buy")
        msg = h.message(order)
        h.redis.drain_pending = AsyncMock(side_effect=[[msg], []])
        h.gw.answers_open = False
        open_bound, completed_bound = _short_bounds()

        with open_bound, completed_bound:
            await h.runner.setup()

            assert h.gw.placed == []
            assert not h.acked(msg)
            h.redis.send_to_dead_letter.assert_not_awaited()
            assert list(h.runner._deferred_messages) == [msg.message_id]

            h.gw.answers_open = True
            await h.retry_when_due()

        assert h.placed_refs() == [BUY_REC]
        assert h.acked(msg)

    async def test_the_main_loop_runs_the_retry(self, session):
        h = Harness(session)
        order = h.approve(BUY_REC, AAPL, "buy")
        h.gw.answers_open = False
        open_bound, completed_bound = _short_bounds()
        h.runner._deferred_retry_interval_seconds = 0.0

        with open_bound, completed_bound:
            await h.deliver(order)
            h.gw.answers_open = True
            h.redis.read_group = AsyncMock(return_value=[])

            steps: list[str] = []

            async def read(stream, *args, **kwargs):
                steps.append(stream)
                if stream == "stream:kill":
                    h.runner._running = False
                return []

            real_retry = h.runner.maybe_retry_deferred_orders

            async def retry(now):
                steps.append("retry")
                return await real_retry(now)

            h.redis.read_group = AsyncMock(side_effect=read)
            with patch.object(h.runner, "setup", AsyncMock()), patch.object(
                h.runner, "shutdown", AsyncMock()
            ), patch.object(
                h.runner, "maybe_retry_deferred_orders", retry
            ), patch("services.execution.runner.write_heartbeat"):
                await h.runner.run()

        assert h.placed_refs() == [BUY_REC]
        # The kill stream is read before a retry can spend its bound.
        assert steps == [APPROVED_ORDERS_STREAM, "stream:kill", "retry"]


class TestStopPath:
    async def _cover(self, h: Harness):
        return await h.runner._broker_stops.ensure_coverage(
            account_id=ACCOUNT, portfolio=PORTFOLIO, con_id=AAPL[1],
            symbol="AAPL", exchange="SMART", currency="USD",
            quantity=21, reference_price=220.65,
        )

    async def test_another_clients_stop_blocks_it_and_stays_coverage(
        self, session
    ):
        h = Harness(session, broker_stops=True)
        h.gw.working(AAPL, 77, REPAIR_CLIENT, STOP_REC, action="SELL",
                     quantity=21.0, order_type="STP")

        assert await self._cover(h) is None
        # The verification scan re-drives unsubmitted stops; still blocked.
        assert await h.runner._broker_stops._resume_unsubmitted_stops() == []

        assert h.gw.placed == []
        assert h.status(STOP_REC) == OrderStatus.APPROVED.value
        assert h.ledger.open_stop_quantity(ACCOUNT, PORTFOLIO, AAPL[1]) == 21
        h.session.rollback()
        assert h.executor._trades == {}
        (page,) = h.alerts("order_submitted_elsewhere")
        assert page.context["recommendation_id"] == STOP_REC
        assert page.context["other_client_id"] == str(REPAIR_CLIENT)
        assert page.context["path"] == "stop"

    async def test_an_unanswered_probe_pages_and_the_next_scan_places_it(
        self, session
    ):
        h = Harness(session, broker_stops=True)
        h.gw.answers_open = False
        open_bound, completed_bound = _short_bounds()

        with open_bound, completed_bound:
            assert await self._cover(h) is None

            assert h.gw.placed == []
            assert h.status(STOP_REC) == OrderStatus.APPROVED.value
            (page,) = h.alerts("broker_stop_not_placed")
            assert "retried on the next verification scan" in page.message

            h.gw.answers_open = True
            resumed = await h.runner._broker_stops._resume_unsubmitted_stops()

        assert resumed == [STOP_REC]
        assert h.placed_refs() == [STOP_REC]
        assert h.status(STOP_REC) == OrderStatus.SUBMITTED.value


class TestKillPath:
    def _kill(self) -> KillMessage:
        return KillMessage(
            timestamp=datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc),
            triggered_by="risk_management",
            reason="drawdown breach",
        )

    def _exit_id(self, ticker: str) -> str:
        return liquidation_exit_id(
            "paper", ticker, int(self._kill().timestamp.timestamp())
        )

    def _hold(self, h: Harness, *holdings) -> None:
        """Open positions in a managed sleeve (one with ledgered intents),
        which is what the kill reads before selling."""
        for (symbol, con_id), quantity in holdings:
            h.approve(f"entry-{symbol}", (symbol, con_id), "buy", quantity)
            h.session.add(Position(
                account_id=ACCOUNT, ticker=symbol, portfolio=PORTFOLIO,
                con_id=con_id, exchange="SMART", currency="USD",
                quantity=quantity, avg_entry_price=100.0, current_price=100.0,
                peak_price=100.0, highest_price_since_entry=100.0,
                opened_at=SEEDED_AT, status="open",
            ))
        h.session.commit()

    async def test_a_kill_still_liquidates_when_ib_does_not_answer(
        self, session
    ):
        """process_kill cancels the position's stop and then sells, once. A
        probe IB never answers must not leave it unprotected and unsold."""
        h = Harness(session)
        self._hold(h, (AAPL, 10.0))
        h.gw.answers_open = False
        open_bound, completed_bound = _short_bounds()

        with open_bound, completed_bound:
            await h.runner.process_kill(self._kill())

        assert h.placed_refs() == [self._exit_id("AAPL")]
        (kill_alert,) = h.alerts("kill_switch_liquidation")
        assert kill_alert.context["positions_liquidated"] == 1

    async def test_a_kill_never_sells_twice_over_another_clients_order(
        self, session
    ):
        h = Harness(session)
        self._hold(h, (AAPL, 10.0), (MSFT, 5.0))
        h.gw.working(AAPL, 77, REPAIR_CLIENT, self._exit_id("AAPL"),
                     action="SELL", order_type="MKT")

        await h.runner.process_kill(self._kill())

        assert h.placed_refs() == [self._exit_id("MSFT")]
        (page,) = h.alerts("order_submitted_elsewhere")
        assert page.context["path"] == "kill_exit"
        assert page.context["other_order_id"] == "77"
        (kill_alert,) = h.alerts("kill_switch_liquidation")
        assert kill_alert.context["positions_liquidated"] == 1

    async def test_only_the_kill_submits_unanswered_not_the_approved_path(
        self, session
    ):
        """The same liquidation id arriving as a risk-side approved sell is
        retried like any other exit: that path has a retry, the kill does
        not."""
        h = Harness(session)
        order = h.approve(self._exit_id("MSFT"), MSFT, "sell")
        h.gw.answers_open = False
        open_bound, completed_bound = _short_bounds()

        with open_bound, completed_bound:
            msg = await h.deliver(order)

        assert h.gw.placed == []
        assert not h.acked(msg)
        assert list(h.runner._deferred_messages) == [msg.message_id]


class TestOrderManagerContract:
    async def test_every_submit_path_raises_instead_of_submitting(self):
        placement = OrderPlacedElsewhere(
            recommendation_id="r", order_id="77", client_id=REPAIR_CLIENT,
            ticker="AAPL", action="SELL", status="Submitted", quantity=1.0,
        )
        executor = AsyncMock()
        executor.find_order_by_ref = AsyncMock(return_value=placement)
        page = AsyncMock()
        manager = OrderManager(executor, AsyncMock(), MagicMock())
        manager.set_submitted_elsewhere_handler(page)

        with pytest.raises(OrderPlacedElsewhereError):
            await manager.submit_entry("AAPL", 1, 100.0, "r")
        with pytest.raises(OrderPlacedElsewhereError):
            await manager.submit_exit("AAPL", 1, "r")
        with pytest.raises(OrderPlacedElsewhereError):
            await manager.submit_exit("AAPL", 1, "r", kill=KillProbe())
        with pytest.raises(OrderPlacedElsewhereError):
            await manager.submit_stop("AAPL", 1, 90.0, "r")

        executor.submit_limit_order.assert_not_awaited()
        executor.submit_market_order.assert_not_awaited()
        executor.submit_stop_order.assert_not_awaited()
        page.assert_awaited_once_with(placement, "entry")
        assert manager.open_orders == {}
        assert await manager.find_stop_order("r") is None

    async def test_unanswered_defers_every_path_but_the_kill(self):
        executor = AsyncMock()
        executor.find_order_by_ref = AsyncMock(
            side_effect=BrokerStateUnavailableError("timed out")
        )
        executor.submit_market_order = AsyncMock(return_value="900")
        manager = OrderManager(executor, AsyncMock(), MagicMock())

        with pytest.raises(SubmissionDeferredError):
            await manager.submit_entry("AAPL", 1, 100.0, "r1")
        with pytest.raises(SubmissionDeferredError):
            await manager.submit_exit("AAPL", 1, "r2")
        with pytest.raises(SubmissionDeferredError):
            await manager.submit_stop("AAPL", 1, 90.0, "r3")
        assert await manager.submit_exit(
            "AAPL", 1, "r4", kill=KillProbe()
        ) == "900"

        executor.submit_limit_order.assert_not_awaited()
        executor.submit_stop_order.assert_not_awaited()
        executor.submit_market_order.assert_awaited_once()

    async def test_a_failed_page_is_retried_next_time(self):
        placement = OrderPlacedElsewhere(
            recommendation_id="r", order_id="77", client_id=REPAIR_CLIENT,
            ticker="AAPL", action="BUY", status="Submitted", quantity=1.0,
        )
        executor = AsyncMock()
        executor.find_order_by_ref = AsyncMock(return_value=placement)
        page = AsyncMock(side_effect=[ConnectionError("redis"), None, None])
        manager = OrderManager(executor, AsyncMock(), MagicMock())
        manager._logger = MagicMock()
        manager.set_submitted_elsewhere_handler(page)

        for _ in range(3):
            with pytest.raises(OrderPlacedElsewhereError):
                await manager.submit_entry("AAPL", 1, 100.0, "r")

        assert page.await_count == 2  # failed, then delivered; never again



# --------------------------------------------------------------------------
# Review follow-ups (PR #237)
# --------------------------------------------------------------------------


class TestADisconnectedGatewayDefersInsteadOfDropping:
    """The commonest outage: the socket is gone and the reconnect fails. The
    probe's NotConnectedError used to reach the generic handler, which
    terminalized the intent and acked the message — the order was dropped."""

    async def test_the_probe_reports_it_as_no_answer(self):
        gw = Gateway()
        executor = _executor(gw)
        gw.go_down(executor)

        with pytest.raises(BrokerStateUnavailableError):
            await executor.find_order_by_ref(BUY_REC)

        assert gw.open_requests == 0

    async def test_a_wrong_account_session_still_stops_the_service(self):
        gw = Gateway()
        executor = _executor(gw)
        gw.ib.isConnected = lambda: False
        executor.connect = AsyncMock(side_effect=WrongAccountTypeError("live"))

        with pytest.raises(WrongAccountTypeError):
            await executor.find_order_by_ref(BUY_REC)

    @pytest.mark.parametrize(
        ("rec", "symbol_con_id", "action"),
        [(BUY_REC, AAPL, "buy"), (SELL_REC, MSFT, "sell")],
    )
    async def test_entry_and_exit_are_held_and_go_out_on_reconnect(
        self, session, rec, symbol_con_id, action
    ):
        h = Harness(session)
        order = h.approve(rec, symbol_con_id, action)
        h.gw.go_down(h.executor)

        msg = await h.deliver(order)

        assert h.gw.placed == []
        assert h.status(rec) == OrderStatus.APPROVED.value
        assert not h.acked(msg)
        h.redis.send_to_dead_letter.assert_not_awaited()
        assert list(h.runner._deferred_messages) == [msg.message_id]

        h.gw.come_back()
        await h.retry_when_due()

        assert h.placed_refs() == [rec]
        assert h.status(rec) == OrderStatus.SUBMITTED.value
        assert h.acked(msg)

    async def test_a_stop_stays_approved_and_the_next_scan_places_it(
        self, session
    ):
        h = Harness(session, broker_stops=True)
        h.gw.go_down(h.executor)

        assert await TestStopPath()._cover(h) is None

        assert h.gw.placed == []
        assert h.status(STOP_REC) == OrderStatus.APPROVED.value
        assert len(h.alerts("broker_stop_not_placed")) == 1

        h.gw.come_back()
        assert await h.runner._broker_stops._resume_unsubmitted_stops() == [
            STOP_REC
        ]
        assert h.placed_refs() == [STOP_REC]


class TestAKillAgainstAHungGateway:
    def _kill(self) -> KillMessage:
        return TestKillPath()._kill()

    def _exit_id(self, ticker: str) -> str:
        return TestKillPath()._exit_id(ticker)

    async def test_only_the_first_sell_waits_on_the_probe(self, session):
        h = Harness(session)
        TestKillPath()._hold(h, (AAPL, 10.0), (MSFT, 5.0), (("NVDA", 4815), 3.0))
        h.gw.answers_open = False
        open_bound, completed_bound = _short_bounds()

        with open_bound, completed_bound:
            await h.runner.process_kill(self._kill())

        assert h.gw.open_requests == 1
        assert sorted(h.placed_refs()) == sorted(
            self._exit_id(t) for t in ("AAPL", "MSFT", "NVDA")
        )

    async def test_a_sell_this_session_already_holds_is_not_placed_again(
        self, session
    ):
        """Blind only after the own-client cache: connect's open-order sync
        cached our earlier sell for this liquidation; IB now does not answer."""
        h = Harness(session)
        TestKillPath()._hold(h, (AAPL, 10.0), (MSFT, 5.0))
        h.gw.cache_own(AAPL, 900, self._exit_id("AAPL"), action="SELL",
                       order_type="MKT")
        h.gw.answers_open = False
        open_bound, completed_bound = _short_bounds()

        with open_bound, completed_bound:
            await h.runner.process_kill(self._kill())

        assert h.placed_refs() == [self._exit_id("MSFT")]
        assert h.order_manager.tracked_order_id(self._exit_id("AAPL")) == "900"
        assert "900" in h.executor._trades

    async def test_the_cache_is_checked_on_the_first_unanswered_probe_too(
        self, session
    ):
        h = Harness(session)
        TestKillPath()._hold(h, (AAPL, 10.0))
        h.gw.cache_own(AAPL, 900, self._exit_id("AAPL"), action="SELL",
                       order_type="MKT")
        h.gw.answers_open = False
        open_bound, completed_bound = _short_bounds()

        with open_bound, completed_bound:
            await h.runner.process_kill(self._kill())

        assert h.gw.open_requests == 1
        assert h.gw.placed == []

    async def test_each_kill_probes_afresh(self, session):
        h = Harness(session)
        manager = h.order_manager
        h.gw.answers_open = False
        open_bound, completed_bound = _short_bounds()

        with open_bound, completed_bound:
            await manager.submit_exit("AAPL", 1, "liq-a", kill=KillProbe())
            await manager.submit_exit("MSFT", 1, "liq-b", kill=KillProbe())

        assert h.gw.open_requests == 2


class TestTheDeferredQueueNeverLosesAMessage:
    async def test_an_unreadable_halt_latch_keeps_it_queued(self, session):
        h = Harness(session)
        order = h.approve(BUY_REC, AAPL, "buy")
        h.gw.answers_open = False
        open_bound, completed_bound = _short_bounds()

        with open_bound, completed_bound:
            msg = await h.deliver(order)
        h.gw.answers_open = True
        with patch.object(
            h.runner, "_load_active_halt",
            AsyncMock(side_effect=HaltStateUnavailable("db down")),
        ):
            await h.retry_when_due()

        assert list(h.runner._deferred_messages) == [msg.message_id]
        assert not h.acked(msg)
        assert len(h.alerts("halt_state_unavailable")) == 1

        await h.retry_when_due()

        assert h.placed_refs() == [BUY_REC]
        assert h.acked(msg)
        assert h.runner._deferred_messages == {}

    async def test_a_cancellation_mid_pass_keeps_it_queued(self, session):
        h = Harness(session)
        order = h.approve(BUY_REC, AAPL, "buy")
        h.gw.go_down(h.executor)
        msg = await h.deliver(order)

        with patch.object(
            h.runner, "_handle_message",
            AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with pytest.raises(asyncio.CancelledError):
                await h.retry_when_due()

        assert list(h.runner._deferred_messages) == [msg.message_id]

    async def test_the_warning_is_throttled(self, session):
        h = Harness(session)
        order = h.approve(BUY_REC, AAPL, "buy")
        h.gw.go_down(h.executor)
        h.runner._logger = MagicMock()

        await h.deliver(order)
        await h.retry_when_due()
        await h.retry_when_due()

        warnings = [
            c for c in h.runner._logger.warning.call_args_list
            if "left unacked" in c.args[0]
        ]
        assert len(warnings) == 1
        (record,) = h.runner._deferred_messages.values()
        assert record.attempts == 3


class TestADeferredBuyExpiresWithItsSession:
    async def test_after_the_close_it_is_failed_and_acked_never_placed(
        self, session
    ):
        h = Harness(session)
        order = h.approve(BUY_REC, AAPL, "buy")
        h.gw.go_down(h.executor)
        msg = await h.deliver(order)

        h.gw.come_back()
        h.now = SESSION_CLOSE + timedelta(minutes=1)
        await h.retry_when_due()

        assert h.gw.placed == []
        intent = h.ledger.get(BUY_REC)
        assert intent.status == OrderStatus.SUBMISSION_FAILED.value
        assert intent.reason == DEFERRED_PAST_SESSION_REASON
        h.session.rollback()
        assert h.acked(msg)
        assert h.runner._deferred_messages == {}

    async def test_a_buy_replayed_at_startup_after_the_close_is_never_placed(
        self, session
    ):
        """Left unacked before a restart, replayed after its session closed,
        with IB answering: the replay path applies the same age test."""
        h = Harness(session)
        order = h.approve(BUY_REC, AAPL, "buy")
        msg = h.message(order)
        h.redis.drain_pending = AsyncMock(side_effect=[[msg], []])
        h.now = SESSION_CLOSE + timedelta(hours=8)

        await h.runner.setup()

        assert h.gw.placed == []
        assert h.gw.open_requests == 0
        intent = h.ledger.get(BUY_REC)
        assert intent.status == OrderStatus.SUBMISSION_FAILED.value
        assert intent.reason == DEFERRED_PAST_SESSION_REASON
        h.session.rollback()
        assert h.acked(msg)
        h.redis.send_to_dead_letter.assert_not_awaited()
        assert h.runner._deferred_messages == {}

    async def test_a_buy_replayed_within_its_session_is_placed(self, session):
        h = Harness(session)
        order = h.approve(BUY_REC, AAPL, "buy")
        msg = h.message(order)
        h.redis.drain_pending = AsyncMock(side_effect=[[msg], []])

        await h.runner.setup()

        assert h.placed_refs() == [BUY_REC]
        assert h.acked(msg)

    async def test_an_exit_replayed_days_later_is_still_placed(self, session):
        h = Harness(session)
        order = h.approve(SELL_REC, MSFT, "sell")
        msg = h.message(order)
        h.redis.drain_pending = AsyncMock(side_effect=[[msg], []])
        h.now = SESSION_CLOSE + timedelta(days=3)

        await h.runner.setup()

        assert h.placed_refs() == [SELL_REC]
        assert h.acked(msg)

    async def test_before_the_close_it_is_still_placed(self, session):
        h = Harness(session)
        order = h.approve(BUY_REC, AAPL, "buy")
        h.gw.go_down(h.executor)
        await h.deliver(order)

        h.gw.come_back()
        h.now = SESSION_CLOSE - timedelta(minutes=1)
        await h.retry_when_due()

        assert h.placed_refs() == [BUY_REC]

    async def test_an_exit_never_expires(self, session):
        h = Harness(session)
        order = h.approve(SELL_REC, MSFT, "sell")
        h.gw.go_down(h.executor)
        await h.deliver(order)

        h.gw.come_back()
        h.now = SESSION_CLOSE + timedelta(days=3)
        await h.retry_when_due()

        assert h.placed_refs() == [SELL_REC]

    async def test_a_buy_approved_after_the_close_is_sized_for_the_next_day(
        self, session
    ):
        h = Harness(session)
        after_close = SESSION_CLOSE + timedelta(minutes=15)  # 16:15 ET Thu

        close = h.runner._session_close_for(after_close)

        assert close == datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc)


class TestOnlyAWorkingForeignOrderBlocks:
    @pytest.mark.parametrize("status", ["Inactive", "PendingCancel"])
    async def test_a_parked_or_dying_one_does_not(self, session, status):
        h = Harness(session)
        order = h.approve(BUY_REC, AAPL, "buy")
        h.gw.working(AAPL, 77, REPAIR_CLIENT, BUY_REC, status=status)

        await h.deliver(order)

        assert h.placed_refs() == [BUY_REC]
        assert h.alerts("order_submitted_elsewhere") == []

    @pytest.mark.parametrize(
        "status", ["PendingSubmit", "ApiPending", "PreSubmitted", "Submitted"]
    )
    async def test_a_working_one_does(self, status):
        gw = Gateway()
        gw.working(AAPL, 77, REPAIR_CLIENT, BUY_REC, status=status)

        found = await _executor(gw).find_order_by_ref(BUY_REC)

        assert isinstance(found, OrderPlacedElsewhere)


class TestPagingEpisodes:
    async def test_a_failed_withheld_exit_page_is_retried(self, session):
        h = Harness(session)
        order = h.approve(SELL_REC, MSFT, "sell")
        h.gw.go_down(h.executor)
        real_publish = h.redis.publish
        h.redis.publish = AsyncMock(side_effect=ConnectionError("redis"))

        await h.deliver(order)
        assert SELL_REC not in h.runner._deferred_exit_paged

        h.redis.publish = real_publish
        await h.retry_when_due()
        await h.retry_when_due()

        assert len(h.alerts("exit_submission_deferred")) == 1
        assert SELL_REC in h.runner._deferred_exit_paged

    async def test_both_paged_sets_are_pruned_when_it_resolves(self, session):
        h = Harness(session)
        order = h.approve(SELL_REC, MSFT, "sell")
        h.gw.go_down(h.executor)
        await h.deliver(order)
        h.gw.come_back()
        h.gw.working(MSFT, 77, REPAIR_CLIENT, SELL_REC, action="SELL")
        await h.retry_when_due()
        assert SELL_REC in h.runner._deferred_exit_paged
        assert SELL_REC in h.order_manager._submitted_elsewhere_paged

        h.gw.open_orders.clear()
        await h.retry_when_due()

        assert h.placed_refs() == [SELL_REC]
        assert SELL_REC not in h.runner._deferred_exit_paged
        assert SELL_REC not in h.order_manager._submitted_elsewhere_paged
