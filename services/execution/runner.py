from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from shared.config import AppConfig
from shared.halt_state import HaltStateRepository
from shared.heartbeat import write_heartbeat
from shared.liquidation import liquidation_exit_id
from shared.logging import get_logger
from shared.models import OrderStatus
from shared.order_ledger import (
    TERMINAL_STATUSES,
    OrderIntentNotFound,
    OrderLedger,
)
from shared.schemas.messages import (
    AlertMessage,
    ApprovedOrderMessage,
    FillMessage,
    KillMessage,
)

APPROVED_ORDERS_STREAM = "stream:approved_orders"
KILLS_STREAM = "stream:kill"
FILLS_STREAM = "stream:fills"
ALERTS_STREAM = "stream:alerts"

CONSUMER_GROUP = "execution_service"
CONSUMER_NAME = "execution_worker_1"

# Backoff between attempts to read the durable halt latch. One entry per
# retry; the first attempt is not delayed. Exhausting the schedule raises
# HaltStateUnavailable — it never degrades into "assume clear".
HALT_LOOKUP_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)


class HaltStateUnavailable(RuntimeError):
    """The durable halt latch could not be read.

    Distinct from both "halted" and "not halted": guessing either way is
    wrong — one silently drops orders, the other submits into a halt. The
    message is retained (no ack, no DLQ) and paged independently.
    """


@dataclass(frozen=True)
class PendingOrderAttribution:
    recommendation_id: str
    portfolio: str | None


@dataclass(frozen=True)
class LocalFillEffect:
    account_id: str
    portfolio: str
    ticker: str
    quantity_delta: float


class ExecutionServiceRunner:
    """Orchestrates the Execution Service.

    Subscribes to ``stream:approved_orders`` and ``stream:kill``.
    Submits orders via :class:`OrderManager`, publishes fills to
    ``stream:fills``, and handles kill events with full liquidation.

    Paper mode toggle via ``config.mode`` — selects the appropriate
    IB port (paper vs live).
    """

    def __init__(
        self,
        config: AppConfig,
        redis_client: Any,
        order_manager: Any,
        order_ledger: OrderLedger | None = None,
    ) -> None:
        self._config = config
        self._redis = redis_client
        self._order_manager = order_manager
        self._order_ledger = order_ledger
        # Direction-aware halt gate. Reuses the ledger's session — execution
        # owns no session of its own — so it is inert when no ledger is
        # injected (tests, and any embedding that runs without durability).
        self._halt_store: HaltStateRepository | None = (
            HaltStateRepository(order_ledger.session)
            if order_ledger is not None
            else None
        )
        self._logger = get_logger("execution_service")
        self._running = False

        # Positions tracked locally (in production loaded from DB)
        self._positions: dict[str, float] = {}
        self._handled_executions: set[tuple[str, str]] = set()
        self._local_fill_effects: dict[
            tuple[str, str], LocalFillEffect
        ] = {}
        self._fill_lock = asyncio.Lock()

        # order_id -> ApprovedOrderMessage, so IB fills can be attributed
        # back to the originating recommendation.
        self._pending_orders: dict[
            str, ApprovedOrderMessage | PendingOrderAttribution
        ] = {}

        # Determine IB port based on mode
        if config.mode == "live":
            self.ib_port = config.ib.live_port
        else:
            self.ib_port = config.ib.paper_port

        # Periodic unfilled-order sweep (cancel stale limits / free reservations).
        # Driven on the reprice interval; needs a market calendar (set by the
        # runner entrypoint) — without one the sweep is skipped.
        self._reprice_interval_seconds = max(
            1, int(config.execution.reprice_interval_minutes) * 60
        )
        self._last_sweep_at: float | None = None
        self._market_calendar: Any = None

    async def setup(self) -> None:
        """Create consumer groups and replay pending messages.

        Messages delivered but not acked before a crash sit in the pending
        entries list and are never re-delivered by the normal ``">"`` read —
        without this replay, an approved order in flight during a restart is
        silently lost.
        """
        self.restore_pending_orders()
        restore_broker = getattr(
            type(self._order_manager), "restore_broker_tracking", None
        )
        if restore_broker is not None:
            await restore_broker(self._order_manager)

        await self._redis.create_consumer_group(
            APPROVED_ORDERS_STREAM, CONSUMER_GROUP
        )
        await self._redis.create_consumer_group(KILLS_STREAM, CONSUMER_GROUP)

        pending_orders = await self._redis.drain_pending(
            APPROVED_ORDERS_STREAM, CONSUMER_GROUP, CONSUMER_NAME
        )
        for msg in pending_orders:
            try:
                order = ApprovedOrderMessage.from_stream_dict(msg.data)
                await self.process_approved_order(order)
                await self._redis.ack(
                    APPROVED_ORDERS_STREAM, CONSUMER_GROUP, msg.message_id
                )
            except HaltStateUnavailable as exc:
                await self._retain_for_unknown_halt_state(
                    APPROVED_ORDERS_STREAM, msg.message_id, exc
                )
            except Exception as exc:
                self._logger.exception(
                    "Error replaying pending order; sending to DLQ",
                    message_id=msg.message_id,
                )
                await self._redis.send_to_dead_letter(
                    APPROVED_ORDERS_STREAM, msg, str(exc)
                )
                await self._redis.ack(
                    APPROVED_ORDERS_STREAM, CONSUMER_GROUP, msg.message_id
                )

        pending_kills = await self._redis.drain_pending(
            KILLS_STREAM, CONSUMER_GROUP, CONSUMER_NAME
        )
        for msg in pending_kills:
            try:
                kill_msg = KillMessage.from_stream_dict(msg.data)
                await self.process_kill(kill_msg)
                await self._redis.ack(KILLS_STREAM, CONSUMER_GROUP, msg.message_id)
            except Exception as exc:
                self._logger.exception(
                    "Error replaying pending kill; sending to DLQ",
                    message_id=msg.message_id,
                )
                await self._redis.send_to_dead_letter(KILLS_STREAM, msg, str(exc))
                await self._redis.ack(KILLS_STREAM, CONSUMER_GROUP, msg.message_id)

        if pending_orders or pending_kills:
            self._logger.warning(
                "Replayed pending messages from a prior crash",
                orders=len(pending_orders),
                kills=len(pending_kills),
            )
        self._logger.info("Execution service consumer groups created")

    def restore_pending_orders(self) -> None:
        """Rebuild execution attribution and idempotency from PostgreSQL."""
        if self._order_ledger is None:
            return
        for intent in self._order_ledger.load_pending_orders():
            if intent.ib_order_id is None:
                continue
            order_id = str(intent.ib_order_id)
            self._pending_orders[order_id] = PendingOrderAttribution(
                recommendation_id=intent.recommendation_id,
                portfolio=intent.portfolio,
            )
            restore = getattr(
                type(self._order_manager), "restore_submission", None
            )
            if restore is not None:
                restore(
                    self._order_manager,
                    intent.recommendation_id,
                    order_id,
                    ticker=intent.symbol,
                    quantity=intent.requested_quantity,
                    limit_price=intent.limit_price,
                )
        self._order_ledger.session.rollback()

    def _commit_ledger(self) -> None:
        if self._order_ledger is not None:
            self._order_ledger.session.commit()

    def _intent_for_order(
        self, order_id: str, *, account_id: str | None = None
    ):
        if self._order_ledger is None:
            return None
        pending = self._pending_orders.get(order_id)
        if pending is not None:
            return self._order_ledger.get(pending.recommendation_id)
        intent = self._order_ledger.get_by_ib_order_id(
            order_id, account_id=account_id
        )
        if intent is not None:
            return intent
        for intent in self._order_ledger.load_pending_orders():
            if str(intent.ib_order_id) == order_id:
                self._pending_orders[order_id] = PendingOrderAttribution(
                    recommendation_id=intent.recommendation_id,
                    portfolio=intent.portfolio,
                )
                return intent
        return None

    async def _load_active_halt(self, store: HaltStateRepository):
        """Read the durable halt latch, retrying transient DB failures.

        Raises :class:`HaltStateUnavailable` once the backoff schedule is
        exhausted. Never returns a guess.
        """
        last_exc: Exception | None = None
        attempts = len(HALT_LOOKUP_RETRY_BACKOFF_SECONDS) + 1
        for attempt in range(attempts):
            try:
                halt = store.load_active_halt(mode=self._config.mode)
            except Exception as exc:
                last_exc = exc
                # The read left the shared session mid-transaction; every
                # later ledger call would fail on it.
                try:
                    store.session.rollback()
                except Exception:
                    self._logger.exception(
                        "Rollback after a failed halt read also failed"
                    )
                self._logger.warning(
                    "Halt-state read failed",
                    attempt=attempt + 1,
                    attempts=attempts,
                    error=str(exc),
                )
                if attempt < len(HALT_LOOKUP_RETRY_BACKOFF_SECONDS):
                    await asyncio.sleep(
                        HALT_LOOKUP_RETRY_BACKOFF_SECONDS[attempt]
                    )
                continue
            store.session.rollback()
            return halt
        raise HaltStateUnavailable(
            f"unable to determine halt state after {attempts} attempts: "
            f"{last_exc}"
        ) from last_exc

    async def _rejected_as_halted(self, order: ApprovedOrderMessage) -> bool:
        """Durably reject ``order`` if the halt latch forbids submitting it.

        Returns True when the order was rejected — the caller must not submit,
        and its message is acked normally.

        Halt enforcement path (design 3A) — the gate blocks exposure-INCREASING
        orders only::

            startup setup()                    per-message loop
                 │                                   │
                 ▼                                   ▼
            PEL replay of approved orders    approved order arrives
                 │                                   │
                 └───────────────┬───────────────────┘
                                 ▼
                      HaltStateRepository check (DB latch)
                                 │
               ┌─────────────────┼──────────────────────┐
               │ halted + BUY    │ halted + ledgered     │ not halted
               ▼                 │ risk-reducing SELL    ▼
          durably reject + ack   └──────────► submit to IB
          (SUBMISSION_FAILED                 (orderRef=rec_id)
           reason=halted; never
           DLQ, never retained)
               │ lookup FAILED (DB down):
               ▼ retain, retry w/ backoff,
               page "unable to determine halt state"

        A halt is exactly when the risk service publishes liquidation sells to
        this stream, so a sell never consults the latch at all — the emergency
        flatten must not be blocked, and must not be blocked by a DB outage
        either. Kill-initiated exits bypass this path structurally
        (:meth:`process_kill` calls ``submit_exit`` directly).
        """
        store, ledger = self._halt_store, self._order_ledger
        if store is None or ledger is None:
            return False
        if order.action != "buy":
            return False
        if await self._load_active_halt(store) is None:
            return False

        self._logger.critical(
            "Rejecting buy: system is halted",
            ticker=order.ticker,
            quantity=order.quantity,
            recommendation_id=order.recommendation_id,
        )
        ledger.transition(
            order.recommendation_id,
            OrderStatus.SUBMISSION_FAILED,
            reason="halted",
        )
        self._commit_ledger()
        # Acked by the caller, not retained: a halt is an operator-attention
        # event, and a buy decided before the incident must not execute after
        # the clear against a market and a book that have both moved. The
        # intent survives as SUBMISSION_FAILED — visible, re-creatable.
        await self._publish_alert(
            event_type="halted_order_rejected",
            priority="high",
            message=(
                f"System halted — rejected {order.action} "
                f"{order.quantity} {order.ticker}"
            ),
            context={
                "ticker": order.ticker,
                "action": order.action,
                "recommendation_id": order.recommendation_id,
            },
        )
        return True

    async def process_approved_order(
        self, order: ApprovedOrderMessage
    ) -> None:
        """Process a single approved order.

        For buy orders: submit a limit entry.
        For sell orders: submit a market exit.

        A ``FillMessage`` is NOT published here — submission is not a fill.
        Fills are published by :meth:`handle_ib_fill` when IB reports actual
        executions (including partials); anything else silently corrupts
        position tracking on rejected, repriced, or partially-filled orders.

        Args:
            order: The approved order message to process.
        """
        self._logger.info(
            "Processing approved order",
            ticker=order.ticker,
            action=order.action,
            quantity=order.quantity,
            recommendation_id=order.recommendation_id,
        )

        order_id: str

        from services.execution.ib_executor import OrderSkippedError

        if self._order_ledger is not None:
            intent = self._order_ledger.get(order.recommendation_id)
            if OrderStatus(intent.status) in TERMINAL_STATUSES:
                self._order_ledger.session.rollback()
                return
            if (
                intent.status
                in {
                    OrderStatus.SUBMITTED.value,
                    OrderStatus.PARTIALLY_FILLED.value,
                }
                and intent.ib_order_id is not None
            ):
                self._pending_orders[str(intent.ib_order_id)] = (
                    PendingOrderAttribution(
                        recommendation_id=intent.recommendation_id,
                        portfolio=intent.portfolio,
                    )
                )
                self._order_ledger.session.rollback()
                return
            # `get()` starts a read transaction. Broker submission is an
            # await point and IB callbacks use this same service-owned
            # session, so end the read before yielding control.
            self._order_ledger.session.rollback()

        # Halt gate — see :meth:`_rejected_as_halted` for the enforcement
        # diagram. Immediately before submission, and on the setup() PEL replay
        # path too: a restart replays orders approved seconds before a halt.
        if await self._rejected_as_halted(order):
            return

        try:
            if order.action == "buy":
                order_id = await self._order_manager.submit_entry(
                    ticker=order.ticker,
                    quantity=order.quantity,
                    limit_price=order.limit_price,
                    recommendation_id=order.recommendation_id,
                )
            else:
                order_id = await self._order_manager.submit_exit(
                    ticker=order.ticker,
                    quantity=order.quantity,
                    recommendation_id=order.recommendation_id,
                )
        except OrderSkippedError as exc:
            # Not a failure: the order cannot be sized on this account
            # (e.g. sub-1-share on a no-fractional account). Ack and move on.
            self._logger.warning(
                "Order skipped",
                ticker=order.ticker,
                action=order.action,
                quantity=order.quantity,
                reason=str(exc),
            )
            if self._order_ledger is not None:
                self._order_ledger.transition(
                    order.recommendation_id,
                    OrderStatus.SUBMISSION_FAILED,
                    reason=str(exc),
                )
                self._commit_ledger()
            return
        except Exception as exc:
            if self._order_ledger is not None:
                self._order_ledger.transition(
                    order.recommendation_id,
                    OrderStatus.SUBMISSION_FAILED,
                    reason=str(exc),
                )
                self._commit_ledger()
                return
            raise

        broker_active = True
        if self._order_ledger is not None:
            try:
                intent = self._order_ledger.record_submission(
                    order.recommendation_id, order_id
                )
                portfolio = intent.portfolio
                self._commit_ledger()
            except Exception:
                self._order_ledger.session.rollback()
                raise
            reconcile = getattr(
                type(self._order_manager), "reconcile_submission", None
            )
            if reconcile is not None:
                broker_active = await reconcile(
                    self._order_manager,
                    order.recommendation_id,
                    order_id,
                )
        else:
            intent = order
            portfolio = order.portfolio

        # Remember the order so fills can be attributed back to the
        # recommendation that caused them.
        if self._order_ledger is None:
            self._pending_orders[order_id] = order
        elif broker_active:
            self._pending_orders[order_id] = PendingOrderAttribution(
                recommendation_id=order.recommendation_id,
                portfolio=portfolio,
            )

        self._logger.info(
            "Order submitted, awaiting fill",
            order_id=order_id,
            ticker=order.ticker,
            action=order.action,
        )

    async def handle_ib_fill(self, fill_info: dict[str, Any]) -> None:
        """Publish a ``FillMessage`` for a real IB execution.

        Registered as the executor's fill handler; invoked once per IB fill
        (partial fills produce one call each) with actual execution price,
        quantity, and commission.

        Args:
            fill_info: Payload from :class:`IBExecutor` — order_id, ticker,
                side, quantity, fill_price, original commission amount and
                currency, USD trading commission, conversion rate, stable
                broker execution identity/timestamp, and order_done.
        """
        account_id = fill_info.get("account_id")
        execution_id = fill_info.get("execution_id")
        execution_key = (
            (str(account_id), str(execution_id))
            if account_id and execution_id
            else None
        )
        async with self._fill_lock:
            duplicate = (
                execution_key in self._handled_executions
                if execution_key is not None
                else False
            )
            if (
                not duplicate
                and execution_key is not None
                and self._order_ledger is not None
            ):
                duplicate = self._order_ledger.execution_fill_exists(
                    *execution_key
                )
            if duplicate:
                self._reconcile_managed_positions()
                self._logger.info(
                    "Duplicate IB execution ignored",
                    account_id=account_id,
                    execution_id=execution_id,
                )
                return

            local_effect = await self._handle_ib_fill_once(fill_info)
            if execution_key is not None:
                self._handled_executions.add(execution_key)
                if local_effect is not None:
                    self._local_fill_effects[execution_key] = local_effect

    async def _handle_ib_fill_once(
        self, fill_info: dict[str, Any]
    ) -> LocalFillEffect | None:
        order_id = fill_info["order_id"]
        pending = self._pending_orders.get(order_id)
        intent = self._intent_for_order(
            order_id, account_id=fill_info.get("account_id")
        )
        attribution = intent or pending

        fill = FillMessage(
            ticker=fill_info["ticker"],
            timestamp=fill_info["timestamp"],
            side=fill_info["side"],
            quantity=fill_info["quantity"],
            cumulative_quantity=fill_info.get("cumulative_quantity"),
            fill_price=fill_info["fill_price"],
            commission=fill_info.get("commission", 0.0),
            commission_currency=fill_info.get("commission_currency"),
            commission_trading=fill_info.get("commission_trading"),
            commission_fx_base_per_trading=fill_info.get(
                "commission_fx_base_per_trading"
            ),
            recommendation_id=(
                attribution.recommendation_id if attribution else "unknown"
            ),
            order_id=order_id,
            execution_id=fill_info.get("execution_id"),
            account_id=fill_info.get("account_id"),
            portfolio=getattr(attribution, "portfolio", None),
            con_id=fill_info.get("con_id"),
            exchange=fill_info.get("exchange"),
            currency=fill_info.get("currency"),
            order_done=bool(fill_info.get("order_done", False)),
        )
        local_effect = None
        if fill.account_id and fill.portfolio:
            quantity_delta = float(fill.quantity)
            if fill.side.lower() != "buy":
                quantity_delta = -quantity_delta
            local_effect = LocalFillEffect(
                account_id=fill.account_id,
                portfolio=fill.portfolio,
                ticker=fill.ticker,
                quantity_delta=quantity_delta,
            )
        if self._order_ledger is not None:
            # Fill publication awaits Redis. End the read-only SQLAlchemy
            # transaction first so status callbacks never share an active
            # transaction while this coroutine is suspended.
            self._order_ledger.session.rollback()
        await self._redis.publish(FILLS_STREAM, fill.to_stream_dict())

        self._logger.info(
            "Fill published",
            order_id=order_id,
            ticker=fill_info["ticker"],
            side=fill_info["side"],
            quantity=fill_info["quantity"],
            fill_price=fill_info["fill_price"],
            order_done=fill_info.get("order_done", False),
        )

        # Keep local position view current so a kill liquidates accurately.
        ticker = fill_info["ticker"]
        delta = fill_info["quantity"]
        if fill_info["side"] == "buy":
            self._positions[ticker] = self._positions.get(ticker, 0) + delta
        else:
            remaining = self._positions.get(ticker, 0) - delta
            if remaining > 0:
                self._positions[ticker] = remaining
            else:
                self._positions.pop(ticker, None)

        # Once IB reports the order complete, it is no longer pending or open.
        if fill_info.get("order_done"):
            self._pending_orders.pop(order_id, None)
            self._order_manager.open_orders.pop(order_id, None)
        return local_effect

    def _reconcile_managed_positions(self) -> None:
        """Overlay unprojected local fills on durable managed positions."""
        if self._order_ledger is None:
            return

        try:
            durable_positions, projected_keys = (
                self._order_ledger.managed_position_snapshot(
                    self._local_fill_effects
                )
            )
            reconciled: dict[str, float] = {}
            for ticker, quantity in durable_positions:
                reconciled[ticker] = reconciled.get(ticker, 0.0) + quantity

            for execution_key in projected_keys:
                self._local_fill_effects.pop(execution_key, None)
            for effect in self._local_fill_effects.values():
                reconciled[effect.ticker] = (
                    reconciled.get(effect.ticker, 0.0)
                    + effect.quantity_delta
                )

            self._positions = {
                ticker: quantity
                for ticker, quantity in reconciled.items()
                if quantity > 0
            }
        except Exception:
            self._logger.exception(
                "Unable to reconcile managed positions; using local cache"
            )
        finally:
            self._order_ledger.session.rollback()

    async def _retain_for_unknown_halt_state(
        self, stream: str, message_id: str, exc: Exception
    ) -> None:
        """Leave a message in the PEL and page.

        Neither an ack nor a dead-letter: an unreadable halt latch is an
        infrastructure fault, not a poison message. The entry stays in the
        pending list so the next ``setup()`` replay retries it once the DB is
        reachable, and the alert is independent of the halt itself so it fires
        even when nothing else can tell the operator anything.
        """
        self._logger.error(
            "Unable to determine halt state; retaining order for retry",
            stream=stream,
            message_id=str(message_id),
            error=str(exc),
        )
        await self._publish_alert(
            event_type="halt_state_unavailable",
            priority="critical",
            message=(
                "Unable to determine halt state; order retained unsubmitted "
                f"on {stream}: {exc}"
            ),
            context={"stream": stream, "message_id": str(message_id)},
        )

    async def _publish_alert(
        self,
        *,
        event_type: str,
        priority: str,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Publish an alert to the alerts stream."""
        alert = AlertMessage(
            timestamp=datetime.now(timezone.utc),
            event_type=event_type,
            priority=priority,
            message=message,
            context=context or {},
        )
        await self._redis.publish(ALERTS_STREAM, alert.to_stream_dict())

    async def handle_ib_order_status(
        self, status_info: dict[str, Any]
    ) -> None:
        """Persist terminal broker statuses before returning to IB callbacks."""
        order_id = str(status_info["order_id"])
        intent = self._intent_for_order(order_id)
        if intent is None:
            self._logger.warning(
                "IB status received for unattributed order",
                order_id=order_id,
                status=status_info.get("status"),
            )
            if self._order_ledger is not None:
                self._order_ledger.session.rollback()
            return

        # A late or duplicate broker status can arrive after the fill projector
        # (or a prior status) already terminalized the intent. Transitioning out
        # of a terminal state raises InvalidOrderTransition; ignore it instead.
        if OrderStatus(intent.status) in TERMINAL_STATUSES:
            self._logger.info(
                "Ignoring IB status for already-terminal intent",
                order_id=order_id,
                status=status_info.get("status"),
                current=intent.status,
            )
            self._order_ledger.session.rollback()
            return

        broker_status = str(status_info.get("status", ""))
        reason = status_info.get("reason") or None
        if (
            broker_status == "Inactive"
            and status_info.get("completed_order_confirmed") is True
            and reason is None
        ):
            reason = "IB completed order is Inactive"
        target: OrderStatus | None = None
        if status_info.get("order_absent_at_ib") is True:
            # Order vanished from IB after a session boundary (see
            # IBExecutor.restore_order_by_ref). Terminalize to EXPIRED whether or
            # not it partially filled — any filled shares are already recorded
            # and are preserved by the transition.
            target = OrderStatus.EXPIRED
        elif broker_status in {"Cancelled", "ApiCancelled"}:
            target = OrderStatus.CANCELLED
        elif broker_status == "Inactive" and reason:
            target = (
                OrderStatus.CANCELLED
                if (
                    intent.filled_quantity > 0
                    or float(status_info.get("filled_quantity", 0.0) or 0.0) > 0
                )
                else OrderStatus.SUBMISSION_FAILED
            )
        elif (
            broker_status == "Filled"
            and status_info.get("completed_order_confirmed") is True
        ):
            target = OrderStatus.FILLED
        elif (
            broker_status == "Expired"
            and intent.filled_quantity == 0
            and status_info.get("completed_order_confirmed") is True
        ):
            target = OrderStatus.EXPIRED

        if target is None:
            self._order_ledger.session.rollback()
            return
        try:
            self._order_ledger.transition(
                intent.recommendation_id, target, reason=reason
            )
            self._commit_ledger()
        except Exception as exc:
            # Never let a transition error escape into the fire-and-forget IB
            # callback task (it would be swallowed and leave the shared session
            # mid-transaction). Roll back and alert instead.
            self._order_ledger.session.rollback()
            self._logger.exception(
                "Failed to persist IB order status",
                order_id=order_id,
                target=target.value,
            )
            await self._publish_alert(
                event_type="order_status_persist_failed",
                priority="high",
                message=(
                    f"Could not persist status {target.value} for order "
                    f"{order_id}: {exc}"
                ),
                context={"order_id": order_id, "target": target.value},
            )
            return
        self._pending_orders.pop(order_id, None)
        self._order_manager.open_orders.pop(order_id, None)

    async def process_kill(self, kill_msg: KillMessage) -> None:
        """Process a kill event: cancel all open orders and liquidate positions.

        Args:
            kill_msg: The kill message with reason and trigger info.
        """
        self._logger.critical(
            "Kill event received — cancelling all orders and liquidating",
            reason=kill_msg.reason,
            triggered_by=kill_msg.triggered_by,
        )

        async with self._fill_lock:
            self._reconcile_managed_positions()
            positions_to_liquidate = dict(self._positions)

        # Cancel all open orders
        await self._order_manager.cancel_all_orders()

        # Deterministic per-kill epoch: exits for one kill converge on the same
        # ids across a replay and across the risk-side authoritative path, so we
        # never double-sell.
        epoch = int(kill_msg.timestamp.timestamp())
        liquidated = 0
        for ticker, quantity in positions_to_liquidate.items():
            if quantity <= 0:
                continue
            exit_id = liquidation_exit_id(self._config.mode, ticker, epoch)
            try:
                if self._exit_already_in_flight(exit_id):
                    self._logger.info(
                        "Kill exit already in flight; skipping",
                        ticker=ticker,
                        recommendation_id=exit_id,
                    )
                    continue
                await self._order_manager.submit_exit(
                    ticker=ticker,
                    quantity=quantity,
                    recommendation_id=exit_id,
                )
                liquidated += 1
                self._logger.info(
                    "Kill liquidation order submitted",
                    ticker=ticker,
                    quantity=quantity,
                    recommendation_id=exit_id,
                )
            except Exception:
                # One position failing (e.g. IB disconnected mid-loop) must not
                # abort the rest, and the critical alert below must still fire.
                self._logger.exception(
                    "Kill liquidation failed for a position; continuing",
                    ticker=ticker,
                )

        # Always publish the critical alert — even on zero positions or partial
        # failure, the operator must learn the kill fired.
        alert = AlertMessage(
            timestamp=datetime.now(timezone.utc),
            event_type="kill_switch_liquidation",
            priority="critical",
            message=f"Kill switch activated by {kill_msg.triggered_by}: {kill_msg.reason}",
            context={
                "triggered_by": kill_msg.triggered_by,
                "positions_seen": len(positions_to_liquidate),
                "positions_liquidated": liquidated,
            },
        )
        await self._redis.publish(ALERTS_STREAM, alert.to_stream_dict())

    def _exit_already_in_flight(self, exit_id: str) -> bool:
        """True if an intent for this deterministic exit id is already submitted
        or terminal — the risk-side authoritative path (or a prior pass/replay)
        has it, so this defense-in-depth net defers to avoid a double-sell."""
        if self._order_ledger is None:
            return False
        try:
            intent = self._order_ledger.get(exit_id)
            in_flight = OrderStatus(intent.status) in TERMINAL_STATUSES or intent.status in {
                OrderStatus.SUBMITTED.value,
                OrderStatus.PARTIALLY_FILLED.value,
            }
        except OrderIntentNotFound:
            in_flight = False
        finally:
            self._order_ledger.session.rollback()
        return in_flight

    async def maybe_run_unfilled_sweep(self, now: float) -> bool:
        """Run the unfilled-order sweep when the reprice interval has elapsed.

        ``now`` is a monotonic timestamp (seconds). No-ops without a market
        calendar. Execution has no live quote feed, so the sweep cancels stale
        limits / frees reservations rather than repricing (see
        OrderManager.sweep_unfilled_orders). Returns True when it ran.
        """
        if self._market_calendar is None:
            return False
        last = self._last_sweep_at
        if last is not None and (now - last) < self._reprice_interval_seconds:
            return False
        self._last_sweep_at = now
        await self._order_manager.sweep_unfilled_orders(
            {}, self._market_calendar
        )
        return True

    async def shutdown(self) -> None:
        """Graceful shutdown: cancel all open orders to avoid orphans."""
        self._logger.info("Execution service shutting down")
        self._running = False
        await self._order_manager.cancel_all_orders()
        self._logger.info("Execution service shutdown complete — no orphaned orders")

    async def run(self) -> None:
        """Main event loop: read from streams and dispatch.

        Runs until ``self._running`` is set to ``False`` or a
        ``KeyboardInterrupt`` / ``asyncio.CancelledError`` is raised.
        """
        await self.setup()
        self._running = True

        self._logger.info(
            "Execution service started",
            mode=self._config.mode,
            ib_port=self.ib_port,
        )

        try:
            while self._running:
                # T6: heartbeat for the container healthcheck — see docker-compose.yml.
                write_heartbeat()
                # Periodic unfilled-order sweep (best-effort — never tear down
                # the loop on a sweep failure).
                try:
                    await self.maybe_run_unfilled_sweep(
                        asyncio.get_running_loop().time()
                    )
                except Exception:
                    self._logger.exception("Unfilled-order sweep failed; continuing")

                await self._consume_and_process(
                    APPROVED_ORDERS_STREAM,
                    ApprovedOrderMessage.from_stream_dict,
                    self.process_approved_order,
                    count=10,
                    block_ms=2000,
                )
                await self._consume_and_process(
                    KILLS_STREAM,
                    KillMessage.from_stream_dict,
                    self.process_kill,
                    count=1,
                    block_ms=500,
                )

        except (KeyboardInterrupt, Exception):
            self._logger.info("Execution service interrupted")
        finally:
            await self.shutdown()

    async def _consume_and_process(
        self,
        stream: str,
        parser: Any,
        handler: Any,
        *,
        count: int,
        block_ms: int,
    ) -> None:
        """Read a batch and process each message, dead-lettering poison messages
        (DLQ + ack + alert) instead of leaving them parked in the PEL."""
        messages = await self._redis.read_group(
            stream, CONSUMER_GROUP, CONSUMER_NAME, count=count, block_ms=block_ms
        )
        for msg in messages:
            try:
                await handler(parser(msg.data))
            except HaltStateUnavailable as exc:
                await self._retain_for_unknown_halt_state(
                    stream, msg.message_id, exc
                )
                continue
            except Exception as exc:
                self._logger.exception(
                    "Poison message; sending to DLQ",
                    stream=stream,
                    message_id=msg.message_id,
                )
                try:
                    await self._redis.send_to_dead_letter(stream, msg, str(exc))
                    await self._redis.ack(stream, CONSUMER_GROUP, msg.message_id)
                except Exception:
                    self._logger.exception(
                        "Failed to dead-letter poison message",
                        stream=stream,
                        message_id=msg.message_id,
                    )
                await self._publish_alert(
                    event_type="poison_message",
                    priority="high",
                    message=f"Poison message on {stream} dead-lettered: {exc}",
                    context={"stream": stream, "message_id": str(msg.message_id)},
                )
                continue
            # A transient ack failure after a successful handler must not
            # dead-letter an already-processed message.
            try:
                await self._redis.ack(stream, CONSUMER_GROUP, msg.message_id)
            except Exception:
                self._logger.exception(
                    "Ack failed after processing; relying on redelivery",
                    stream=stream,
                    message_id=msg.message_id,
                )


if __name__ == "__main__":
    import asyncio

    from shared.config import load_config

    config = load_config("config/default.yaml")

    async def main() -> None:
        import redis.asyncio as aioredis

        from services.execution.ib_executor import IBExecutor
        from services.execution.order_manager import OrderManager
        from shared.heartbeat import register_heartbeat_collector
        from shared.observability import setup_metrics
        from shared.order_ledger import OrderLedger
        from shared.redis_client import RedisStreamClient

        setup_metrics("execution", port=config.observability.prometheus_port)
        register_heartbeat_collector()

        redis_conn = aioredis.from_url(config.redis.url)
        redis_client = RedisStreamClient(redis_conn)
        executor = IBExecutor(
            host=config.ib.host,
            port=config.ib.paper_port if config.mode != "live" else config.ib.live_port,
            client_id=config.ib.client_id,
            allow_fractional=config.execution.fractional_orders,
            account_id=config.ib.account_id,
        )
        # Connect BEFORE consuming orders. A failed connect exits nonzero so
        # the container restart policy retries; running without IB would
        # consume approved orders while executing nothing. expect_paper
        # refuses a LIVE Gateway session answering on the paper port.
        await executor.connect(expect_paper=(config.mode != "live"))

        # Load real holdings so a kill event liquidates actual positions.
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from shared.position_loader import load_open_positions

        engine = create_engine(config.database.url)
        session = sessionmaker(bind=engine)()
        order_manager = OrderManager(
            executor=executor,
            redis_client=redis_client,
            db_session=session,
            reprice_interval_minutes=config.execution.reprice_interval_minutes,
            max_reprice_attempts=config.execution.max_reprice_attempts,
        )
        runner = ExecutionServiceRunner(
            config=config,
            redis_client=redis_client,
            order_manager=order_manager,
            order_ledger=OrderLedger(session),
        )
        # Wire the market calendar so the periodic unfilled-order sweep runs.
        from shared.market_calendar import MarketCalendar

        runner._market_calendar = MarketCalendar()
        try:
            positions = load_open_positions(session)
            runner._positions = {
                ticker: p["quantity"] for ticker, p in positions.items()
            }
            executor.set_fill_handler(runner.handle_ib_fill)
            executor.set_order_status_handler(runner.handle_ib_order_status)
            await runner.run()
        finally:
            session.close()
            await executor.disconnect()

    asyncio.run(main())
