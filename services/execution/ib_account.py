from __future__ import annotations

import asyncio
import inspect
import math
from datetime import datetime, timezone
from typing import Any

from shared.broker_state import (
    BrokerAccountSnapshot,
    BrokerOpenOrder,
    BrokerPosition,
    optional_float as _optional_float,
    optional_str as _optional_str,
)


class AccountValidationError(RuntimeError):
    pass


class AccountSummaryRefusedError(AccountValidationError):
    """IB refused the account-summary request — Error 322.

    Distinct from a malformed summary because the remedy is different: nothing
    is wrong with the data, there are too many subscriptions registered against
    this gateway session and the oldest ones belong to processes that have
    already exited. Restarting the gateway clears them.
    """


#: What the snapshot actually reads. NetLiquidation arrives in the account's
#: base currency; ExchangeRate and TotalCashBalance are per-currency rows that
#: only appear when $LEDGER:ALL is requested. ib_insync's accountSummaryAsync
#: asks for all 33 tags; this asks for the two that are read.
ACCOUNT_SUMMARY_TAGS = "NetLiquidation,$LEDGER:ALL"

#: A request IB never answers must not hang the caller. Generous — the library's
#: own comment puts a normal round trip at ~250ms.
ACCOUNT_SUMMARY_TIMEOUT_SECONDS = 30.0

#: IB's refusal code: "Maximum number of account summary requests exceeded;
#: desubscribe to previous request first".
_ERROR_ACCOUNT_SUMMARY_REFUSED = 322


async def read_account_summary(
    ib: Any, *, timeout: float = ACCOUNT_SUMMARY_TIMEOUT_SECONDS
) -> list[Any]:
    """Take one account summary and release the subscription it opened — KAN-79.

    `reqAccountSummary` opens a SUBSCRIPTION, not a one-shot query, and the
    gateway holds it for the session — it outlives the process that made it.
    Nothing in this tree ever cancelled one, so they accumulated across runs
    until IB refused new ones. On 2026-09-09 that was 69 refusals in a single
    paper run, each arriving right behind an Error 1102: on every reconnect the
    gateway replays its stored subscriptions to IBKR, and IBKR rejects each
    replay whose original registration is still live.

    The snapshot is a point-in-time read. Nothing wants a standing subscription,
    so this cancels as soon as the rows are in hand — before returning them, so
    that a caller whose extraction raises cannot leak one either.

    WHY THIS GOES THROUGH ib.client AND NOT IB.accountSummaryAsync
    --------------------------------------------------------------
    `IB` exposes no `cancelAccountSummary`, and `accountSummaryAsync` discards
    the reqId it allocated, so there is no handle to release what it opened.
    `Client.cancelAccountSummary(reqId)` does exist. Issuing the request at the
    client level is the only way to hold the reqId the cancel needs.

    (`accountSummaryAsync` also would not have leaked *per call*: it re-requests
    only when `wrapper.acctSummary` is empty. The leak is one per connection,
    which is one per run, which over weeks is enough.)
    """
    client = ib.client
    wrapper = ib.wrapper
    req_id = client.getReqId()

    refusals: list[str] = []

    def _note_refusal(err_req_id, error_code, error_string, _contract=None) -> None:
        # Only our own reqId. The refusals in the log arrive out of band against
        # reqIds owned by processes that have already exited.
        if err_req_id == req_id and error_code == _ERROR_ACCOUNT_SUMMARY_REFUSED:
            refusals.append(str(error_string))

    ib.errorEvent += _note_refusal
    refused = False
    try:
        future = wrapper.startReq(req_id)
        client.reqAccountSummary(req_id, "All", ACCOUNT_SUMMARY_TAGS)
        try:
            await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise AccountValidationError(
                f"IB did not answer the account summary request within {timeout}s"
            ) from exc

        if refusals:
            # ib_insync ends a refused request's future with NO rows, so without
            # this the caller sees "expected exactly one NetLiquidation value" —
            # a message that blames the data for a refused subscription.
            refused = True
            raise AccountSummaryRefusedError(
                f"IB refused the account summary (Error "
                f"{_ERROR_ACCOUNT_SUMMARY_REFUSED}): {refusals[0]}. Account-summary "
                f"subscriptions are held by the gateway for the whole session and "
                f"outlive the process that opened them; restarting IB Gateway "
                f"clears them."
            )

        return list(wrapper.acctSummary.values())
    finally:
        ib.errorEvent -= _note_refusal
        # A refused request registered nothing, so cancelling it would draw an
        # Error 300 ("can't find EId") and put a second misleading line in the
        # log for one event. Every other path cancels, including the timeout:
        # a request IB never answered may still have registered.
        if not refused:
            client.cancelAccountSummary(req_id)


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _matching_rows(
    rows: list[Any],
    *,
    tag: str,
    currency: str,
    account_id: str,
    allow_all: bool = False,
) -> list[Any]:
    accepted_accounts = {"", account_id}
    if allow_all:
        accepted_accounts.add("All")
    return [
        row
        for row in rows
        if str(getattr(row, "tag", "")) == tag
        and str(getattr(row, "currency", "")) == currency
        and str(getattr(row, "account", account_id)) in accepted_accounts
    ]


def _one_float(rows: list[Any], *, label: str) -> float:
    if len(rows) != 1:
        raise AccountValidationError(f"expected exactly one {label} value")
    try:
        value = float(rows[0].value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise AccountValidationError(f"invalid {label} value") from exc
    if not math.isfinite(value):
        raise AccountValidationError(f"invalid {label} value")
    return value


class IBAccountReader:
    """Read one validated IB account into contract-keyed immutable state."""

    def __init__(
        self,
        ib: Any,
        *,
        expected_mode: str,
        expected_base_currency: str,
        trading_currency: str,
    ) -> None:
        mode = expected_mode.lower()
        if mode not in {"paper", "live"}:
            raise ValueError("expected_mode must be 'paper' or 'live'")
        if expected_base_currency != "SGD" or trading_currency != "USD":
            raise ValueError(
                "account snapshots require SGD base currency and USD trading currency"
            )
        self._ib = ib
        self._expected_mode = mode
        self._expected_base_currency = expected_base_currency
        self._trading_currency = trading_currency

    async def snapshot(self) -> BrokerAccountSnapshot:
        accounts = list(await _resolve(self._ib.managedAccounts()))
        if len(accounts) != 1:
            raise AccountValidationError(
                "IB reconciliation requires exactly one managed account"
            )
        account_id = str(accounts[0])
        is_paper = account_id.startswith("DU")
        is_live = account_id.startswith("U") and not is_paper
        if self._expected_mode == "paper" and not is_paper:
            raise AccountValidationError(
                f"paper mode requires a DU account; connected to {account_id}"
            )
        if self._expected_mode == "live" and not is_live:
            raise AccountValidationError(
                f"live mode requires a U account; connected to {account_id}"
            )

        summary = await read_account_summary(self._ib)
        captured_at = datetime.now(timezone.utc)
        nav_base = _one_float(
            _matching_rows(
                summary,
                tag="NetLiquidation",
                currency=self._expected_base_currency,
                account_id=account_id,
            ),
            label=f"{self._expected_base_currency} NetLiquidation",
        )
        fx = _one_float(
            _matching_rows(
                summary,
                tag="ExchangeRate",
                currency=self._trading_currency,
                account_id=account_id,
                allow_all=True,
            ),
            label=f"{self._trading_currency} ExchangeRate",
        )
        # IB does not report SettledCash per currency; TotalCashBalance is the
        # per-currency settled cash figure. It may be negative when the account
        # is short the trading currency against its base-currency holdings.
        settled_cash = _one_float(
            _matching_rows(
                summary,
                tag="TotalCashBalance",
                currency=self._trading_currency,
                account_id=account_id,
                allow_all=True,
            ),
            label=f"{self._trading_currency} TotalCashBalance",
        )
        if nav_base <= 0 or fx <= 0:
            raise AccountValidationError("NAV and FX rate must be positive")
        nav_trading_equivalent = nav_base / fx
        if not math.isfinite(nav_trading_equivalent) or nav_trading_equivalent <= 0:
            raise AccountValidationError("invalid derived USD NAV")

        positions: dict[int, BrokerPosition] = {}
        for item in await _resolve(self._ib.positions()):
            item_account = str(getattr(item, "account", account_id))
            if item_account != account_id:
                raise AccountValidationError(
                    f"position belongs to unexpected account {item_account}"
                )
            contract = item.contract
            # FX currency (secType=CASH) holdings are cash, not equity-ledger
            # positions — they arise from SGD->USD funding conversions and are
            # already captured in NAV / settled_cash_trading. Reconciling them
            # against the durable equity book would flag a spurious
            # "missing_in_db" discrepancy and force-disable entries.
            if str(getattr(contract, "secType", "") or "") == "CASH":
                continue
            con_id = int(contract.conId)
            if con_id <= 0 or con_id in positions:
                raise AccountValidationError(
                    f"invalid or duplicate contract id {con_id}"
                )
            positions[con_id] = BrokerPosition(
                account_id=account_id,
                con_id=con_id,
                symbol=str(getattr(contract, "localSymbol", None) or contract.symbol),
                quantity=float(item.position),
                average_cost=float(item.avgCost),
                exchange=getattr(contract, "exchange", None),
                currency=getattr(contract, "currency", None),
            )

        # A reconciliation client may differ from the client that submitted
        # orders. reqAllOpenOrders makes those account-wide orders visible.
        trades = await _resolve(self._ib.reqAllOpenOrdersAsync())
        open_orders: dict[str, BrokerOpenOrder] = {}
        for trade in trades:
            contract = trade.contract
            order = trade.order
            order_status = trade.orderStatus
            order_account = str(getattr(order, "account", None) or account_id)
            if order_account != account_id:
                raise AccountValidationError(
                    f"open order belongs to unexpected account {order_account}"
                )
            order_id = str(order.orderId)
            if order_id in open_orders:
                raise AccountValidationError(f"duplicate open order id {order_id}")
            open_orders[order_id] = BrokerOpenOrder(
                account_id=account_id,
                ib_order_id=order_id,
                con_id=int(contract.conId),
                symbol=str(getattr(contract, "localSymbol", None) or contract.symbol),
                action=str(order.action).upper(),
                total_quantity=float(order.totalQuantity),
                filled_quantity=float(getattr(order_status, "filled", 0.0)),
                status=str(order_status.status),
                order_type=_optional_str(getattr(order, "orderType", None)),
                aux_price=_optional_float(getattr(order, "auxPrice", None)),
                tif=_optional_str(getattr(order, "tif", None)),
            )

        return BrokerAccountSnapshot(
            account_id=account_id,
            mode=self._expected_mode,
            base_currency=self._expected_base_currency,
            trading_currency=self._trading_currency,
            net_liquidation_base=nav_base,
            fx_base_per_trading=fx,
            net_liquidation_trading_equivalent=nav_trading_equivalent,
            settled_cash_trading=settled_cash,
            fx_source="$LEDGER:ALL/ExchangeRate",
            fx_captured_at=captured_at,
            positions=positions,
            open_orders=open_orders,
            captured_at=captured_at,
        )
