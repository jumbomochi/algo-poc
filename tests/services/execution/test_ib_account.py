from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.execution.ib_account import AccountValidationError, IBAccountReader
from tests.services.execution.fake_ib_gateway import FakeGateway, default_rows


def _fake_ib(accounts=("DUN551088",)):
    ib = MagicMock()
    ib.managedAccounts.return_value = list(accounts)
    ib.accountSummary.side_effect = AssertionError(
        "sync accountSummary is forbidden inside the async reader"
    )
    ib.accountSummaryAsync.side_effect = AssertionError(
        "IB.accountSummaryAsync cannot be cancelled — it discards the reqId, and "
        "IB exposes no cancelAccountSummary. The reader must request through "
        "ib.client so it can release the subscription (KAN-79)."
    )
    # A gateway that enforces IB's subscription cap. `ib.gateway.rows` is the
    # summary the reader will see; tests mutate it in place.
    ib.gateway = FakeGateway(rows=default_rows(accounts[0] if accounts else ""))
    ib.client = ib.gateway.client
    ib.wrapper = ib.gateway.wrapper
    ib.errorEvent = ib.gateway.errorEvent
    contract = SimpleNamespace(
        conId=265598, symbol="AAPL", localSymbol="AAPL", exchange="SMART", currency="USD"
    )
    ib.positions.return_value = [
        SimpleNamespace(account=accounts[0] if accounts else "", contract=contract, position=10, avgCost=100)
    ]
    order = SimpleNamespace(orderId=9, action="BUY", totalQuantity=4)
    status = SimpleNamespace(status="Submitted", filled=1)
    trades = [SimpleNamespace(contract=contract, order=order, orderStatus=status)]
    ib.reqAllOpenOrders.side_effect = AssertionError("sync IB call is forbidden")
    ib.reqAllOpenOrdersAsync = AsyncMock(return_value=trades)
    ib.openTrades.return_value = trades
    return ib


@pytest.mark.asyncio
async def test_account_reader_returns_contract_keyed_snapshot():
    ib = _fake_ib()
    snapshot = await IBAccountReader(
        ib,
        expected_mode="paper",
        expected_base_currency="SGD",
        trading_currency="USD",
    ).snapshot()

    assert snapshot.account_id == "DUN551088"
    assert snapshot.mode == "paper"
    assert snapshot.base_currency == "SGD"
    assert snapshot.trading_currency == "USD"
    assert snapshot.net_liquidation_base == pytest.approx(1_001_757.23)
    assert snapshot.fx_base_per_trading == pytest.approx(1.2928304)
    assert snapshot.net_liquidation_trading_equivalent == pytest.approx(774_855.87)
    assert snapshot.settled_cash_trading == pytest.approx(-4_711.26)
    assert snapshot.fx_source == "$LEDGER:ALL/ExchangeRate"
    assert snapshot.fx_captured_at == snapshot.captured_at
    assert snapshot.captured_at.tzinfo is not None
    assert snapshot.captured_at.utcoffset().total_seconds() == 0
    assert snapshot.positions[265598].quantity == 10
    assert snapshot.open_orders["9"].remaining_quantity == 3
    # KAN-79: the subscription the snapshot opened is released before it
    # returns, so nothing accumulates on the gateway across runs.
    assert ib.gateway.requested == ib.gateway.cancelled
    assert ib.gateway.open_subscriptions == set()
    ib.accountSummary.assert_not_called()
    ib.reqAllOpenOrdersAsync.assert_awaited_once_with()
    ib.reqAllOpenOrders.assert_not_called()


@pytest.mark.asyncio
async def test_account_reader_excludes_currency_cash_positions():
    """FX currency (secType=CASH) holdings are cash, not equity-ledger positions.

    A SGD->USD conversion shows up in ib.positions() as a CASH USD.SGD position.
    It must NOT be reconciled against the durable equity book (it is already
    captured in NAV / settled cash) — otherwise funding USD to trade creates a
    'missing_in_db' discrepancy that force-disables entries (the catch-22).
    """
    ib = _fake_ib()
    stk = SimpleNamespace(
        conId=265598, symbol="AAPL", localSymbol="AAPL",
        exchange="SMART", currency="USD", secType="STK",
    )
    cash = SimpleNamespace(
        conId=37928772, symbol="USD", localSymbol="USD.SGD",
        exchange="IDEALPRO", currency="SGD", secType="CASH",
    )
    ib.positions.return_value = [
        SimpleNamespace(account="DUN551088", contract=stk, position=10, avgCost=100),
        SimpleNamespace(account="DUN551088", contract=cash, position=100000, avgCost=1.29),
    ]

    snapshot = await IBAccountReader(
        ib,
        expected_mode="paper",
        expected_base_currency="SGD",
        trading_currency="USD",
    ).snapshot()

    assert 265598 in snapshot.positions          # equity position kept
    assert 37928772 not in snapshot.positions     # FX cash position excluded
    assert len(snapshot.positions) == 1


@pytest.mark.asyncio
async def test_account_reader_requires_exactly_one_managed_account():
    with pytest.raises(AccountValidationError, match="exactly one"):
        await IBAccountReader(
            _fake_ib(("DU1", "DU2")),
            expected_mode="paper",
            expected_base_currency="SGD",
            trading_currency="USD",
        ).snapshot()


@pytest.mark.asyncio
async def test_account_reader_rejects_live_account_in_paper_mode():
    with pytest.raises(AccountValidationError, match="paper"):
        await IBAccountReader(
            _fake_ib(("U17723819",)),
            expected_mode="paper",
            expected_base_currency="SGD",
            trading_currency="USD",
        ).snapshot()


@pytest.mark.asyncio
async def test_account_reader_rejects_paper_account_in_live_mode():
    with pytest.raises(AccountValidationError, match="live"):
        await IBAccountReader(
            _fake_ib(),
            expected_mode="live",
            expected_base_currency="SGD",
            trading_currency="USD",
        ).snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tag", "row_count", "error"),
    [
        ("NetLiquidation", 0, "SGD NetLiquidation"),
        ("NetLiquidation", 2, "SGD NetLiquidation"),
        ("ExchangeRate", 0, "USD ExchangeRate"),
        ("ExchangeRate", 2, "USD ExchangeRate"),
        ("TotalCashBalance", 0, "USD TotalCashBalance"),
        ("TotalCashBalance", 2, "USD TotalCashBalance"),
    ],
)
async def test_account_reader_requires_exactly_one_currency_value(
    tag, row_count, error
):
    """Duplicates arrive under DIFFERENT account labels, never as two identical
    rows: ib_insync keys `wrapper.acctSummary` by (account, tag, currency), so
    an exact repeat overwrites rather than accumulating. `_matching_rows`
    accepts "", the account id, and (for the $LEDGER tags) "All", so two of
    those labels carrying the same tag and currency is the reachable shape —
    and the one the guard has to catch.
    """
    ib = _fake_ib()
    summary = default_rows()
    template = next(row for row in summary if row.tag == tag)
    summary = [row for row in summary if row.tag != tag]
    accepted_accounts = ["", "DUN551088", "All"]
    summary.extend(
        SimpleNamespace(**{**vars(template), "account": accepted_accounts[i]})
        for i in range(row_count)
    )
    ib.gateway.rows = summary

    with pytest.raises(AccountValidationError, match=error):
        await IBAccountReader(
            ib,
            expected_mode="paper",
            expected_base_currency="SGD",
            trading_currency="USD",
        ).snapshot()


@pytest.mark.asyncio
async def test_account_reader_selects_trading_currency_cash_among_currencies():
    # IB returns TotalCashBalance per currency (SGD/USD/BASE); the reader must
    # pick the trading-currency (USD) row and tolerate a negative balance.
    ib = _fake_ib()
    summary = default_rows()
    summary.extend(
        [
            SimpleNamespace(
                account="All", tag="TotalCashBalance", value="1001993.27", currency="SGD"
            ),
            SimpleNamespace(
                account="All", tag="TotalCashBalance", value="995912.98", currency="BASE"
            ),
        ]
    )
    ib.gateway.rows = summary

    snapshot = await IBAccountReader(
        ib,
        expected_mode="paper",
        expected_base_currency="SGD",
        trading_currency="USD",
    ).snapshot()

    assert snapshot.settled_cash_trading == pytest.approx(-4_711.26)


@pytest.mark.asyncio
async def test_account_reader_rejects_nav_in_wrong_currency():
    ib = _fake_ib()
    ib.gateway.rows[0].currency = "USD"

    with pytest.raises(AccountValidationError, match="SGD NetLiquidation"):
        await IBAccountReader(
            ib,
            expected_mode="paper",
            expected_base_currency="SGD",
            trading_currency="USD",
        ).snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tag", "value", "error"),
    [
        ("NetLiquidation", "nan", "invalid SGD NetLiquidation"),
        ("ExchangeRate", "inf", "invalid USD ExchangeRate"),
        ("TotalCashBalance", "-inf", "invalid USD TotalCashBalance"),
        ("NetLiquidation", "not-a-number", "invalid SGD NetLiquidation"),
    ],
)
async def test_account_reader_rejects_non_finite_or_invalid_values(
    tag, value, error
):
    ib = _fake_ib()
    next(
        row for row in ib.gateway.rows if row.tag == tag
    ).value = value

    with pytest.raises(AccountValidationError, match=error):
        await IBAccountReader(
            ib,
            expected_mode="paper",
            expected_base_currency="SGD",
            trading_currency="USD",
        ).snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tag", "value"),
    [
        ("NetLiquidation", "0"),
        ("NetLiquidation", "-1"),
        ("ExchangeRate", "0"),
        ("ExchangeRate", "-1"),
    ],
)
async def test_account_reader_rejects_non_positive_nav_or_fx(tag, value):
    ib = _fake_ib()
    next(
        row for row in ib.gateway.rows if row.tag == tag
    ).value = value

    with pytest.raises(AccountValidationError, match="NAV and FX rate must be positive"):
        await IBAccountReader(
            ib,
            expected_mode="paper",
            expected_base_currency="SGD",
            trading_currency="USD",
        ).snapshot()


@pytest.mark.asyncio
async def test_account_reader_rejects_derived_usd_nav_overflow():
    ib = _fake_ib()
    next(
        row
        for row in ib.gateway.rows
        if row.tag == "ExchangeRate"
    ).value = "5e-324"

    with pytest.raises(AccountValidationError, match="invalid derived USD NAV"):
        await IBAccountReader(
            ib,
            expected_mode="paper",
            expected_base_currency="SGD",
            trading_currency="USD",
        ).snapshot()


@pytest.mark.parametrize(
    ("expected_base_currency", "trading_currency"),
    [("USD", "USD"), ("SGD", "SGD")],
)
def test_account_reader_rejects_unsupported_currency_configuration(
    expected_base_currency, trading_currency
):
    with pytest.raises(ValueError, match="SGD.*USD"):
        IBAccountReader(
            _fake_ib(),
            expected_mode="paper",
            expected_base_currency=expected_base_currency,
            trading_currency=trading_currency,
        )
