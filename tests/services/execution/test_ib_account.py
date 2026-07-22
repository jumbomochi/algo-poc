from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.execution.ib_account import AccountValidationError, IBAccountReader


def _fake_ib(accounts=("DUN551088",)):
    ib = MagicMock()
    ib.managedAccounts.return_value = list(accounts)
    ib.accountSummary.return_value = [
        SimpleNamespace(account=accounts[0] if accounts else "", tag="NetLiquidation", value="1000000", currency="USD")
    ]
    contract = SimpleNamespace(
        conId=265598, symbol="AAPL", localSymbol="AAPL", exchange="SMART", currency="USD"
    )
    ib.positions.return_value = [
        SimpleNamespace(account=accounts[0] if accounts else "", contract=contract, position=10, avgCost=100)
    ]
    order = SimpleNamespace(orderId=9, action="BUY", totalQuantity=4)
    status = SimpleNamespace(status="Submitted", filled=1)
    trades = [SimpleNamespace(contract=contract, order=order, orderStatus=status)]
    ib.reqAllOpenOrders.return_value = trades
    ib.openTrades.return_value = trades
    return ib


@pytest.mark.asyncio
async def test_account_reader_returns_contract_keyed_snapshot():
    ib = _fake_ib()
    snapshot = await IBAccountReader(ib, expected_mode="paper").snapshot()

    assert snapshot.account_id == "DUN551088"
    assert snapshot.mode == "paper"
    assert snapshot.net_liquidation == 1_000_000
    assert snapshot.positions[265598].quantity == 10
    assert snapshot.open_orders["9"].remaining_quantity == 3
    ib.reqAllOpenOrders.assert_called_once_with()


@pytest.mark.asyncio
async def test_account_reader_requires_exactly_one_managed_account():
    with pytest.raises(AccountValidationError, match="exactly one"):
        await IBAccountReader(_fake_ib(("DU1", "DU2")), expected_mode="paper").snapshot()


@pytest.mark.asyncio
async def test_account_reader_rejects_live_account_in_paper_mode():
    with pytest.raises(AccountValidationError, match="paper"):
        await IBAccountReader(_fake_ib(("U17723819",)), expected_mode="paper").snapshot()


@pytest.mark.asyncio
async def test_account_reader_rejects_paper_account_in_live_mode():
    with pytest.raises(AccountValidationError, match="live"):
        await IBAccountReader(_fake_ib(), expected_mode="live").snapshot()


@pytest.mark.asyncio
async def test_account_reader_requires_net_liquidation():
    ib = _fake_ib()
    ib.accountSummary.return_value = []
    with pytest.raises(AccountValidationError, match="NetLiquidation"):
        await IBAccountReader(ib, expected_mode="paper").snapshot()
