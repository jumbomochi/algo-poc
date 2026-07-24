from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.execution.ib_executor import IBExecutor


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
