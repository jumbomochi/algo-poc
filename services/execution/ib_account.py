from __future__ import annotations

import inspect
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


class IBAccountReader:
    """Read one validated IB account into contract-keyed immutable state."""

    def __init__(self, ib: Any, *, expected_mode: str) -> None:
        mode = expected_mode.lower()
        if mode not in {"paper", "live"}:
            raise ValueError("expected_mode must be 'paper' or 'live'")
        self._ib = ib
        self._expected_mode = mode

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
        nav_values = [
            item for item in summary
            if getattr(item, "tag", None) == "NetLiquidation"
            and getattr(item, "account", account_id) in {"", account_id}
            and getattr(item, "currency", "USD") in {"", "USD", "BASE"}
        ]
        if len(nav_values) != 1:
            raise AccountValidationError(
                "expected exactly one USD/BASE NetLiquidation value"
            )
        try:
            net_liquidation = float(nav_values[0].value)
        except (TypeError, ValueError) as exc:
            raise AccountValidationError("invalid NetLiquidation value") from exc

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
            net_liquidation=net_liquidation,
            positions=positions,
            open_orders=open_orders,
            captured_at=datetime.now(timezone.utc),
        )
