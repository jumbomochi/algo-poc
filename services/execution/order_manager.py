from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from shared.logging import get_logger

logger = get_logger("order_manager")

# How long :meth:`OrderManager.cancel_working_orders` waits for IB to take a
# cancel off its book. One entry per retry; the book is checked once
# immediately, so an already-dead order costs nothing. Bounded on purpose —
# the caller is an emergency sell, and waiting forever is its own failure.
CANCEL_ACK_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.0, 2.0)

# Order types the unfilled-limit sweep must not touch: neither has a limit to
# reprice, and neither should be cancelled for still being open.
_SWEEP_EXEMPT_ORDER_TYPES = frozenset({"market", "stop"})


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
        self._recovered: set[str] = set()

        # Open orders: maps order_id -> order info dict
        self.open_orders: dict[str, dict[str, Any]] = {}

        # Overridable so tests do not sleep out the real schedule.
        self._cancel_ack_backoff = CANCEL_ACK_BACKOFF_SECONDS

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

        recovered = await self._executor.find_order_by_ref(recommendation_id)
        if isinstance(recovered, (str, int)):
            order_id = str(recovered)
            self._recovered.add(recommendation_id)
        else:
            order_id = await self._executor.submit_limit_order(
                ticker,
                quantity,
                limit_price,
                recommendation_id=recommendation_id,
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
            "order_type": "limit",
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

        recovered = await self._executor.find_order_by_ref(recommendation_id)
        if isinstance(recovered, (str, int)):
            order_id = str(recovered)
            self._recovered.add(recommendation_id)
        else:
            order_id = await self._executor.submit_market_order(
                ticker, quantity, recommendation_id=recommendation_id
            )

        # Track for idempotency
        self._submitted[recommendation_id] = order_id

        # Track as an open order so cancel_all_orders can reach a stuck exit
        # (a market exit normally fills at once, but a queued/rejected one must
        # not be invisible to the kill path). Marked "market" so the unfilled
        # sweep never tries to reprice it.
        now = datetime.now(timezone.utc)
        self.open_orders[order_id] = {
            "ticker": ticker,
            "quantity": quantity,
            "limit_price": None,
            "placed_at": now,
            "last_repriced_at": now,
            "reprice_count": 0,
            "recommendation_id": recommendation_id,
            "order_type": "market",
        }

        self._logger.info(
            "Exit order submitted",
            order_id=order_id,
            ticker=ticker,
            quantity=quantity,
            recommendation_id=recommendation_id,
        )

        return order_id

    async def submit_stop(
        self,
        ticker: str,
        quantity: float,
        stop_price: float,
        recommendation_id: str,
        *,
        tif: str = "GTC",
        outside_rth: bool = False,
    ) -> str:
        """Place a protective stop that rests at the broker (KAN-19).

        Tracked in :attr:`open_orders` deliberately. The spike (KAN-18)
        confirmed end-to-end that :meth:`cancel_all_orders` cannot reach a stop
        it does not know about: the kill would then complete "successfully"
        while leaving protective sells resting against positions it had just
        flattened, and each one sells short when triggered. Tracking the stop
        here is what puts it inside the kill's cancel-all, before liquidation.

        Marked ``order_type="stop"`` so the unfilled-order sweep leaves it
        alone — resting unfilled for weeks is what a GTC stop is *for*.
        """
        if recommendation_id in self._submitted:
            self._logger.info(
                "Duplicate stop submission blocked",
                recommendation_id=recommendation_id,
                existing_order_id=self._submitted[recommendation_id],
            )
            return self._submitted[recommendation_id]

        recovered = await self._executor.find_order_by_ref(recommendation_id)
        if isinstance(recovered, (str, int)):
            order_id = str(recovered)
            self._recovered.add(recommendation_id)
        else:
            order_id = await self._executor.submit_stop_order(
                ticker,
                quantity,
                stop_price,
                recommendation_id=recommendation_id,
                tif=tif,
                outside_rth=outside_rth,
            )

        self._submitted[recommendation_id] = order_id

        now = datetime.now(timezone.utc)
        self.open_orders[order_id] = {
            "ticker": ticker,
            "quantity": quantity,
            "limit_price": None,
            "stop_price": stop_price,
            "placed_at": now,
            "last_repriced_at": now,
            "reprice_count": 0,
            "recommendation_id": recommendation_id,
            "order_type": "stop",
        }

        self._logger.info(
            "Stop order submitted",
            order_id=order_id,
            ticker=ticker,
            quantity=quantity,
            stop_price=stop_price,
            tif=tif,
            recommendation_id=recommendation_id,
        )

        return order_id

    def restore_submission(
        self,
        recommendation_id: str,
        order_id: str,
        *,
        ticker: str,
        quantity: float,
        limit_price: float | None,
        order_type: str | None = None,
    ) -> None:
        """Restore durable idempotency/open-order state after a restart.

        ``order_type`` carries the ledger's own word for what the order is. It
        matters for a resting stop (KAN-19): restored without it, the order
        looks like an unfilled limit to :meth:`check_unfilled_orders`, which
        cancels it at the next close — the protection would survive the
        Gateway restart it is designed to survive, then be cancelled by our
        own housekeeping.
        """
        restored = {
            "ticker": ticker,
            "quantity": quantity,
            "limit_price": limit_price,
            "placed_at": datetime.now(timezone.utc),
            "last_repriced_at": datetime.now(timezone.utc),
            "reprice_count": 0,
            "recommendation_id": recommendation_id,
        }
        if order_type is not None:
            restored["order_type"] = order_type
        self._submitted[recommendation_id] = order_id
        self.open_orders.setdefault(order_id, restored)

    async def restore_broker_tracking(self) -> None:
        """Reattach executor callbacks for submissions loaded from the DB."""
        for recommendation_id, order_id in self._submitted.items():
            restored = await self._executor.restore_order_by_ref(
                recommendation_id, order_id
            )
            if restored is None:
                raise RuntimeError(
                    f"persisted order {order_id} ({recommendation_id}) "
                    "is missing at IB"
                )

    async def reconcile_submission(
        self, recommendation_id: str, order_id: str
    ) -> bool:
        """Reconcile a recovered crash-window order after DB submission commit."""
        if recommendation_id not in self._recovered:
            return True
        restored = await self._executor.restore_order_by_ref(
            recommendation_id, order_id
        )
        if restored is None:
            raise RuntimeError(
                f"recovered order {order_id} ({recommendation_id}) "
                "is missing at IB"
            )
        self._recovered.discard(recommendation_id)
        return restored

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
            # Market orders (e.g. exits/liquidations) fill at once and have no
            # limit to reprice; the unfilled-limit sweep must leave them alone.
            # Nor may it touch a resting stop (KAN-19): sitting unfilled for
            # weeks is what a GTC stop is for, and the close-approaching rule
            # below would cancel overnight protection every afternoon.
            #
            # Listed explicitly rather than skipping "anything that is not a
            # limit": restore_submission writes open_orders entries with no
            # order_type at all, and those must keep being swept.
            if info.get("order_type") in _SWEEP_EXEMPT_ORDER_TYPES:
                continue
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

    async def sweep_unfilled_orders(
        self,
        current_prices: dict[str, float],
        market_calendar: Any,
    ) -> int:
        """Apply the unfilled-order decisions from :meth:`check_unfilled_orders`.

        Cancels orders that are near close or past the reprice-attempt limit
        (freeing their reservation once IB reports the cancellation). Repricing
        needs a live quote — which the execution service has no feed for — so a
        reprice decision only advances the order's bookkeeping, letting a
        persistently-unfilled limit progress to the max-attempts cancel instead
        of silently dying at IB session expiry. Returns the number of orders
        cancelled.
        """
        cancelled = 0
        for action in self.check_unfilled_orders(current_prices, market_calendar):
            info = self.open_orders.get(action.order_id)
            if info is None:
                continue
            if action.action_type == "cancel":
                try:
                    await self._executor.cancel_order(action.order_id)
                    self.open_orders.pop(action.order_id, None)
                    cancelled += 1
                except Exception:
                    self._logger.exception(
                        "Failed to cancel unfilled order",
                        order_id=action.order_id,
                    )
            else:  # reprice — no live quote feed, so only advance bookkeeping
                info["reprice_count"] += 1
                info["last_repriced_at"] = datetime.now(timezone.utc)
                self._logger.info(
                    "Unfilled limit aged (no quote feed to reprice)",
                    order_id=action.order_id,
                    reprice_count=info["reprice_count"],
                )
        return cancelled

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

    async def list_open_broker_orders(self) -> list[Any]:
        """Every order live at the broker, with its stable ``orderRef``.

        Not ``self.open_orders``: that is this process's own view, and the
        post-halt sweep exists precisely for an order the process does not
        know it placed.
        """
        return list(await self._executor.list_open_orders())

    async def cancel_broker_order(self, order_id: str) -> bool:
        """Cancel one live broker order, tracked locally or not.

        Local tracking is dropped only on a submitted cancel, matching
        :meth:`cancel_all_orders` — a failed cancel is still live at IB.
        """
        cancelled = await self._executor.cancel_broker_order(order_id)
        if cancelled:
            self.open_orders.pop(order_id, None)
        return cancelled

    async def cancel_working_orders(
        self, orders: Sequence[tuple[str, str]]
    ) -> list[str]:
        """Cancel each ``(order_id, order_ref)`` and wait until IB agrees.

        Returns the refs still live once the wait is exhausted — empty means
        every one of them is off the broker's book.

        The confirmation is the point (KAN-10). ``cancelOrder`` only *requests*
        a cancel; a BUY that fills between the request and the sell is the
        exact race the oversell guard exists to close, so the caller must not
        size anything off a request that has merely been sent. IB's own open
        orders decide, matched by ``orderRef`` — the one identifier that
        survives a restart, which is when a working order is least likely to
        be tracked in ``open_orders``.

        The book settles it, not the request: a ``False`` (IB does not list the
        order) and even a raising cancel are both fine if the ref is gone
        afterwards, because gone is the state being asked for. What leaves a
        ref unconfirmed is a book that still lists it, or one that cannot be
        read at all — the disconnect case, where nothing can be confirmed and
        so nothing is.
        """
        pending = {str(order_ref) for _, order_ref in orders}
        if not pending:
            return []

        for order_id, order_ref in orders:
            try:
                await self.cancel_broker_order(str(order_id))
            except Exception:
                self._logger.exception(
                    "Failed to request cancel of a working order",
                    order_id=order_id,
                    order_ref=order_ref,
                )

        for attempt, delay in enumerate((0.0, *self._cancel_ack_backoff)):
            if delay:
                await asyncio.sleep(delay)
            try:
                live = {
                    str(order.order_ref)
                    for order in await self.list_open_broker_orders()
                }
            except Exception:
                self._logger.exception(
                    "Could not read open broker orders to confirm a cancel",
                    attempt=attempt,
                )
                continue
            pending &= live
            if not pending:
                return []

        self._logger.error(
            "Working orders still live after cancel", order_refs=sorted(pending)
        )
        return sorted(pending)

    async def broker_position(self, con_id: int) -> float:
        """Net quantity the broker reports held for ``con_id``."""
        return await self._executor.broker_position(con_id)

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
                # Only forget orders whose cancel was actually submitted; a
                # failed cancel is still live at IB and must stay tracked
                # so the next attempt retries it.
                del self.open_orders[order_id]
                self._logger.info("Order cancelled", order_id=order_id)
            except Exception:
                self._logger.exception(
                    "Failed to cancel order", order_id=order_id
                )
        if self.open_orders:
            self._logger.error(
                "Orders remain open after cancel-all",
                remaining=list(self.open_orders.keys()),
            )
        return cancelled
