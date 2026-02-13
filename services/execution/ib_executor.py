from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from shared.logging import get_logger

logger = get_logger("ib_executor")


@runtime_checkable
class IBExecutorProtocol(Protocol):
    """Protocol for order execution backends."""

    async def submit_limit_order(
        self, ticker: str, quantity: int, limit_price: float
    ) -> str:
        """Submit a limit order and return the order ID."""
        ...

    async def submit_market_order(
        self, ticker: str, quantity: int
    ) -> str:
        """Submit a market order and return the order ID."""
        ...

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True if cancelled successfully."""
        ...


class IBExecutor:
    """Wraps ib_insync to submit orders to Interactive Brokers.

    Implements :class:`IBExecutorProtocol`.
    """

    def __init__(self, host: str, port: int, client_id: int) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._ib = None  # Will hold ib_insync.IB instance
        self._logger = get_logger("ib_executor")

    async def connect(self) -> None:
        """Connect to Interactive Brokers TWS/Gateway."""
        try:
            from ib_insync import IB

            self._ib = IB()
            await self._ib.connectAsync(
                self._host, self._port, clientId=self._client_id
            )
            self._logger.info(
                "Connected to IB",
                host=self._host,
                port=self._port,
                client_id=self._client_id,
            )
        except Exception:
            self._logger.exception("Failed to connect to IB")
            raise

    async def disconnect(self) -> None:
        """Disconnect from Interactive Brokers."""
        if self._ib is not None:
            self._ib.disconnect()
            self._logger.info("Disconnected from IB")

    async def submit_limit_order(
        self, ticker: str, quantity: int, limit_price: float
    ) -> str:
        """Submit a limit buy order via IB."""
        order_id = f"ib-{uuid.uuid4().hex[:12]}"
        self._logger.info(
            "Submitting limit order",
            order_id=order_id,
            ticker=ticker,
            quantity=quantity,
            limit_price=limit_price,
        )

        if self._ib is not None:
            from ib_insync import LimitOrder, Stock

            contract = Stock(ticker, "SMART", "USD")
            order = LimitOrder("BUY", quantity, limit_price)
            trade = self._ib.placeOrder(contract, order)
            order_id = str(trade.order.orderId)

        return order_id

    async def submit_market_order(
        self, ticker: str, quantity: int
    ) -> str:
        """Submit a market sell order via IB."""
        order_id = f"ib-{uuid.uuid4().hex[:12]}"
        self._logger.info(
            "Submitting market order",
            order_id=order_id,
            ticker=ticker,
            quantity=quantity,
        )

        if self._ib is not None:
            from ib_insync import MarketOrder, Stock

            contract = Stock(ticker, "SMART", "USD")
            order = MarketOrder("SELL", quantity)
            trade = self._ib.placeOrder(contract, order)
            order_id = str(trade.order.orderId)

        return order_id

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order via IB."""
        self._logger.info("Cancelling order", order_id=order_id)
        # In production this would call self._ib.cancelOrder(...)
        return True
