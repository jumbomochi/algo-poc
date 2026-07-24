from __future__ import annotations

import asyncio
import math
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from shared.logging import get_logger

logger = get_logger("ib_executor")

# Payload passed to the fill handler on every real IB fill (partial or full).
FillHandler = Callable[[dict[str, Any]], Awaitable[None]]
OrderStatusHandler = Callable[[dict[str, Any]], Awaitable[None]]


def _commission_in_usd(
    amount: float,
    currency: str,
    *,
    fx_base_per_trading: float | None,
) -> float | None:
    if currency == "USD":
        return amount
    if currency == "SGD" and fx_base_per_trading is not None:
        if math.isfinite(fx_base_per_trading) and fx_base_per_trading > 0:
            return amount / fx_base_per_trading
    return None


@runtime_checkable
class IBExecutorProtocol(Protocol):
    """Protocol for order execution backends."""

    async def submit_limit_order(
        self,
        ticker: str,
        quantity: float,
        limit_price: float,
        recommendation_id: str | None = None,
    ) -> str:
        """Submit a limit order and return the order ID."""
        ...

    async def submit_market_order(
        self,
        ticker: str,
        quantity: float,
        recommendation_id: str | None = None,
    ) -> str:
        """Submit a market order and return the order ID."""
        ...

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if cancelled successfully."""
        ...

    async def find_order_by_ref(
        self, recommendation_id: str
    ) -> str | None:
        """Find an open or completed broker order by stable orderRef."""
        ...

    async def restore_order_by_ref(
        self, recommendation_id: str, expected_order_id: str
    ) -> bool | None:
        """Restore callbacks; false means completed, None means missing."""
        ...


class NotConnectedError(RuntimeError):
    """Raised when an order operation is attempted without an IB connection."""


class WrongAccountTypeError(RuntimeError):
    """Raised when the Gateway session's account type contradicts the mode."""


class OrderSkippedError(RuntimeError):
    """Raised when an order cannot be placed as sized (e.g. a fractional
    quantity rounds to zero whole shares on an account without fractional
    API support)."""


class IBExecutor:
    """Wraps ib_insync to submit orders to Interactive Brokers.

    Implements :class:`IBExecutorProtocol`.

    Fails loud: every order operation raises :class:`NotConnectedError`
    when the IB connection is absent — a fake order id that reports
    success while never touching the broker is how positions silently
    diverge from reality.

    Real fills: callers register an async fill handler via
    :meth:`set_fill_handler`; it is invoked for every actual IB fill
    (partial or full) with execution price/quantity, never at submission
    time.
    """

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        allow_fractional: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._allow_fractional = allow_fractional
        self._ib = None  # Will hold ib_insync.IB instance
        self._trades: dict[str, Any] = {}  # order_id -> ib_insync.Trade
        self._fill_handler: FillHandler | None = None
        self._order_status_handler: OrderStatusHandler | None = None
        self._expect_paper: bool | None = None
        self._logger = get_logger("ib_executor")

    def _effective_quantity(self, ticker: str, quantity: float) -> float:
        """Round to whole shares when the account can't trade fractions.

        Raises :class:`OrderSkippedError` when the rounded quantity is zero —
        the caller must treat the order as skipped, not failed.
        """
        if self._allow_fractional or float(quantity).is_integer():
            return quantity
        rounded = float(int(quantity))
        if rounded <= 0:
            raise OrderSkippedError(
                f"{ticker}: fractional quantity {quantity} rounds to zero "
                "whole shares (account has no fractional API support)"
            )
        self._logger.warning(
            "Quantity rounded to whole shares (no fractional API support)",
            ticker=ticker,
            requested=quantity,
            placed=rounded,
        )
        return rounded

    @property
    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    def set_fill_handler(self, handler: FillHandler) -> None:
        """Register the async callback invoked on every real IB fill."""
        self._fill_handler = handler

    def set_order_status_handler(self, handler: OrderStatusHandler) -> None:
        """Register the callback for broker lifecycle status changes."""
        self._order_status_handler = handler

    @staticmethod
    def _status_reason(trade: Any) -> str:
        """Extract inbound broker context for rejection-like statuses."""
        why_held = str(getattr(trade.orderStatus, "whyHeld", "") or "")
        if why_held:
            return why_held
        for entry in reversed(getattr(trade, "log", ())):
            message = str(getattr(entry, "message", "") or "")
            if message:
                return message
        return ""

    async def connect(self, expect_paper: bool | None = None) -> None:
        """Connect to Interactive Brokers TWS/Gateway.

        Args:
            expect_paper: When True, refuse the connection unless every
                managed account is a paper account (``DU`` prefix). Guards
                against the Gateway being logged into a LIVE session on the
                paper port — which happened on 2026-07-04 (live account
                U-prefix answering on 7497 after a manual live login).
        """
        self._expect_paper = expect_paper
        try:
            from ib_insync import IB

            self._ib = IB()
            await self._ib.connectAsync(
                self._host, self._port, clientId=self._client_id
            )
            accounts = self._ib.managedAccounts()

            if expect_paper:
                non_paper = [a for a in accounts if not a.startswith("DU")]
                if non_paper:
                    self._ib.disconnect()
                    self._ib = None
                    raise WrongAccountTypeError(
                        f"Paper mode but the Gateway session holds LIVE "
                        f"account(s) {non_paper} on port {self._port}. "
                        "Re-login the Gateway with the paper credentials."
                    )

            self._logger.info(
                "Connected to IB",
                host=self._host,
                port=self._port,
                client_id=self._client_id,
                accounts=accounts,
            )
        except WrongAccountTypeError:
            raise
        except Exception:
            self._ib = None
            self._logger.exception("Failed to connect to IB")
            raise

    async def disconnect(self) -> None:
        """Disconnect from Interactive Brokers."""
        if self._ib is not None:
            self._ib.disconnect()
            self._logger.info("Disconnected from IB")

    async def _ensure_connected(self) -> None:
        """Reconnect on demand when the Gateway dropped the session.

        The Gateway drops API sockets routinely (data-farm resets, the
        nightly auto-restart); orders must not fail on a stale socket while
        the Gateway itself is healthy. The reconnect re-applies the same
        ``expect_paper`` guard as the original connect —
        :class:`WrongAccountTypeError` propagates untouched. Any other
        reconnect failure raises :class:`NotConnectedError`: still failing
        loud, never faking an order id.
        """
        if self.is_connected:
            return
        self._logger.warning(
            "IB connection lost — reconnecting",
            host=self._host,
            port=self._port,
        )
        try:
            await self.connect(expect_paper=self._expect_paper)
        except WrongAccountTypeError:
            raise
        except Exception as exc:
            raise NotConnectedError(
                f"IB not connected ({self._host}:{self._port}) and "
                f"reconnect failed: {exc}"
            ) from exc

    def _register_trade(self, order_id: str, trade: Any, ticker: str, side: str) -> None:
        """Track the trade and publish fills after IB reports commission."""
        self._trades[order_id] = trade

        def _on_commission_report(
            trade: Any, fill: Any, commission_report: Any
        ) -> None:
            commission = float(
                getattr(commission_report, "commission", 0.0) or 0.0
            )
            commission_currency = str(
                getattr(commission_report, "currency", "") or ""
            )
            commission_fx_base_per_trading = None
            if commission_currency == "SGD" and self._ib is not None:
                rows = [
                    row
                    for row in (self._ib.accountValues() or ())
                    if getattr(row, "tag", None) == "ExchangeRate"
                    and getattr(row, "currency", None) == "USD"
                ]
                if len(rows) == 1:
                    try:
                        candidate = float(rows[0].value)
                    except (TypeError, ValueError):
                        candidate = None
                    if (
                        candidate is not None
                        and math.isfinite(candidate)
                        and candidate > 0
                    ):
                        commission_fx_base_per_trading = candidate
            payload = {
                "execution_id": str(fill.execution.execId),
                "account_id": str(fill.execution.acctNumber),
                "order_id": order_id,
                "con_id": int(fill.contract.conId),
                "ticker": ticker,
                "exchange": fill.contract.exchange or "SMART",
                "currency": fill.contract.currency or "USD",
                "side": side,
                "quantity": float(fill.execution.shares),
                "cumulative_quantity": float(fill.execution.cumQty),
                "fill_price": float(fill.execution.price),
                "commission": commission,
                "commission_currency": commission_currency,
                "commission_trading": _commission_in_usd(
                    commission,
                    commission_currency,
                    fx_base_per_trading=commission_fx_base_per_trading,
                ),
                "commission_fx_base_per_trading": (
                    commission_fx_base_per_trading
                ),
                "order_done": trade.isDone(),
            }
            if self._fill_handler is None:
                self._logger.warning("IB fill received but no handler set", **payload)
                return
            asyncio.ensure_future(self._fill_handler(payload))

        trade.commissionReportEvent += _on_commission_report

        def _on_status(trade: Any) -> None:
            if self._order_status_handler is None:
                return
            status = str(trade.orderStatus.status)
            reason = self._status_reason(trade)
            asyncio.ensure_future(
                self._emit_order_status(
                    order_id,
                    status,
                    reason,
                    filled_quantity=float(trade.orderStatus.filled or 0.0),
                )
            )

        trade.statusEvent += _on_status

    async def _emit_order_status(
        self,
        order_id: str,
        status: str,
        reason: str,
        *,
        filled_quantity: float = 0.0,
    ) -> None:
        if self._order_status_handler is None:
            return
        confirmed = False
        if status == "Expired":
            confirmed = await self.completed_order_confirms_expiry(order_id)
        await self._order_status_handler({
            "order_id": order_id,
            "status": status,
            "reason": reason,
            "filled_quantity": float(filled_quantity),
            "completed_order_confirmed": confirmed,
        })

    async def completed_order_confirms_expiry(self, order_id: str) -> bool:
        """Return true only when IB completed-order history says Expired."""
        await self._ensure_connected()
        completed = await self._ib.reqCompletedOrdersAsync(apiOnly=False)
        return any(
            str(trade.order.orderId) == str(order_id)
            and str(trade.orderStatus.status) == "Expired"
            for trade in completed
        )

    async def find_order_by_ref(self, recommendation_id: str) -> str | None:
        """Recover an IB-accepted order from its stable recommendation ref."""
        await self._ensure_connected()
        trades = list(self._ib.openTrades())
        for trade in trades:
            if str(getattr(trade.order, "orderRef", "")) != recommendation_id:
                continue
            order_id = str(trade.order.orderId)
            action = str(getattr(trade.order, "action", "")).lower()
            side = "buy" if action == "buy" else "sell"
            ticker = str(trade.contract.symbol)
            if order_id not in self._trades:
                self._register_trade(order_id, trade, ticker=ticker, side=side)
            return order_id
        completed = await self._ib.reqCompletedOrdersAsync(apiOnly=False)
        for trade in completed:
            if str(getattr(trade.order, "orderRef", "")) == recommendation_id:
                return str(trade.order.orderId)
        return None

    async def restore_order_by_ref(
        self, recommendation_id: str, expected_order_id: str
    ) -> bool | None:
        """Reattach callbacks, or reconcile a terminal completed order."""
        await self._ensure_connected()
        for trade in self._ib.openTrades():
            if str(getattr(trade.order, "orderRef", "")) != recommendation_id:
                continue
            order_id = str(trade.order.orderId)
            if order_id != str(expected_order_id):
                raise RuntimeError(
                    f"orderRef {recommendation_id} maps to broker order "
                    f"{order_id}, expected {expected_order_id}"
                )
            action = str(getattr(trade.order, "action", "")).lower()
            if order_id not in self._trades:
                self._register_trade(
                    order_id,
                    trade,
                    ticker=str(trade.contract.symbol),
                    side="buy" if action == "buy" else "sell",
                )
            return True

        completed = await self._ib.reqCompletedOrdersAsync(apiOnly=False)
        for trade in completed:
            if (
                str(getattr(trade.order, "orderRef", ""))
                != recommendation_id
                or str(trade.order.orderId) != str(expected_order_id)
            ):
                continue
            status = str(trade.orderStatus.status)
            reason = self._status_reason(trade)
            if status == "Inactive" and not reason:
                reason = "IB completed order is Inactive"
            if self._order_status_handler is not None:
                await self._order_status_handler({
                    "order_id": str(expected_order_id),
                    "status": status,
                    "reason": reason,
                    "filled_quantity": float(
                        getattr(trade.orderStatus, "filled", 0.0) or 0.0
                    ),
                    "completed_order_confirmed": True,
                })
            return False
        return None

    async def submit_limit_order(
        self,
        ticker: str,
        quantity: float,
        limit_price: float,
        recommendation_id: str | None = None,
    ) -> str:
        """Submit a limit buy order via IB."""
        await self._ensure_connected()
        quantity = self._effective_quantity(ticker, quantity)
        from ib_insync import LimitOrder, Stock

        contract = Stock(ticker, "SMART", "USD")
        # Explicit TIF: without it the order inherits the TWS desktop preset,
        # which can mutate/cancel API orders (Error 10349 observed).
        order = LimitOrder("BUY", quantity, limit_price, tif="DAY")
        if recommendation_id is not None:
            order.orderRef = recommendation_id
        trade = self._ib.placeOrder(contract, order)
        order_id = str(trade.order.orderId)
        self._register_trade(order_id, trade, ticker=ticker, side="buy")

        self._logger.info(
            "Limit order submitted",
            order_id=order_id,
            ticker=ticker,
            quantity=quantity,
            limit_price=limit_price,
        )
        return order_id

    async def submit_market_order(
        self,
        ticker: str,
        quantity: float,
        recommendation_id: str | None = None,
    ) -> str:
        """Submit a market sell order via IB."""
        await self._ensure_connected()
        quantity = self._effective_quantity(ticker, quantity)
        from ib_insync import MarketOrder, Stock

        contract = Stock(ticker, "SMART", "USD")
        order = MarketOrder("SELL", quantity, tif="DAY")
        if recommendation_id is not None:
            order.orderRef = recommendation_id
        trade = self._ib.placeOrder(contract, order)
        order_id = str(trade.order.orderId)
        self._register_trade(order_id, trade, ticker=ticker, side="sell")

        self._logger.info(
            "Market order submitted",
            order_id=order_id,
            ticker=ticker,
            quantity=quantity,
        )
        return order_id

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order via IB."""
        await self._ensure_connected()
        trade = self._trades.get(order_id)
        if trade is None:
            self._logger.warning(
                "Cancel requested for unknown order", order_id=order_id
            )
            return False
        self._ib.cancelOrder(trade.order)
        self._logger.info("Order cancel requested", order_id=order_id)
        return True
