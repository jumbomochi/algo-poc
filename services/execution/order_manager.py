from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from shared.logging import get_logger

logger = get_logger("order_manager")


@dataclass
class OrderAction:
    """Describes an action to take on an unfilled order."""

    order_id: str
    action_type: Literal["reprice", "cancel"]
    new_price: float | None = None


@dataclass
class PartialFillDecision:
    """Decision on how to handle a partial fill."""

    action: Literal["accept", "flag_for_review"]
    filled_pct: float
    message: str


class OrderManager:
    """Manages order submission with idempotency and unfilled order handling.

    Tracks submitted recommendation IDs to prevent duplicate orders.
    Manages open orders for reprice / cancel decisions.

    Args:
        executor: An object implementing :class:`IBExecutorProtocol`.
        redis_client: Redis stream client for publishing events.
        db_session: Database session for persistence.
        reprice_interval_minutes: Minutes before an unfilled order is repriced.
        max_reprice_attempts: Maximum number of reprice attempts before cancelling.
    """

    def __init__(
        self,
        executor: Any,
        redis_client: Any,
        db_session: Any,
        reprice_interval_minutes: int = 60,
        max_reprice_attempts: int = 3,
    ) -> None:
        self._executor = executor
        self._redis = redis_client
        self._db = db_session
        self._reprice_interval_minutes = reprice_interval_minutes
        self._max_reprice_attempts = max_reprice_attempts
        self._logger = get_logger("order_manager")

        # Idempotency: maps recommendation_id -> order_id
        self._submitted: dict[str, str] = {}

        # Open orders: maps order_id -> order info dict
        self.open_orders: dict[str, dict[str, Any]] = {}

    async def submit_entry(
        self,
        ticker: str,
        quantity: int,
        limit_price: float,
        recommendation_id: str,
    ) -> str:
        """Submit a limit entry order.

        Idempotent: if the recommendation_id has already been submitted,
        returns the existing order ID without submitting again.

        Args:
            ticker: The stock ticker symbol.
            quantity: Number of shares to buy.
            limit_price: Limit price for the order.
            recommendation_id: Unique recommendation identifier for idempotency.

        Returns:
            The order ID string.
        """
        # Idempotency check
        if recommendation_id in self._submitted:
            self._logger.info(
                "Duplicate entry submission blocked",
                recommendation_id=recommendation_id,
                existing_order_id=self._submitted[recommendation_id],
            )
            return self._submitted[recommendation_id]

        order_id = await self._executor.submit_limit_order(
            ticker, quantity, limit_price
        )

        now = datetime.now(timezone.utc)

        # Track for idempotency
        self._submitted[recommendation_id] = order_id

        # Track as open order
        self.open_orders[order_id] = {
            "ticker": ticker,
            "quantity": quantity,
            "limit_price": limit_price,
            "placed_at": now,
            "last_repriced_at": now,
            "reprice_count": 0,
            "recommendation_id": recommendation_id,
        }

        self._logger.info(
            "Entry order submitted",
            order_id=order_id,
            ticker=ticker,
            quantity=quantity,
            limit_price=limit_price,
            recommendation_id=recommendation_id,
        )

        return order_id

    async def submit_exit(
        self,
        ticker: str,
        quantity: int,
        recommendation_id: str,
    ) -> str:
        """Submit a market exit order.

        Idempotent: if the recommendation_id has already been submitted,
        returns the existing order ID without submitting again.

        Args:
            ticker: The stock ticker symbol.
            quantity: Number of shares to sell.
            recommendation_id: Unique recommendation identifier for idempotency.

        Returns:
            The order ID string.
        """
        # Idempotency check
        if recommendation_id in self._submitted:
            self._logger.info(
                "Duplicate exit submission blocked",
                recommendation_id=recommendation_id,
                existing_order_id=self._submitted[recommendation_id],
            )
            return self._submitted[recommendation_id]

        order_id = await self._executor.submit_market_order(ticker, quantity)

        # Track for idempotency
        self._submitted[recommendation_id] = order_id

        self._logger.info(
            "Exit order submitted",
            order_id=order_id,
            ticker=ticker,
            quantity=quantity,
            recommendation_id=recommendation_id,
        )

        return order_id

    def check_unfilled_orders(
        self,
        current_prices: dict[str, float],
        market_calendar: Any,
    ) -> list[OrderAction]:
        """Check all open orders and decide on reprice or cancel actions.

        Rules:
        1. If market close is within 15 minutes -> cancel
        2. If max reprice attempts reached -> cancel
        3. If unfilled for longer than reprice_interval_minutes -> reprice
        4. Otherwise -> no action

        Args:
            current_prices: Map of ticker -> current market price.
            market_calendar: MarketCalendar instance for close time checks.

        Returns:
            List of OrderAction describing what to do with each order.
        """
        now = datetime.now(timezone.utc)
        actions: list[OrderAction] = []

        for order_id, info in self.open_orders.items():
            ticker = info["ticker"]
            next_close = market_calendar.get_next_market_close(now)

            # Rule 1: Cancel if market close is within 15 minutes
            time_to_close = next_close - now
            if time_to_close <= timedelta(minutes=15):
                actions.append(
                    OrderAction(
                        order_id=order_id,
                        action_type="cancel",
                        new_price=None,
                    )
                )
                self._logger.info(
                    "Unfilled order cancelled at market close",
                    order_id=order_id,
                    ticker=ticker,
                )
                continue

            # Rule 2: Cancel if max reprice attempts reached
            if info["reprice_count"] >= self._max_reprice_attempts:
                actions.append(
                    OrderAction(
                        order_id=order_id,
                        action_type="cancel",
                        new_price=None,
                    )
                )
                self._logger.info(
                    "Unfilled order cancelled after max reprices",
                    order_id=order_id,
                    ticker=ticker,
                    reprice_count=info["reprice_count"],
                )
                continue

            # Rule 3: Reprice if unfilled for longer than interval
            time_since_last = now - info["last_repriced_at"]
            if time_since_last >= timedelta(
                minutes=self._reprice_interval_minutes
            ):
                new_price = current_prices.get(ticker, info["limit_price"])
                actions.append(
                    OrderAction(
                        order_id=order_id,
                        action_type="reprice",
                        new_price=new_price,
                    )
                )
                self._logger.info(
                    "Unfilled order repriced",
                    order_id=order_id,
                    ticker=ticker,
                    new_price=new_price,
                )
                continue

            # Rule 4: Recently placed — no action

        return actions

    def handle_partial_fill(
        self,
        order_id: str,
        filled_quantity: int,
        total_quantity: int,
        min_viable_fill_pct: float,
    ) -> PartialFillDecision:
        """Decide how to handle a partial fill.

        Args:
            order_id: The order that was partially filled.
            filled_quantity: Number of shares actually filled.
            total_quantity: Total number of shares in the original order.
            min_viable_fill_pct: Minimum fill percentage to accept (e.g. 40.0).

        Returns:
            A PartialFillDecision indicating whether to accept or flag.
        """
        filled_pct = (filled_quantity / total_quantity) * 100.0

        if filled_pct >= min_viable_fill_pct:
            self._logger.info(
                "Partial fill accepted",
                order_id=order_id,
                filled_pct=filled_pct,
            )
            return PartialFillDecision(
                action="accept",
                filled_pct=filled_pct,
                message=f"Accepted as undersized position ({filled_pct:.1f}% filled)",
            )

        self._logger.warning(
            "Partial fill flagged for review",
            order_id=order_id,
            filled_pct=filled_pct,
            min_viable=min_viable_fill_pct,
        )
        return PartialFillDecision(
            action="flag_for_review",
            filled_pct=filled_pct,
            message=(
                f"Below minimum viable fill ({filled_pct:.1f}% < {min_viable_fill_pct}%). "
                f"Flagged for operator review."
            ),
        )

    async def cancel_all_orders(self) -> list[str]:
        """Cancel all open orders.

        Returns:
            List of cancelled order IDs.
        """
        cancelled = []
        for order_id in list(self.open_orders.keys()):
            try:
                await self._executor.cancel_order(order_id)
                cancelled.append(order_id)
                self._logger.info("Order cancelled", order_id=order_id)
            except Exception:
                self._logger.exception(
                    "Failed to cancel order", order_id=order_id
                )
        self.open_orders.clear()
        return cancelled
