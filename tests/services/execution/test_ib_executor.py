from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.execution.ib_executor import IBExecutor

EXECUTED_AT = datetime(2026, 7, 24, 8, 30, tzinfo=timezone.utc)


class Event:
    def __init__(self) -> None:
        self.callbacks = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self

    def emit(self, *args) -> None:
        for callback in self.callbacks:
            callback(*args)


def make_account_value(
    value: object,
    *,
    tag: str = "ExchangeRate",
    currency: str = "USD",
):
    return SimpleNamespace(tag=tag, currency=currency, value=value)


async def capture_fill_payload(
    *,
    commission: float,
    commission_currency: str,
    account_values: list[object] | None,
) -> dict[str, object]:
    executor = IBExecutor("h", 7497, 1)
    executor._ib = MagicMock()
    executor._ib.accountValues.return_value = account_values
    handler = AsyncMock()
    executor.set_fill_handler(handler)

    trade = MagicMock()
    trade.fillEvent = Event()
    trade.commissionReportEvent = Event()
    trade.statusEvent = Event()
    trade.isDone.return_value = False
    fill = SimpleNamespace(
        execution=SimpleNamespace(
            execId="exec-1",
            acctNumber="DU12345",
            shares=2,
            cumQty=2,
            price=100,
            time=EXECUTED_AT,
        ),
        contract=SimpleNamespace(
            conId=265598,
            exchange="SMART",
            currency="USD",
        ),
        commissionReport=SimpleNamespace(
            commission=0.0,
            currency="",
        ),
    )
    commission_report = SimpleNamespace(
        commission=commission,
        currency=commission_currency,
    )

    executor._register_trade("9", trade, ticker="AAPL", side="buy")
    trade.fillEvent.emit(trade, fill)
    await asyncio.sleep(0)
    assert handler.await_count == 0

    trade.commissionReportEvent.emit(trade, fill, commission_report)
    await asyncio.sleep(0)
    return handler.await_args.args[0]


@pytest.mark.asyncio
async def test_usd_commission_maps_one_to_one_without_fx_rate():
    payload = await capture_fill_payload(
        commission=1.25,
        commission_currency="USD",
        account_values=[],
    )

    assert payload["commission"] == pytest.approx(1.25)
    assert payload["commission_currency"] == "USD"
    assert payload["commission_trading"] == pytest.approx(1.25)
    assert payload["commission_fx_base_per_trading"] is None
    assert payload["timestamp"] == EXECUTED_AT


@pytest.mark.asyncio
async def test_sgd_commission_uses_single_positive_usd_exchange_rate():
    payload = await capture_fill_payload(
        commission=1.25,
        commission_currency="SGD",
        account_values=[make_account_value("1.25")],
    )

    assert payload["commission"] == pytest.approx(1.25)
    assert payload["commission_currency"] == "SGD"
    assert payload["commission_trading"] == pytest.approx(1.0)
    assert payload["commission_fx_base_per_trading"] == pytest.approx(1.25)


@pytest.mark.parametrize(
    "account_values",
    [
        [],
        None,
        [make_account_value("1.25"), make_account_value("1.26")],
        [make_account_value("not-a-number")],
        [make_account_value("nan")],
        [make_account_value("inf")],
        [make_account_value("0")],
        [make_account_value("-1")],
    ],
)
@pytest.mark.asyncio
async def test_sgd_commission_fails_closed_without_one_valid_rate(
    account_values,
):
    payload = await capture_fill_payload(
        commission=1.25,
        commission_currency="SGD",
        account_values=account_values,
    )

    assert payload["commission_trading"] is None
    assert payload["commission_fx_base_per_trading"] is None


@pytest.mark.asyncio
async def test_unsupported_commission_currency_is_not_translated():
    payload = await capture_fill_payload(
        commission=1.25,
        commission_currency="EUR",
        account_values=[make_account_value("1.25")],
    )

    assert payload["commission_currency"] == "EUR"
    assert payload["commission_trading"] is None
    assert payload["commission_fx_base_per_trading"] is None


@pytest.mark.asyncio
async def test_duplicate_authoritative_commission_delivery_replays_same_fill():
    executor = IBExecutor("h", 7497, 1)
    executor._ib = MagicMock()
    executor._ib.accountValues.return_value = []
    handler = AsyncMock()
    executor.set_fill_handler(handler)
    trade = MagicMock()
    trade.fillEvent = Event()
    trade.commissionReportEvent = Event()
    trade.statusEvent = Event()
    trade.isDone.return_value = True
    fill = SimpleNamespace(
        execution=SimpleNamespace(
            execId="exec-duplicate",
            acctNumber="DU12345",
            shares=2,
            cumQty=2,
            price=100,
            time=EXECUTED_AT,
        ),
        contract=SimpleNamespace(
            conId=265598,
            exchange="SMART",
            currency="USD",
        ),
        commissionReport=SimpleNamespace(commission=0.0, currency=""),
    )
    report = SimpleNamespace(commission=1.25, currency="USD")
    executor._register_trade("9", trade, ticker="AAPL", side="buy")

    trade.fillEvent.emit(trade, fill)
    trade.commissionReportEvent.emit(trade, fill, report)
    trade.commissionReportEvent.emit(trade, fill, report)
    await asyncio.sleep(0)

    assert handler.await_count == 2
    first, second = [call.args[0] for call in handler.await_args_list]
    assert first == second
    assert first["execution_id"] == "exec-duplicate"
    assert first["timestamp"] == EXECUTED_AT


@pytest.mark.asyncio
async def test_spawn_routes_task_exception_to_logger():
    """Fill/status callbacks are fire-and-forget; a failing one must be logged,
    not silently swallowed by the event loop's default handler."""
    executor = IBExecutor("h", 7497, 1)
    executor._logger = MagicMock()

    async def boom():
        raise RuntimeError("callback blew up")

    task = executor._spawn(boom())
    try:
        await task
    except RuntimeError:
        pass
    # done-callback ran and logged the failure.
    assert executor._logger.error.called or executor._logger.exception.called


@pytest.mark.asyncio
async def test_reconnect_reregisters_callbacks_for_open_trades():
    """A mid-session reconnect recreates the IB client; callbacks bound to the
    old Trade objects must be re-registered onto the fresh ones or fills/status
    for in-flight orders are silently lost."""
    from unittest.mock import patch

    executor = IBExecutor("h", 7497, 1)
    executor.set_fill_handler(AsyncMock())
    executor.set_order_status_handler(AsyncMock())

    old_trade = SimpleNamespace(
        order=SimpleNamespace(orderId=9),
        commissionReportEvent=Event(),
        statusEvent=Event(),
    )
    executor._register_trade("9", old_trade, "AAPL", "buy")

    new_trade = SimpleNamespace(
        order=SimpleNamespace(orderId=9),
        orderStatus=SimpleNamespace(status="Submitted", filled=0),
        commissionReportEvent=Event(),
        statusEvent=Event(),
    )
    fake_ib = MagicMock()
    fake_ib.connectAsync = AsyncMock()
    fake_ib.managedAccounts.return_value = ["DUN551088"]
    fake_ib.openTrades.return_value = [new_trade]

    with patch("ib_insync.IB", return_value=fake_ib):
        await executor.connect(expect_paper=True)  # simulates the reconnect

    assert len(new_trade.statusEvent.callbacks) == 1
    assert len(new_trade.commissionReportEvent.callbacks) == 1
    assert executor._trades["9"] is new_trade


@pytest.mark.asyncio
async def test_reconnect_warns_about_orders_absent_after_outage():
    """An order that completed during the outage is no longer open at IB; its
    callbacks can't be replayed, so the reconnect must surface it (warning) for
    reconciliation instead of silently diverging."""
    from unittest.mock import patch

    executor = IBExecutor("h", 7497, 1)
    executor.set_fill_handler(AsyncMock())
    executor.set_order_status_handler(AsyncMock())
    executor._logger = MagicMock()

    old_trade = SimpleNamespace(
        order=SimpleNamespace(orderId=9),
        commissionReportEvent=Event(),
        statusEvent=Event(),
    )
    executor._register_trade("9", old_trade, "AAPL", "buy")

    fake_ib = MagicMock()
    fake_ib.connectAsync = AsyncMock()
    fake_ib.managedAccounts.return_value = ["DUN551088"]
    fake_ib.openTrades.return_value = []  # order 9 completed during the outage

    with patch("ib_insync.IB", return_value=fake_ib):
        await executor.connect(expect_paper=True)

    assert executor._logger.warning.called
    warned_ids = executor._logger.warning.call_args.kwargs.get("order_ids")
    assert warned_ids == ["9"]


def make_open_trade(
    order_id: int = 77,
    *,
    order_ref: str = "rec-1",
    action: str = "BUY",
    symbol: str = "AAPL",
    quantity: float = 50.0,
    account: str | None = "DUN551088",
):
    return SimpleNamespace(
        order=SimpleNamespace(
            orderId=order_id,
            orderRef=order_ref,
            action=action,
            totalQuantity=quantity,
            account=account,
        ),
        contract=SimpleNamespace(symbol=symbol),
        commissionReportEvent=Event(),
        statusEvent=Event(),
    )


@pytest.mark.asyncio
async def test_list_open_orders_exposes_the_order_ref():
    """KAN-13: the post-halt sweep identifies a raced order only by its
    orderRef, which the account snapshot's BrokerOpenOrder does not carry."""
    executor = IBExecutor("h", 7497, 1)
    executor._ib = MagicMock()
    executor._ib.openTrades.return_value = [make_open_trade()]

    orders = await executor.list_open_orders()

    assert [(o.order_id, o.order_ref, o.action, o.ticker) for o in orders] == [
        ("77", "rec-1", "BUY", "AAPL")
    ]
    assert orders[0].quantity == 50.0
    assert orders[0].account_id == "DUN551088"


@pytest.mark.asyncio
async def test_list_open_orders_does_not_bind_callbacks_to_foreign_orders():
    """An order returned here may belong to another client id or a manual TWS
    session; wiring our fill/status handlers onto it would corrupt attribution."""
    executor = IBExecutor("h", 7497, 1)
    executor.set_fill_handler(AsyncMock())
    executor.set_order_status_handler(AsyncMock())
    executor._ib = MagicMock()
    trade = make_open_trade(order_id=1234, order_ref="manual-tws")
    executor._ib.openTrades.return_value = [trade]

    await executor.list_open_orders()

    assert executor._trades == {}
    assert trade.statusEvent.callbacks == []
    assert trade.commissionReportEvent.callbacks == []


@pytest.mark.asyncio
async def test_cancel_broker_order_cancels_an_untracked_live_order():
    """After a restart _trades is empty while the order is still working at
    IB — a halt-safety path cannot depend on in-process memory."""
    executor = IBExecutor("h", 7497, 1)
    executor._ib = MagicMock()
    trade = make_open_trade(order_id=77)
    executor._ib.openTrades.return_value = [trade]

    assert await executor.cancel_broker_order("77") is True

    executor._ib.cancelOrder.assert_called_once_with(trade.order)


@pytest.mark.asyncio
async def test_cancel_broker_order_reports_an_order_not_open_at_ib():
    executor = IBExecutor("h", 7497, 1)
    executor._ib = MagicMock()
    executor._ib.openTrades.return_value = []

    assert await executor.cancel_broker_order("77") is False
    executor._ib.cancelOrder.assert_not_called()


def make_position(
    *,
    con_id: int = 265598,
    quantity: float = 12.0,
    account: str = "DUN551088",
    sec_type: str = "STK",
):
    return SimpleNamespace(
        account=account,
        position=quantity,
        contract=SimpleNamespace(conId=con_id, secType=sec_type, symbol="AAPL"),
    )


def make_position_executor(positions, *, account_id="DUN551088"):
    executor = IBExecutor("h", 7497, 1, account_id=account_id)
    executor._ib = MagicMock()
    executor._ib.reqPositionsAsync = AsyncMock(return_value=positions)
    return executor


@pytest.mark.asyncio
async def test_broker_position_reports_the_held_quantity():
    """KAN-10: the broker is authoritative on what can be sold — the ledger's
    view of a position lags every fill that has not been projected yet."""
    executor = make_position_executor([make_position(quantity=12.0)])

    assert await executor.broker_position(265598) == 12.0


@pytest.mark.asyncio
async def test_broker_position_is_zero_for_a_contract_not_held():
    executor = make_position_executor([make_position(con_id=999999)])

    assert await executor.broker_position(265598) == 0.0


@pytest.mark.asyncio
async def test_broker_position_ignores_other_accounts():
    """A single Gateway session can report positions for more than one
    account; counting a foreign one would license a sell this account cannot
    cover."""
    executor = make_position_executor(
        [make_position(account="DU00000", quantity=50.0)]
    )

    assert await executor.broker_position(265598) == 0.0


@pytest.mark.asyncio
async def test_broker_position_without_a_configured_account_sums_what_ib_reports():
    """The account is pinned in production (KAN-11); with none configured the
    executor cannot filter, so it reports what the session shows rather than
    silently answering zero."""
    executor = make_position_executor(
        [make_position(account="DU00000", quantity=4.0)], account_id=None
    )

    assert await executor.broker_position(265598) == 4.0
