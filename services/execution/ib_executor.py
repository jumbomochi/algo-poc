from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from shared.logging import get_logger

logger = get_logger("ib_executor")

# Payload passed to the fill handler on every real IB fill (partial or full).
FillHandler = Callable[[dict[str, Any]], Awaitable[None]]


@runtime_checkable
class IBExecutorProtocol(Protocol):
    """Protocol for order execution backends."""

    async def submit_limit_order(
        self, ticker: str, quantity: float, limit_price: float
    ) -> str:
        """Submit a limit order and return the order ID."""
        ...

    async def submit_market_order(self, ticker: str, quantity: float) -> str:
        """Submit a market order and return the order ID."""
        ...

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if cancelled successfully."""
        ...


class NotConnectedError(RuntimeError):
    """Raised when an order operation is attempted without an IB connection."""


class WrongAccountTypeError(RuntimeError):
    """Raised when the Gateway session's account type contradicts the mode."""


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

    def __init__(self, host: str, port: int, client_id: int) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._ib = None  # Will hold ib_insync.IB instance
        self._trades: dict[str, Any] = {}  # order_id -> ib_insync.Trade
        self._fill_handler: FillHandler | None = None
        self._logger = get_logger("ib_executor")

    @property
    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    def set_fill_handler(self, handler: FillHandler) -> None:
        """Register the async callback invoked on every real IB fill."""
        self._fill_handler = handler

    async def connect(self, expect_paper: bool | None = None) -> None:
        """Connect to Interactive Brokers TWS/Gateway.

        Args:
            expect_paper: When True, refuse the connection unless every
                managed account is a paper account (``DU`` prefix). Guards
                against the Gateway being logged into a LIVE session on the
                paper port — which happened on 2026-07-04 (live account
                U-prefix answering on 7497 after a manual live login).
        """
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

    def _require_connection(self) -> None:
        if not self.is_connected:
            raise NotConnectedError(
                f"IB not connected ({self._host}:{self._port}); refusing to "
                "fake an order id"
            )

    def _register_trade(self, order_id: str, trade: Any, ticker: str, side: str) -> None:
        """Track the trade and wire its fill event to the registered handler."""
        self._trades[order_id] = trade

        def _on_fill(trade: Any, fill: Any) -> None:
            payload = {
                "order_id": order_id,
                "ticker": ticker,
                "side": side,
                "quantity": float(fill.execution.shares),
                "fill_price": float(fill.execution.price),
                "commission": float(
                    getattr(fill.commissionReport, "commission", 0.0) or 0.0
                ),
                "order_done": trade.isDone(),
            }
            if self._fill_handler is None:
                self._logger.warning("IB fill received but no handler set", **payload)
                return
            asyncio.ensure_future(self._fill_handler(payload))

        trade.fillEvent += _on_fill

    async def submit_limit_order(
        self, ticker: str, quantity: float, limit_price: float
    ) -> str:
        """Submit a limit buy order via IB."""
        self._require_connection()
        from ib_insync import LimitOrder, Stock

        contract = Stock(ticker, "SMART", "USD")
        order = LimitOrder("BUY", quantity, limit_price)
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

    async def submit_market_order(self, ticker: str, quantity: float) -> str:
        """Submit a market sell order via IB."""
        self._require_connection()
        from ib_insync import MarketOrder, Stock

        contract = Stock(ticker, "SMART", "USD")
        order = MarketOrder("SELL", quantity)
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
        self._require_connection()
        trade = self._trades.get(order_id)
        if trade is None:
            self._logger.warning(
                "Cancel requested for unknown order", order_id=order_id
            )
            return False
        self._ib.cancelOrder(trade.order)
        self._logger.info("Order cancel requested", order_id=order_id)
        return True
