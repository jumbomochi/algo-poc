from __future__ import annotations

import inspect
import math
from datetime import datetime, timezone
from typing import Any

from shared.broker_state import (
    BrokerAccountSnapshot,
    BrokerOpenOrder,
    BrokerPosition,
)


class AccountValidationError(RuntimeError):
    pass


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

        summary = list(await _resolve(self._ib.accountSummaryAsync()))
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
        settled_cash = _one_float(
            _matching_rows(
                summary,
                tag="SettledCash",
                currency=self._trading_currency,
                account_id=account_id,
                allow_all=True,
            ),
            label=f"{self._trading_currency} SettledCash",
        )
        if nav_base <= 0 or fx <= 0:
            raise AccountValidationError("NAV and FX rate must be positive")

        positions: dict[int, BrokerPosition] = {}
        for item in await _resolve(self._ib.positions()):
            item_account = str(getattr(item, "account", account_id))
            if item_account != account_id:
                raise AccountValidationError(
                    f"position belongs to unexpected account {item_account}"
                )
            contract = item.contract
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
            )

        return BrokerAccountSnapshot(
            account_id=account_id,
            mode=self._expected_mode,
            base_currency=self._expected_base_currency,
            trading_currency=self._trading_currency,
            net_liquidation_base=nav_base,
            fx_base_per_trading=fx,
            net_liquidation_trading_equivalent=nav_base / fx,
            settled_cash_trading=settled_cash,
            fx_source="$LEDGER:ALL/ExchangeRate",
            fx_captured_at=captured_at,
            positions=positions,
            open_orders=open_orders,
            captured_at=captured_at,
        )
