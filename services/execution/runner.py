from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from services.execution.broker_stops import BrokerStopManager
from services.execution.order_manager import (
    KillProbe,
    OrderPlacedElsewhereError,
    SubmissionDeferredError,
)
from shared.config import AppConfig
from shared.halt_state import HaltStateRepository
from shared.heartbeat import write_heartbeat
from shared.liquidation import liquidation_exit_id
from shared.logging import get_logger
from shared.models import OrderStatus
from shared.order_ledger import (
    BROKER_STOP_ORDER_TYPE,
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
#: Consecutive failed execution-sweep passes before paging (KAN-95). One or
#: two are a Gateway blip the next pass absorbs; three at the 15-minute
#: default is 45 minutes of no recovery.
EXECUTION_SWEEP_FAILURE_PAGE_AFTER = 3
#: Consecutive resolution passes an order may be held back (deferred on an
#: unbooked served fill, or retried after a failure) before it is paged once
#: (KAN-106). Four at the 15-minute sweep interval is an hour.
ABSENT_HOLDBACK_PAGE_AFTER = 4
#: Quiet period before fills the live path booked from IB's execution record
#: are paged, when no sweep pass pages them (KAN-102). A Gateway restart that
#: dropped N fills replays them in one burst; the page waits for the burst to
#: settle and lists them all, instead of paging once per fill.
RECOVERY_PAGE_SETTLE_SECONDS = 10.0
ALERTS_STREAM = "stream:alerts"

CONSUMER_GROUP = "execution_service"
CONSUMER_NAME = "execution_worker_1"

# Backoff between attempts to read the durable halt latch. One entry per
# retry; the first attempt is not delayed. Exhausting the schedule raises
# HaltStateUnavailable — it never degrades into "assume clear".
HALT_LOOKUP_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)

# How often an approved order whose idempotency probe IB did not answer is
# retried (KAN-112). Its message stays unacked in the PEL meanwhile, so a
# restart replays it too. Each retry costs at most the executor's bounded
# open/completed-order requests, and a pass stops at the first one IB still
# does not answer — the loop also has the kill stream to read.
DEFERRED_ORDER_RETRY_SECONDS: float = 30.0
# How often one another IB client has working is re-probed (KAN-112). Slow:
# that order is an answer, not an outage, and it goes away when a human (or
# the market) finishes it — at which point the order is submitted (or, if IB
# lists it as completed, adopted) without anyone having to re-drive it.
SUBMITTED_ELSEWHERE_RETRY_SECONDS: float = 300.0
# A deferred order logs a warning at most this often; the passes between log
# at info.
DEFERRED_WARNING_INTERVAL_SECONDS: float = 600.0
# Reason a deferred BUY is terminalized with once the session it was sized
# for has closed: a stale-priced entry is never placed hours later.
DEFERRED_PAST_SESSION_REASON = "deferred past session"

# Cadence of the post-halt reconcile sweep while a halt is active. The sweep
# fires immediately the first time a halt is seen, so this only bounds how
# long an order placed *after* that first pass can sit live at the broker.
HALT_SWEEP_INTERVAL_SECONDS: float = 30.0

# What marks a sell on ``stream:approved_orders`` as a risk-side exit, and so
# subject to the oversell guard (KAN-10) — one key per control that risk's
# exit emitter can publish. An ordinary sleeve sell carries none of them: it is
# sized on the recommendation path and is out of scope by design, and
# cancelling a working BUY under a routine rotation would be a behaviour change
# nobody asked for.
#
# Two things keep the cross-service coupling honest. The stronger one is that
# risk's single emitter *defaults* to ``kill_switch``, so an exit that passes
# no adjustments at all is still labelled. The other is
# tests/services/risk_management/test_exit_emitter.py, which parses the risk
# service for every ``risk_adjustments={...}`` literal it publishes and fails
# if one carries no key listed here — literals only, so a future control that
# builds its adjustments in a variable would slip past it. An unguarded
# risk-side sell is logged either way (see :meth:`_guards_oversell`).
RISK_EXIT_ADJUSTMENT_KEYS: frozenset[str] = frozenset(
    {"kill_switch", "stop_loss", "passive_trim", "republished"}
)

# The subset that flattens a position rather than shaving it. Only these
# cancel working BUYs: a passive trim is a rebalance back to the soft target,
# a few shares against a position that stays open, so cancelling an unrelated
# sleeve's entry under it would destroy a legitimate order to no purpose. The
# quantity cap still applies to every risk exit.
FLATTENING_EXIT_ADJUSTMENT_KEYS: frozenset[str] = (
    RISK_EXIT_ADJUSTMENT_KEYS - {"passive_trim"}
)

# Backoff between attempts to read the broker's position, mirroring
# HALT_LOOKUP_RETRY_BACKOFF_SECONDS. The first attempt is not delayed. A
# dropped API socket is routine on this Gateway, and one transient reconnect
# race must not be enough to refuse an emergency flatten.
BROKER_POSITION_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0)

# Quantities are floats (IBKR Share Slices), so every comparison the guard
# makes needs slack: a broker read of 49.9999 against a 50-share exit would
# otherwise "cap" to 49.9999, which whole-share truncation then places as 49 —
# an emergency flatten that leaves a share behind. Same value the risk service
# uses for the same reason.
EXIT_QUANTITY_EPSILON = 1e-6


class HaltStateUnavailable(RuntimeError):
    """The durable halt latch could not be read.

    Distinct from both "halted" and "not halted": guessing either way is
    wrong — one silently drops orders, the other submits into a halt. The
    message is retained (no ack, no DLQ) and paged independently.
    """


@dataclass
class DeferredMessage:
    """An approved-order message held unacked for a later attempt (KAN-112).

    ``kind`` is ``"unanswered"`` (IB did not say whether an order exists) or
    ``"elsewhere"`` (another client has one working).
    """

    message_id: str
    stream: str
    msg: Any
    parser: Any
    handler: Any
    recommendation_id: str
    kind: str
    due_at: float
    attempts: int = 0
    last_warned_at: float | None = None


@dataclass(frozen=True)
class PendingOrderAttribution:
    recommendation_id: str
    portfolio: str | None


@dataclass(frozen=True)
class HaltSweepAction:
    """What the post-halt sweep decided about one live broker order."""

    order_id: str
    order_ref: str
    ticker: str
    quantity: float
    cancel: bool
    record_submission: bool


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

        # IB liveness (KAN-94). Reconnecting used to be lazy — only an executor
        # call healed a dropped session — so a Gateway restart with no order in
        # flight left fills going to a dead socket until the next order.
        self._ib_liveness_interval_seconds = int(
            config.execution.ib_liveness_interval_seconds
        )
        self._ib_disconnect_alert_seconds = int(
            config.execution.ib_disconnect_alert_seconds
        )
        self._last_ib_liveness_at: float | None = None
        self._ib_disconnected_since: float | None = None
        self._ib_disconnect_alerted = False

        # In-service execution sweep (KAN-95). Replaces the 04:15 sweep in
        # run_paper.py, which read executions on another client id hours
        # after IB's execution day had rolled and never recovered a fill.
        self._execution_sweep_interval_seconds = (
            int(config.execution.execution_sweep_interval_minutes) * 60
        )
        self._last_execution_sweep_at: float | None = None
        self._last_execution_sweep_generation: int | None = None
        # The sweep runs as a background task (KAN-98): a hung reqExecutions
        # must never hold stream:kill unread for its 30 s timeout.
        self._execution_sweep_task: asyncio.Task | None = None
        # Paging state: a run of failed passes pages once at the threshold,
        # and an untracked broker order pages once per order id.
        self._execution_sweep_failures = 0
        self._execution_sweep_untracked_paged: set[str] = set()
        # Fills the live path booked from IB's execution record, not yet
        # paged (KAN-102). The next sweep pass pages them with its own; a
        # burst no pass picks up is paged once it settles.
        self._unpaged_recoveries: list[FillMessage] = []
        self._last_recovery_queued_at: float | None = None
        # Resolving orders IB no longer knows after a reconnect (KAN-106).
        # A fresh IB session (the executor's session generation; a
        # same-socket 1101 does not count) makes it pending. A sweep pass that
        # started after it, succeeded, and ended on the same connection
        # generation makes it ready, so a fill IB still serves is booked
        # before any expiry. One attempt per successful pass: a failure waits
        # for the next interval or reconnect, not the next loop.
        self._absent_resolution_pending = False
        self._absent_resolution_ready = False
        # The connection generation the ready pass swept on; the resolution
        # stops the moment the session moves off it.
        self._absent_resolution_generation: int | None = None
        self._last_session_generation: int | None = None
        # Consecutive passes each order has been held back, and the ones
        # already paged for it.
        self._absent_holdback_passes: dict[str, int] = {}
        self._absent_holdback_paged: set[str] = set()

        # Approved orders not submitted because IB did not answer the
        # idempotency probe, or another client has them working (KAN-112),
        # keyed by message id. Unacked, so they are also in the PEL for a
        # restart; each is retried when due, until it settles.
        self._deferred_messages: dict[str, DeferredMessage] = {}
        self._deferred_retry_interval_seconds = DEFERRED_ORDER_RETRY_SECONDS
        self._elsewhere_retry_interval_seconds = SUBMITTED_ELSEWHERE_RETRY_SECONDS
        self._deferred_warning_interval_seconds = DEFERRED_WARNING_INTERVAL_SECONDS
        # Exits already paged for being withheld — once per episode.
        self._deferred_exit_paged: set[str] = set()
        # The calendar a deferred BUY's session close is read from when the
        # entrypoint has not wired one (tests, embeddings).
        self._deferral_calendar: Any = None
        # KAN-112: the order manager pages, once per recommendation, when
        # another IB client already has the order working. Probed on the type
        # like restore_broker_tracking, so stand-in order managers are left
        # alone.
        set_elsewhere_handler = getattr(
            type(order_manager), "set_submitted_elsewhere_handler", None
        )
        if set_elsewhere_handler is not None:
            set_elsewhere_handler(order_manager, self._alert_submitted_elsewhere)

        # Post-halt reconcile sweep (KAN-13). Its own timer, and deliberately
        # NOT sharing the unfilled sweep's calendar gate: that sweep returns
        # False whenever `_market_calendar` is unset, and a halt-safety path
        # must not inherit a known "silently does nothing" failure mode.
        self._last_halt_sweep_at: float | None = None
        # (order_id, event_type) already alerted during the current halt —
        # the sweep re-runs every interval and must not re-page each pass.
        self._halt_sweep_alerted: set[tuple[str, str]] = set()

        # Broker-native protective stops (KAN-19). Needs the ledger: an
        # unledgered resting stop reads as a `major` reconciliation divergence
        # and disables entries for the session, so without durability there is
        # no safe way to place one.
        self._broker_stops: BrokerStopManager | None = None
        # The KAN-20 verification scan, on the risk service's passive-scan
        # cadence. It runs here rather than there because verifying a broker
        # stop takes a broker connection, which only this service has. The
        # risk scan keeps emitting software stop-loss exits either way — with
        # stops resting, KAN-10's outstanding-sell guard already sizes those
        # to zero, so the software path is the fallback the design intends.
        self._stop_verification_interval_seconds = 1800
        self._last_stop_verification_at: float | None = None
        if order_ledger is not None and config.execution.broker_stops_enabled:
            if config.ib.account_id is None:
                # Unpinned, the startup backfill would walk every open position
                # in the database — including another account's — and place
                # protective sells for shares this session does not hold. The
                # ledger would record the position's account while IB stamped
                # the session's, which reconciliation reads as a mismatch and
                # answers by disabling entries.
                raise ValueError(
                    "broker_stops_enabled requires ib.account_id to be set: "
                    "protective stops must name the account they protect"
                )
            self._broker_stops = BrokerStopManager(
                order_manager=order_manager,
                order_ledger=order_ledger,
                mode=config.mode,
                account_id=config.ib.account_id,
                trailing_pct=config.risk.stop_loss_trailing_pct,
                enabled=True,
                tif=config.execution.broker_stops_tif,
                outside_rth=config.execution.broker_stops_outside_rth,
                whole_shares=not config.execution.fractional_orders,
                on_placement_failed=self._alert_stop_not_placed,
                on_drift_detected=self._alert_stop_drift,
            )
            self._stop_verification_interval_seconds = max(
                1, int(config.risk.passive_scan_interval_minutes) * 60
            )

    async def setup(self) -> None:
        """Create consumer groups and replay pending messages.

        Messages delivered but not acked before a crash sit in the pending
        entries list and are never re-delivered by the normal ``">"`` read —
        without this replay, an approved order in flight during a restart is
        silently lost.
        """
        self.restore_pending_orders()
        # Before the rebind: commission reports trailing connect's own
        # execution sync fire on a rebound trade too (KAN-102).
        self._seed_booked_executions()
        restore_broker = getattr(
            type(self._order_manager), "restore_broker_tracking", None
        )
        if restore_broker is not None:
            degraded = await restore_broker(self._order_manager)
            if isinstance(degraded, (list, tuple, set)) and degraded:
                await self._start_with_tracking_degraded(
                    [str(order_id) for order_id in degraded]
                )

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
            except (SubmissionDeferredError, OrderPlacedElsewhereError) as exc:
                self._defer_message(
                    APPROVED_ORDERS_STREAM,
                    msg,
                    ApprovedOrderMessage.from_stream_dict,
                    self.process_approved_order,
                    exc,
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

        # After the replay, so a position opened by a replayed order is covered
        # by the same pass rather than waiting for the next restart.
        await self.backfill_broker_stops()

        if pending_orders or pending_kills:
            self._logger.warning(
                "Replayed pending messages from a prior crash",
                orders=len(pending_orders),
                kills=len(pending_kills),
            )
        self._logger.info("Execution service consumer groups created")

    async def _start_with_tracking_degraded(self, order_ids: list[str]) -> None:
        """Start anyway when IB would not say what became of restored orders.

        Refusing to start is the 2026-08 restart deadlock: a crash-looping
        execution books no fills, places no stops and reads no kill. So the
        service starts, pages, and hands the orders to the post-reconnect
        resolution, which runs after the first successful execution sweep —
        that pass both books any fill IB serves and makes it ready. Not ready
        now: resolving before a sweep is exactly the pre-emption KAN-106
        orders against. New submissions are unaffected: they are idempotent
        by recommendation id, and the sweep books fills the unbound callbacks
        miss.
        """
        self._absent_resolution_pending = True
        self._absent_resolution_ready = False
        open_orders = getattr(self._order_manager, "open_orders", None)
        if not isinstance(open_orders, dict):
            open_orders = {}
        described = []
        recommendation_ids = []
        for order_id in order_ids:
            attribution = self._pending_orders.get(order_id)
            recommendation_id = str(
                getattr(attribution, "recommendation_id", None) or "unknown"
            )
            info = open_orders.get(order_id)
            ticker = (
                str(info.get("ticker") or "?") if isinstance(info, dict) else "?"
            )
            recommendation_ids.append(recommendation_id)
            described.append(f"{order_id} ({ticker}, {recommendation_id})")
        await self._publish_alert_best_effort(
            event_type="order_tracking_degraded",
            priority="high",
            message=(
                "Execution started with order tracking DEGRADED: IB did not "
                "answer its open/completed-order requests at startup, so "
                f"{len(order_ids)} restored order(s) — {'; '.join(described)} "
                "— have no live callbacks yet. They are resolved after the "
                "first successful execution sweep, which also books any fill "
                "IB serves; until then reconciliation may block entries. "
                "Check the Gateway if this page repeats."
            ),
            context={
                "order_ids": ",".join(order_ids),
                "recommendation_ids": ",".join(recommendation_ids),
            },
        )

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
                    # Only for a stop. Risk stamps every exit "market", and
                    # passing that through would newly exempt restored exits
                    # from the unfilled sweep — a behaviour change in a path
                    # this feature is not supposed to touch (KAN-19 AC4).
                    order_type=(
                        intent.order_type
                        if str(intent.order_type).lower()
                        == BROKER_STOP_ORDER_TYPE
                        else None
                    ),
                )
        self._order_ledger.session.rollback()

    def _seed_booked_executions(self) -> None:
        """Tell the executor which executions of restored orders are booked.

        After a restart the first sweep's ``reqExecutions`` replays every
        execution of a restored, partly filled order through its live
        callback. The ones the book already holds are dropped at the executor
        rather than handed over stamped as recovered (KAN-102, window c).
        Best-effort: without the seed the runner's dedupe still drops them.
        """
        if self._order_ledger is None or not self._pending_orders:
            return
        mark = getattr(type(self._order_manager), "mark_executions_booked", None)
        if mark is None:
            return
        try:
            booked = self._order_ledger.booked_execution_ids(self._pending_orders)
            mark(self._order_manager, booked)
        except Exception:
            self._logger.exception(
                "Could not seed booked executions for restored orders; the "
                "runner's dedupe still drops their replays"
            )
            return
        finally:
            # Its own guard: a rollback failure must not escape setup() and
            # turn a best-effort seed into a startup failure.
            try:
                self._order_ledger.session.rollback()
            except Exception:
                self._logger.exception(
                    "Rollback after seeding booked executions failed"
                )
        if booked:
            self._logger.info(
                "Seeded booked executions of restored orders",
                count=len(booked),
            )

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
        quantity = order.quantity
        # Identity scope of this order, read while the ledger is open below and
        # used by the oversell guard — the broker holds one position per
        # {account, conId}, and neither field is on the stream message.
        account_id: str | None = None
        con_id: int | None = None
        ledger_quantity: float | None = None

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
            account_id, con_id = intent.account_id, intent.con_id
            ledger_quantity = float(intent.requested_quantity)
            # `get()` starts a read transaction. Broker submission is an
            # await point and IB callbacks use this same service-owned
            # session, so end the read before yielding control.
            self._order_ledger.session.rollback()

        # Halt gate — see :meth:`_rejected_as_halted` for the enforcement
        # diagram. Immediately before submission, and on the setup() PEL replay
        # path too: a restart replays orders approved seconds before a halt.
        if await self._rejected_as_halted(order):
            return

        # Oversell guard — see :meth:`_apply_oversell_guard`. After the halt
        # gate and immediately before submission: it reads the broker, and the
        # answer is only worth having if nothing else can intervene after it.
        cap_reason: str | None = None
        if self._guards_oversell(order, con_id=con_id):
            guarded = await self._apply_oversell_guard(
                order,
                account_id=account_id,
                con_id=con_id,
                ledger_quantity=ledger_quantity,
            )
            if guarded is None:
                return
            quantity, cap_reason = guarded

        try:
            if order.action == "buy":
                order_id = await self._order_manager.submit_entry(
                    ticker=order.ticker,
                    quantity=quantity,
                    limit_price=order.limit_price,
                    recommendation_id=order.recommendation_id,
                )
            else:
                order_id = await self._order_manager.submit_exit(
                    ticker=order.ticker,
                    quantity=quantity,
                    recommendation_id=order.recommendation_id,
                )
        except OrderPlacedElsewhereError:
            # KAN-112. Another IB client already has an order working for
            # this recommendation; the order manager has logged and paged.
            # The intent stays APPROVED — no new status, no migration —
            # because that is what the ledger can truthfully say: approved,
            # not submitted by us. SUBMISSION_FAILED would claim no order
            # exists, and for an exit it would let the risk service re-emit
            # under seq + 1 (a terminal intent no longer suppresses it) and
            # sell twice. The caller keeps the message unacked and re-probes
            # it slowly, so it goes out once that order is gone — an APPROVED
            # exit must not sit forever muting the position's next exit.
            raise
        except SubmissionDeferredError as exc:
            # KAN-112. IB did not answer whether an order already exists, so
            # nothing was placed. The intent stays APPROVED and the caller
            # keeps the message unacked and retries it — never
            # SUBMISSION_FAILED, which would drop the order on a Gateway blip.
            if order.action != "buy":
                await self._page_exit_deferred_once(order, exc)
            raise
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
                    order.recommendation_id, order_id, reason=cap_reason
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

    def _guards_oversell(
        self, order: ApprovedOrderMessage, *, con_id: int | None
    ) -> bool:
        """Whether the oversell guard applies to this order.

        Risk-side sells only (see :data:`RISK_EXIT_ADJUSTMENT_KEYS`), and only
        when everything the guard needs is present: a ledger to find working
        BUYs in, the contract they would be working on, and an order manager
        that can reach the broker. The last is the same ``getattr(type(...))``
        probe used for ``restore_broker_tracking`` — an embedding without a
        real broker (tests, backtests) keeps the pre-guard behaviour rather
        than failing on a call that cannot be made. It is logged, because a
        safety control that quietly does not run is the failure mode this
        whole tranche of work exists to remove.
        """
        if order.action != "sell":
            return False
        if not RISK_EXIT_ADJUSTMENT_KEYS.intersection(order.risk_adjustments or {}):
            return False
        if self._order_ledger is None or con_id is None:
            missing = "ledger" if self._order_ledger is None else "con_id"
        elif getattr(type(self._order_manager), "broker_position", None) is None:
            missing = "broker access"
        else:
            return True
        self._logger.warning(
            "Risk-side sell not guarded against overselling",
            ticker=order.ticker,
            quantity=order.quantity,
            recommendation_id=order.recommendation_id,
            missing=missing,
        )
        return False

    async def _apply_oversell_guard(
        self,
        order: ApprovedOrderMessage,
        *,
        account_id: str | None,
        con_id: int | None,
        ledger_quantity: float | None,
    ) -> tuple[float, str | None] | None:
        """Make a risk-side sell safe to place, or refuse to place it.

        Returns ``(quantity, reason)`` to submit, or None when nothing should
        be submitted — in which case the intent has already been terminalised
        and, where it matters, an alert raised.

        Position projection is asynchronous, so the book's view of a holding
        lags the broker's. Sizing an exit off that lag is how an account whose
        entire strategy is unlevered long-only ends up short — during the one
        incident where attention is elsewhere::

            flattening exit (kill / stop-loss)?
                   |  yes — a trim shaves a position that stays open, so it
                   |  caps but never cancels anything
                   v
            working BUYs for {account, con_id}
                   |
                   +-- submitted --> cancel at IB, confirm off the book
                   +-- approved, never submitted --> SUBMISSION_FAILED
                   |        (its message is still on the stream; without this
                   |         it reaches IB moments after the flatten)
                   v  cancel unconfirmed --> refuse (never sell into an
                   |                         unresolved working BUY)
            broker position for con_id  (retried on a backoff)
                   |
                   +-- unreadable --> refuse
                   v
            available = broker position - sells already working
                   |
                   +-- <= 0 --> refuse
                   v
            sell min(published, ledger requested, available)

        Refusing means SUBMISSION_FAILED plus an alert, NOT leaving the message
        in the PEL. The PEL is only re-read by :meth:`setup`, so a retained
        exit would sit there until the service restarts — and while it sat, its
        intent would stay non-terminal, which is precisely what
        ``nonterminal_sell_exists`` reads to suppress the *next* stop-loss for
        the position. Retaining would mute the control it is protecting. A
        terminal intent instead lets risk re-fire at ``seq + 1`` on its next
        scan (KAN-9), which is the self-healing path this service already had
        before the guard existed.

        Subtracting the sells already working is what makes the cap true rather
        than approximately true: a broker position is not reduced by an
        unfilled sell, so a kill stacked on a working stop-loss would otherwise
        see the full position twice and sell it twice. The kill path
        deliberately does not suppress on a pending sell (it must flatten the
        whole book), so that stacking is a designed-for case, not a rare one.
        """
        ledger = self._order_ledger
        assert ledger is not None  # guaranteed by _guards_oversell

        if FLATTENING_EXIT_ADJUSTMENT_KEYS.intersection(
            order.risk_adjustments or {}
        ):
            unconfirmed = await self._cancel_working_buys(
                order, account_id=account_id, con_id=con_id
            )
            if unconfirmed:
                await self._refuse_exit(
                    order,
                    reason=(
                        "oversell guard: working buys still live ("
                        + ", ".join(unconfirmed)
                        + ")"
                    ),
                    event_type="oversell_guard_cancel_failed",
                    message=(
                        f"Exit {order.recommendation_id} not submitted: could "
                        f"not cancel working buys on {order.ticker} "
                        f"({', '.join(unconfirmed)})"
                    ),
                    context={
                        "ticker": order.ticker,
                        "con_id": con_id,
                        "order_refs": unconfirmed,
                    },
                )
                return None

        broker_quantity = await self._read_broker_position(con_id)
        if broker_quantity is None:
            await self._refuse_exit(
                order,
                reason=(
                    f"oversell guard: broker position for {order.ticker} "
                    "could not be read"
                ),
                event_type="oversell_guard_position_unavailable",
                message=(
                    f"Exit {order.recommendation_id} not submitted: the broker "
                    f"position for {order.ticker} could not be read"
                ),
                context={"ticker": order.ticker, "con_id": con_id},
            )
            return None

        try:
            working_sells = ledger.outstanding_sell_quantity(
                account_id=account_id,
                con_id=con_id,
                exclude_recommendation_id=order.recommendation_id,
            )
        finally:
            ledger.session.rollback()
        available = broker_quantity - working_sells

        if broker_quantity <= EXIT_QUANTITY_EPSILON:
            self._logger.critical(
                "Exit not submitted: broker reports no position",
                ticker=order.ticker,
                con_id=con_id,
                book_quantity=order.quantity,
                recommendation_id=order.recommendation_id,
            )
            await self._refuse_exit(
                order,
                reason=(
                    f"oversell guard: broker holds none of {order.ticker} "
                    f"(con_id {con_id}) but the book expected {order.quantity}"
                ),
                event_type="oversell_guard_no_position",
                message=(
                    f"Exit {order.recommendation_id} not submitted: broker "
                    f"holds no {order.ticker} while the book expected "
                    f"{order.quantity} — reconciliation required"
                ),
                context={
                    "ticker": order.ticker,
                    "con_id": con_id,
                    "book_quantity": order.quantity,
                },
            )
            return None

        if available <= EXIT_QUANTITY_EPSILON:
            # Not a fault: sells already working cover the whole position, so
            # this exit has nothing left to sell. Alerting would page for the
            # guard doing its job — and a kill flattening a book of positions
            # that each already have a stop-loss working would page per name.
            self._logger.warning(
                "Exit not submitted: outstanding sells already cover the position",
                ticker=order.ticker,
                con_id=con_id,
                broker_quantity=broker_quantity,
                working_sells=working_sells,
                recommendation_id=order.recommendation_id,
            )
            await self._refuse_exit(
                order,
                reason=(
                    f"oversell guard: {working_sells} already working against "
                    f"the {broker_quantity} the broker holds"
                ),
            )
            return None

        requested = order.quantity
        if ledger_quantity is not None:
            requested = min(requested, ledger_quantity)
        if available < requested - EXIT_QUANTITY_EPSILON:
            self._logger.warning(
                "Exit capped to what the broker can cover",
                ticker=order.ticker,
                con_id=con_id,
                book_quantity=order.quantity,
                broker_quantity=broker_quantity,
                working_sells=working_sells,
                placed_quantity=available,
                recommendation_id=order.recommendation_id,
            )
            return available, (
                f"oversell guard: capped {requested} to {available} "
                f"({broker_quantity} held, {working_sells} already selling)"
            )

        return requested, None

    async def _cancel_working_buys(
        self,
        order: ApprovedOrderMessage,
        *,
        account_id: str | None,
        con_id: int | None,
    ) -> list[str]:
        """Take every working BUY on the contract off the table.

        Returns the refs that are still live afterwards — empty means the
        contract is clear. An APPROVED intent has no broker order to cancel
        but its message is still on ``stream:approved_orders``; terminalising
        it is what stops it reaching IB moments after the flatten (the same
        race, one message later), and the terminal check at the top of
        :meth:`process_approved_order` is what makes that stick.
        """
        ledger = self._order_ledger
        assert ledger is not None

        try:
            working: list[tuple[str, str]] = []
            for intent in ledger.active_buy_intents(
                account_id=account_id, con_id=con_id
            ):
                if intent.ib_order_id is None:
                    ledger.transition(
                        intent.recommendation_id,
                        OrderStatus.SUBMISSION_FAILED,
                        reason=(
                            "oversell guard: cancelled ahead of exit "
                            f"{order.recommendation_id}"
                        ),
                    )
                else:
                    working.append(
                        (str(intent.ib_order_id), intent.recommendation_id)
                    )
            self._commit_ledger()
        except Exception:
            ledger.session.rollback()
            raise

        if working:
            self._logger.warning(
                "Cancelling working buys before an exit",
                ticker=order.ticker,
                con_id=con_id,
                order_refs=[ref for _, ref in working],
                recommendation_id=order.recommendation_id,
            )
        unconfirmed = await self._order_manager.cancel_working_orders(working)
        if unconfirmed:
            self._logger.critical(
                "Working buys still live; holding back the exit",
                ticker=order.ticker,
                con_id=con_id,
                order_refs=unconfirmed,
                recommendation_id=order.recommendation_id,
            )
        return unconfirmed

    async def _read_broker_position(self, con_id: int | None) -> float | None:
        """Broker-held quantity for ``con_id``, or None if it cannot be read.

        Retried on :data:`BROKER_POSITION_RETRY_BACKOFF_SECONDS` for the same
        reason the halt latch is: this Gateway drops API sockets routinely, and
        a single transient reconnect race must not be enough to refuse an
        emergency flatten.
        """
        attempts = len(BROKER_POSITION_RETRY_BACKOFF_SECONDS) + 1
        for attempt in range(attempts):
            try:
                return float(
                    await self._order_manager.broker_position(con_id)
                )
            except Exception as exc:
                self._logger.warning(
                    "Broker position read failed",
                    con_id=con_id,
                    attempt=attempt + 1,
                    of=attempts,
                    error=str(exc),
                )
                if attempt < len(BROKER_POSITION_RETRY_BACKOFF_SECONDS):
                    await asyncio.sleep(
                        BROKER_POSITION_RETRY_BACKOFF_SECONDS[attempt]
                    )
        return None

    async def _refuse_exit(
        self,
        order: ApprovedOrderMessage,
        *,
        reason: str,
        event_type: str | None = None,
        message: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Terminalise an exit the guard will not place, and page if it is a
        fault. See :meth:`_apply_oversell_guard` for why this terminalises
        rather than retaining the message."""
        ledger = self._order_ledger
        assert ledger is not None
        try:
            ledger.transition(
                order.recommendation_id,
                OrderStatus.SUBMISSION_FAILED,
                reason=reason,
            )
            self._commit_ledger()
        except Exception:
            ledger.session.rollback()
            raise
        if event_type is None or message is None:
            return
        await self._publish_alert(
            event_type=event_type,
            priority="critical",
            message=message,
            context={
                **(context or {}),
                "recommendation_id": order.recommendation_id,
            },
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
            recovery_source=fill_info.get("recovery_source"),
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
        if fill.recovery_source:
            # The live callback first saw this execution in IB's execution
            # record: it was probably missed live (KAN-98). Paged in a batch
            # with the rest of its sweep pass or burst (KAN-102). Queued once:
            # if a later step here raises, the executor's retry runs this
            # again for the same execution.
            if fill.execution_id is None or all(
                queued.execution_id != fill.execution_id
                for queued in self._unpaged_recoveries
            ):
                self._unpaged_recoveries.append(fill)
            self._last_recovery_queued_at = asyncio.get_running_loop().time()

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

        # A newly-opened or topped-up position is unprotected until a stop
        # rests against it, so place one here rather than on a later timer.
        if fill.side.lower() == "buy":
            await self._cover_position_with_stop(fill_info, attribution, local_effect)
        return local_effect

    async def _cover_position_with_stop(
        self,
        fill_info: dict[str, Any],
        attribution: Any,
        local_effect: LocalFillEffect | None,
    ) -> None:
        """Bring the position this buy fill grew up to full stop coverage.

        Never allowed to disturb the fill: the fill is a fact IB already
        recorded, and losing it because a protective order was refused would
        trade a real accounting error for a hypothetical one. The failure is
        logged and paged instead (see ``_alert_stop_not_placed``).
        """
        if self._broker_stops is None or local_effect is None:
            return
        con_id = fill_info.get("con_id")
        if con_id is None:
            return
        try:
            await self._broker_stops.ensure_coverage(
                account_id=local_effect.account_id,
                portfolio=local_effect.portfolio,
                con_id=int(con_id),
                symbol=local_effect.ticker,
                exchange=fill_info.get("exchange"),
                currency=fill_info.get("currency"),
                quantity=self._held_after_fill(local_effect, int(con_id)),
                # The high since entry, which for a fill that just set a new
                # high is the fill itself. Backfill re-reads the stored high.
                reference_price=float(fill_info["fill_price"]),
            )
        except Exception:
            # Roll back first: a half-applied ledger transaction on this
            # shared session poisons every IB callback that follows.
            self._order_ledger.session.rollback()
            self._logger.exception(
                "Protective stop placement failed after a fill",
                ticker=local_effect.ticker,
                con_id=con_id,
            )

    def _held_after_fill(
        self, local_effect: LocalFillEffect, con_id: int
    ) -> float:
        """Shares held for this ``{account, portfolio, con_id}`` right now.

        The durable position plus every local fill the projector has not
        applied yet — the same overlay :meth:`_reconcile_managed_positions`
        uses. Sizing off the session's fills alone would under-cover a position
        opened before this process started; sizing off the durable row alone
        would under-cover one opened seconds ago.
        """
        # Drop the fills the projector has already folded into the durable
        # book. Without this the same shares are counted twice — once by the
        # Position row, once by the local effect that produced it — and the
        # stop placed for the difference over-covers the position, which sells
        # the account short when it triggers.
        self._drop_projected_fill_effects()
        key = (local_effect.account_id, local_effect.portfolio, local_effect.ticker)
        durable = self._broker_stops.durable_quantity(
            local_effect.account_id, local_effect.portfolio, con_id
        )
        unprojected = sum(
            effect.quantity_delta
            for effect in self._local_fill_effects.values()
            if (effect.account_id, effect.portfolio, effect.ticker) == key
        )
        return durable + unprojected + local_effect.quantity_delta

    def _drop_projected_fill_effects(self) -> None:
        """Forget local fills the durable book now accounts for.

        Also what keeps ``_local_fill_effects`` from growing without bound in
        a long-lived process: until now only the duplicate-fill and kill paths
        ever pruned it.
        """
        if self._order_ledger is None or not self._local_fill_effects:
            return
        try:
            projected = self._order_ledger.projected_execution_keys(
                self._local_fill_effects
            )
        except Exception:
            self._logger.exception(
                "Could not read which fills the projector has applied"
            )
            return
        finally:
            self._order_ledger.session.rollback()
        for execution_key in projected:
            self._local_fill_effects.pop(execution_key, None)

    async def _alert_stop_not_placed(self, **context: Any) -> None:
        """Page: a position is open with no protective stop resting on it."""
        await self._publish_alert(
            event_type="broker_stop_not_placed",
            priority="critical",
            message=(
                f"Protective stop not placed for {context.get('symbol')} — "
                f"{context.get('quantity')} shares are unprotected at the "
                f"broker: {context.get('reason')}"
            ),
            context={key: str(value) for key, value in context.items()},
        )

    async def _alert_stop_drift(self, drift: Any) -> None:
        """Page: a resting stop no longer matches the intent describing it."""
        await self._publish_alert(
            event_type="broker_stop_drift",
            priority="high",
            message=(
                f"Protective stop for {drift.symbol} has drifted from its "
                f"ledger intent and was left as-is: {drift.reason}"
            ),
            context={
                "symbol": drift.symbol,
                "con_id": str(drift.con_id),
                "order_id": drift.order_id,
                "recommendation_id": drift.recommendation_id,
                "expected_quantity": str(drift.expected_quantity),
                "broker_quantity": str(drift.broker_quantity),
                "expected_price": str(drift.expected_price),
                "broker_price": str(drift.broker_price),
            },
        )

    async def maybe_run_stop_verification(self, now: float) -> bool:
        """Verify every position's broker stop when the scan interval elapses.

        ``now`` is a monotonic timestamp (seconds). Inert with KAN-19's flag
        off — there are no broker stops to verify, and the risk service's scan
        keeps emitting software stop-loss exits exactly as before. Returns True
        when the verification ran.
        """
        if self._broker_stops is None:
            return False
        last = self._last_stop_verification_at
        if (
            last is not None
            and (now - last) < self._stop_verification_interval_seconds
        ):
            return False
        self._last_stop_verification_at = now
        try:
            report = await self._broker_stops.verify_coverage()
        except Exception:
            # Best-effort, like every other sweep on this loop: a verification
            # failure must not tear down the loop that still has to process
            # fills and liquidation sells. The rollback is itself guarded —
            # when the database is the thing that died, rollback() raises too,
            # and letting that escape would take the service down for exactly
            # the blip this handler exists to survive.
            try:
                self._order_ledger.session.rollback()
            except Exception:
                self._logger.exception("Rollback after a failed verification")
            self._logger.exception("Broker stop verification failed; continuing")
            return True
        if report:
            self._logger.info(
                "Broker stop verification adjusted resting protection",
                placed_order_ids=report.placed,
                cancelled_order_ids=report.cancelled_order_ids,
                released_intents=report.released_intents,
                drifts=len(report.drifts),
            )
        return True

    async def backfill_broker_stops(self) -> list[str]:
        """Cover every open position that has no resting stop (KAN-19 AC2)."""
        if self._broker_stops is None:
            return []
        try:
            return await self._broker_stops.backfill_open_positions()
        except Exception:
            self._order_ledger.session.rollback()
            self._logger.exception("Broker stop backfill failed")
            return []

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

    async def _alert_submitted_elsewhere(self, placement: Any, path: str) -> None:
        """Page: an order for this recommendation is working under another client.

        KAN-112. Wired into the order manager, which calls it once per
        episode. Nothing was submitted and nothing was bound; the message is
        re-probed every few minutes, and a human decides whether that order
        stands (and reconciles it) or is cancelled.
        """
        await self._publish_alert(
            event_type="order_submitted_elsewhere",
            priority="high",
            message=(
                f"{path} for {placement.recommendation_id} NOT submitted: IB "
                f"already has order {placement.order_id} ({placement.action} "
                f"{placement.quantity:g} {placement.ticker}, "
                f"{placement.status or 'status unknown'}) working for it under "
                f"client id {placement.client_id}. Not duplicated and not "
                "tracked by execution; re-checked every "
                f"{self._elsewhere_retry_interval_seconds / 60:g} min. See "
                "docs/operations/order-submitted-elsewhere.md."
            ),
            context={
                "recommendation_id": placement.recommendation_id,
                "other_order_id": str(placement.order_id),
                "other_client_id": str(placement.client_id),
                "ticker": placement.ticker,
                "action": placement.action,
                "path": path,
            },
        )

    async def _page_exit_deferred_once(
        self, order: ApprovedOrderMessage, exc: SubmissionDeferredError
    ) -> None:
        """Page once when an exit is withheld because IB did not answer.

        An entry waits with a warning; an exit is the position's way out and
        must not sit unsent in silence (KAN-112).
        """
        if order.recommendation_id in self._deferred_exit_paged:
            return
        try:
            await self._publish_alert(
                event_type="exit_submission_deferred",
                priority="high",
                message=(
                    f"Exit {order.action} {order.quantity:g} {order.ticker} "
                    f"({order.recommendation_id}) NOT submitted: IB did not "
                    "answer whether an order already exists for it. Retrying "
                    f"every {self._deferred_retry_interval_seconds:g}s until "
                    "it does."
                ),
                context={
                    "recommendation_id": order.recommendation_id,
                    "ticker": order.ticker,
                    "reason": exc.reason,
                },
            )
        except Exception:
            # Not marked paged: the next deferral of this exit tries again.
            self._logger.exception(
                "Could not page a withheld exit",
                recommendation_id=order.recommendation_id,
            )
            return
        self._deferred_exit_paged.add(order.recommendation_id)

    def _defer_message(
        self,
        stream: str,
        msg: Any,
        parser: Any,
        handler: Any,
        exc: SubmissionDeferredError | OrderPlacedElsewhereError,
    ) -> None:
        """Keep a message unacked and schedule its next attempt.

        Called after the attempt, so the next one is due an interval from
        when this one *finished* — a probe that took its full bound does not
        make the next attempt due at once.
        """
        if isinstance(exc, OrderPlacedElsewhereError):
            kind = "elsewhere"
            recommendation_id = exc.placement.recommendation_id
            interval = self._elsewhere_retry_interval_seconds
        else:
            kind = "unanswered"
            recommendation_id = exc.recommendation_id
            interval = self._deferred_retry_interval_seconds
        now = asyncio.get_running_loop().time()
        message_id = str(msg.message_id)
        record = self._deferred_messages.get(message_id)
        if record is None:
            record = DeferredMessage(
                message_id=message_id, stream=stream, msg=msg, parser=parser,
                handler=handler, recommendation_id=recommendation_id,
                kind=kind, due_at=now + interval,
            )
            self._deferred_messages[message_id] = record
        record.kind = kind
        record.attempts += 1
        record.due_at = now + interval
        warn = (
            record.last_warned_at is None
            or now - record.last_warned_at
            >= self._deferred_warning_interval_seconds
        )
        if warn:
            record.last_warned_at = now
        (self._logger.warning if warn else self._logger.info)(
            "Order not submitted; message left unacked for retry",
            stream=stream,
            message_id=message_id,
            recommendation_id=recommendation_id,
            cause=kind,
            attempts=record.attempts,
            retry_in_seconds=interval,
        )

    async def maybe_retry_deferred_orders(self, now: float) -> bool:
        """Retry the deferred approved orders that are due (KAN-112).

        Each is re-run through the same handler, so the halt gate, the ledger
        checks and the probe all run again. A message leaves the queue only on
        a settled outcome (processed, dead-lettered, or a BUY expired past its
        session); an unreadable halt latch or a cancellation mid-pass keeps
        it. A pass stops at the first message IB still does not answer.
        """
        due = [
            message_id
            for message_id, record in self._deferred_messages.items()
            if record.due_at <= now
        ]
        if not due:
            return False
        for message_id in due:
            record = self._deferred_messages.get(message_id)
            if record is None:
                continue
            if await self._expire_stale_deferred_buy(record):
                continue
            outcome: str | None = None
            try:
                outcome = await self._handle_message(
                    record.stream, record.msg, record.parser, record.handler
                )
            finally:
                if outcome in ("processed", "dead_lettered"):
                    self._settle_deferred(message_id)
                elif outcome == "retained":
                    record.due_at = (
                        asyncio.get_running_loop().time()
                        + self._deferred_retry_interval_seconds
                    )
            if outcome == "deferred" and record.kind == "unanswered":
                break
        return True

    def _settle_deferred(self, message_id: str) -> None:
        """Drop a settled message, and end its paging episodes."""
        record = self._deferred_messages.pop(message_id, None)
        if record is None:
            return
        self._deferred_exit_paged.discard(record.recommendation_id)
        forget = getattr(
            type(self._order_manager), "forget_submitted_elsewhere", None
        )
        if forget is not None:
            forget(self._order_manager, record.recommendation_id)

    async def _expire_stale_deferred_buy(self, record: DeferredMessage) -> bool:
        """Terminalize a deferred BUY whose session has closed (KAN-112).

        A BUY is sized and priced for one session — the next to close after
        it was approved. Placed after that, it would be a stale-priced entry
        nobody decided on. Exits are never expired: getting out stays right.
        """
        try:
            order = record.parser(record.msg.data)
        except Exception:
            return False
        if getattr(order, "action", None) != "buy":
            return False
        close = self._session_close_for(order.timestamp)
        if close is None or self._utcnow() <= close:
            return False
        ledger = self._order_ledger
        if ledger is not None:
            try:
                intent = ledger.get(record.recommendation_id)
                if intent.status == OrderStatus.APPROVED.value:
                    ledger.transition(
                        record.recommendation_id,
                        OrderStatus.SUBMISSION_FAILED,
                        reason=DEFERRED_PAST_SESSION_REASON,
                    )
                    self._commit_ledger()
                else:
                    ledger.session.rollback()
            except OrderIntentNotFound:
                ledger.session.rollback()
            except Exception:
                ledger.session.rollback()
                self._logger.exception(
                    "Could not expire a deferred buy; it stays queued",
                    recommendation_id=record.recommendation_id,
                )
                return False
        try:
            await self._redis.ack(record.stream, CONSUMER_GROUP, record.msg.message_id)
        except Exception:
            self._logger.exception(
                "Ack failed for an expired deferred buy; relying on redelivery",
                message_id=record.message_id,
            )
        self._settle_deferred(record.message_id)
        self._logger.warning(
            "Deferred buy expired: the session it was sized for has closed",
            recommendation_id=record.recommendation_id,
            ticker=order.ticker,
            session_close=close.isoformat(),
            attempts=record.attempts,
            cause=record.kind,
        )
        return True

    def _session_close_for(self, approved_at: datetime) -> datetime | None:
        """Close of the first session ending after ``approved_at``."""
        if approved_at.tzinfo is None:
            approved_at = approved_at.replace(tzinfo=timezone.utc)
        calendar = self._market_calendar or self._deferral_calendar
        try:
            if calendar is None:
                from shared.market_calendar import MarketCalendar

                calendar = self._deferral_calendar = MarketCalendar()
            close = calendar.get_next_market_close(approved_at)
            if close <= approved_at:
                close = calendar.get_next_market_close(
                    approved_at + timedelta(days=1)
                )
        except Exception:
            self._logger.exception(
                "Could not read the session close for a deferred buy"
            )
            return None
        return close

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

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

    async def handle_ib_connectivity_alert(
        self, info: dict[str, Any]
    ) -> None:
        """Page on IB Error 1101 — connectivity restored, subscriptions lost.

        The executor re-requests its open orders, but any fill that completed
        while it was blind cannot be replayed from a callback, so a human has
        to check the book against the broker. On 2026-09-18 this condition was
        entirely silent and 15 filled positions never reached the ledger.
        """
        order_ids = info.get("tracked_order_ids") or []
        await self._publish_alert(
            event_type="ib_connectivity_data_lost",
            priority="high",
            message=(
                f"IB Error {info.get('error_code')}: connectivity restored but "
                f"data LOST on {info.get('host')}:{info.get('port')}. Open "
                f"orders were re-requested; fills on the "
                f"{len(order_ids)} order(s) tracked this session may have "
                f"completed unobserved — reconcile against the broker."
            ),
            context={
                "error_code": str(info.get("error_code")),
                "host": str(info.get("host")),
                "port": str(info.get("port")),
                "tracked_order_ids": ",".join(str(o) for o in order_ids),
            },
        )

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

        snapshot = status_info.get("intent_snapshot")
        if status_info.get("order_absent_at_ib") is True and snapshot is not None:
            # The post-reconnect resolution read the intent before asking IB
            # (KAN-106). A fill or status that landed since means the order
            # was not simply gone; the expiry is a guess that evidence has
            # overtaken, so it is refused and the resolution looks again.
            if (
                str(intent.status) != str(snapshot.get("status"))
                or float(intent.filled_quantity or 0.0)
                > float(snapshot.get("filled_quantity", 0.0)) + 1e-9
            ):
                self._logger.warning(
                    "Refusing an absent-at-IB expiry: the intent changed "
                    "since the resolution read it",
                    order_id=order_id,
                    snapshot=snapshot,
                    current_status=intent.status,
                    current_filled_quantity=intent.filled_quantity,
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
        expired_on_absence = status_info.get("order_absent_at_ib") is True
        # Read before the commit expires the row: the missed-fill check below
        # needs these after the transition (KAN-96).
        absent_identity = SimpleNamespace(
            recommendation_id=intent.recommendation_id,
            account_id=intent.account_id,
            portfolio=intent.portfolio,
            con_id=intent.con_id,
            symbol=intent.symbol,
            action=str(intent.action).upper(),
            ib_order_id=order_id,
        )
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
        if expired_on_absence and absent_identity.action == "BUY":
            await self._alert_if_absent_buy_filled(absent_identity)

    async def _alert_if_absent_buy_filled(self, intent: SimpleNamespace) -> None:
        """Page when IB holds shares the book does not, after an absent expiry.

        Expiring an order IB no longer knows assumes "any filled shares are
        already recorded". When a fill was missed while disconnected that is
        false, and the expiry removes the only record of which sleeve owned it
        (XLC, 2026-09-25 → 2026-09-30). Comparing broker and book for the
        contract right here turns that into a page naming the intent and the
        repair, instead of an anonymous ``missing_in_db`` days later (KAN-96).

        Best-effort: the expiry is already committed and must stand whatever
        this check does.
        """
        try:
            broker_qty = float(
                await self._order_manager.broker_position(int(intent.con_id))
            )
            book_qty = self._order_ledger.open_position_quantity(
                account_id=intent.account_id, con_id=int(intent.con_id)
            )
            self._order_ledger.session.rollback()
        except Exception:
            try:
                self._order_ledger.session.rollback()
            except Exception:
                self._logger.exception("Rollback after a failed missed-fill check")
            self._logger.exception(
                "Could not compare broker and book after an absent expiry",
                recommendation_id=intent.recommendation_id,
            )
            return
        if broker_qty - book_qty < 1.0:
            return
        try:
            await self._publish_alert(
                event_type="probable_missed_fill",
                priority="high",
                message=(
                    f"Probable missed fill: {intent.recommendation_id} "
                    f"({intent.symbol}, {intent.portfolio}, IB order "
                    f"{intent.ib_order_id}) was expired because IB no longer knew "
                    f"the order, but IB holds {broker_qty:g} {intent.symbol} and "
                    f"the book holds {book_qty:g}. Reconciliation will block "
                    "entries. Rebuild the fill from an IB Flex Trades statement: "
                    "python scripts/ops/restore_missed_entries.py --statement "
                    f"<csv> --account {intent.account_id} --statement-tz "
                    "America/New_York"
                ),
                context={
                    "recommendation_id": str(intent.recommendation_id),
                    "symbol": str(intent.symbol),
                    "portfolio": str(intent.portfolio),
                    "ib_order_id": str(intent.ib_order_id),
                    "con_id": str(intent.con_id),
                    "broker_qty": str(broker_qty),
                    "book_qty": str(book_qty),
                },
            )
        except Exception:
            # The expiry stands either way, and nothing on this path may fail
            # execution startup (it runs inside restore_broker_tracking).
            self._logger.exception(
                "Could not publish probable_missed_fill",
                recommendation_id=intent.recommendation_id,
            )

    async def process_kill(self, kill_msg: KillMessage) -> None:
        """Process a kill event: cancel all open orders and liquidate positions.

        Working orders go up front — a queued entry must not fill into a book
        this is flattening. Protective stops do **not**: each position's stop
        is cancelled immediately before that position's own liquidation sell
        (KAN-20). Cancelling them all first strips protection from every
        position at once and then flattens them one at a time, so a sell that
        fails mid-loop leaves its position un-flattened *and* unprotected —
        and so does every position still waiting behind it.

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

        # Cancel every working order, leaving the stops resting for now.
        await self._order_manager.cancel_all_orders(include_stops=False)

        # Deterministic per-kill epoch: exits for one kill converge on the same
        # ids across a replay and across the risk-side authoritative path, so we
        # never double-sell.
        epoch = int(kill_msg.timestamp.timestamp())
        liquidated = 0
        # One probe view per kill (KAN-112): after IB leaves one unanswered,
        # the rest of this kill's sells skip it rather than each waiting out
        # the same bound.
        kill_probe = KillProbe()
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
                await self._cancel_stops_before_liquidating(ticker)
                # Submitted even when IB does not answer the idempotency
                # probe (KAN-112; see KillProbe for the window that leaves).
                # This position's stop was cancelled a line above and
                # process_kill is one-shot, so withholding the sell would
                # leave it unprotected AND un-flattened. Another client's
                # working order under this id is an answer, and still blocks
                # it.
                await self._order_manager.submit_exit(
                    ticker=ticker,
                    quantity=quantity,
                    recommendation_id=exit_id,
                    kill=kill_probe,
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

        # Sweep the stops the per-position path did not reach: one on a name
        # the book no longer holds, or on one whose exit was already in flight.
        # Either would rest against a flat position and sell short on trigger.
        # Stops only — cancel_all_orders() here would cancel the liquidation
        # exits submitted moments ago and leave the book un-flattened.
        await self._order_manager.cancel_all_stops()

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

    async def _cancel_stops_before_liquidating(self, ticker: str) -> None:
        """Clear this position's protective stops, moments before its sell.

        A stop left resting while the liquidation sells the shares out from
        under it becomes a short on trigger. Doing it here rather than in the
        up-front cancel-all is what keeps the unprotected window to this one
        position (KAN-20).
        """
        canceller = getattr(
            self._order_manager, "cancel_stops_for_ticker", None
        )
        if canceller is None:
            return
        cancelled = await canceller(ticker)
        if cancelled:
            self._logger.info(
                "Cancelled protective stops ahead of a liquidation sell",
                ticker=ticker,
                order_ids=cancelled,
            )

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

    async def maybe_check_ib_connection(self, now: float) -> bool:
        """Reconnect a dropped IB session when the liveness interval elapses.

        ``now`` is a monotonic timestamp (seconds). Runs whatever the market
        state and whether or not any order is open: on 2026-09-25 the Gateway
        restarted at 13:53 SGT with order 219 resting and nothing else to do,
        so nothing reconnected and its 21:30 fill was never seen (KAN-94).

        A failed reconnect is logged and retried next interval; a disconnect
        lasting ``ib_disconnect_alert_seconds`` pages once, and the reconnect
        after that page says so. A session on the wrong account pages
        ``ib_wrong_account`` (critical) and then propagates, stopping the
        service the way startup refuses it — it must never be quieter than
        the per-order poison alerts it pre-empts. Paging is best-effort: a
        Redis failure is logged, never raised in place of the real outcome.
        Returns True when the check ran.
        """
        from services.execution.ib_executor import WrongAccountTypeError

        last = self._last_ib_liveness_at
        if last is not None and (now - last) < self._ib_liveness_interval_seconds:
            return False
        self._last_ib_liveness_at = now
        try:
            reconnected = await self._order_manager.ensure_broker_connection()
        except WrongAccountTypeError as exc:
            await self._publish_alert_best_effort(
                event_type="ib_wrong_account",
                priority="critical",
                message=(
                    f"Execution is STOPPING: the IB session on "
                    f"{self._config.ib.host}:{self.ib_port} is the wrong "
                    f"account — {exc}"
                ),
                context={"host": str(self._config.ib.host),
                         "port": str(self.ib_port)},
            )
            raise
        except Exception as exc:
            if self._ib_disconnected_since is None:
                self._ib_disconnected_since = now
            down_for = now - self._ib_disconnected_since
            self._logger.warning(
                "IB reconnect failed; retrying next interval",
                error=str(exc),
                disconnected_seconds=round(down_for),
            )
            if (
                not self._ib_disconnect_alerted
                and down_for >= self._ib_disconnect_alert_seconds
            ):
                self._ib_disconnect_alerted = True
                await self._publish_ib_disconnected(down_for, exc)
            return True

        if reconnected is True:
            self._logger.info(
                "IB session restored by the liveness check",
                host=self._config.ib.host,
                port=self.ib_port,
            )
        if self._ib_disconnect_alerted:
            await self._publish_alert_best_effort(
                event_type="ib_reconnected",
                priority="low",
                message=(
                    f"Execution reconnected to IB on {self._config.ib.host}:"
                    f"{self.ib_port} after at least "
                    f"{round((now - self._ib_disconnected_since) / 60)} min. "
                    "Fills that completed while it was down are not replayed "
                    "by callbacks — check reconciliation."
                ),
            )
        self._ib_disconnected_since = None
        self._ib_disconnect_alerted = False
        return True

    async def _publish_ib_disconnected(
        self, down_for: float, exc: Exception
    ) -> None:
        tracked = len(getattr(self._order_manager, "open_orders", {}) or {})
        await self._publish_alert_best_effort(
            event_type="ib_disconnected",
            priority="high",
            message=(
                f"Execution has been disconnected from IB on "
                f"{self._config.ib.host}:{self.ib_port} for at least "
                f"{round(down_for / 60)} min and cannot reconnect ({exc}). "
                f"{tracked} tracked open order(s): any fill IB reports now "
                "is not reaching the book."
            ),
            context={
                "host": str(self._config.ib.host),
                "port": str(self.ib_port),
                "disconnected_seconds": str(round(down_for)),
                "tracked_open_orders": str(tracked),
            },
        )

    async def _publish_alert_best_effort(self, **alert: Any) -> None:
        """Publish an alert, logging instead of raising when Redis fails.

        For paths where a failed page must not replace the outcome it reports
        — above all a reconnect that has just succeeded, where an escaped
        error would run ``shutdown()`` and cancel the resting orders the
        reconnect exists to protect.
        """
        try:
            await self._publish_alert(**alert)
        except Exception:
            self._logger.exception(
                "Could not publish alert", event_type=alert.get("event_type")
            )

    async def maybe_run_execution_sweep(self, now: float) -> bool:
        """Book fills the live callback missed, from IB's own record (KAN-95).

        ``now`` is a monotonic timestamp (seconds). Runs every
        ``execution_sweep_interval_minutes`` and immediately after any
        reconnect (the executor's connection generation changed), so a fill
        that landed while the socket was dead is read back the same evening.
        Inert without a ledger: there is nothing to attribute a fill to.

        The decision is ``plan_sweep``'s, unchanged. Executions this process
        already handled are skipped and recovered ones are marked handled,
        under the same lock as the live callback, so the two paths never
        publish one execution twice. Best-effort: a failure is logged and
        retried next interval. Returns True when a sweep ran.
        """
        from services.execution.execution_sweep import plan_sweep

        if self._order_ledger is None:
            return False
        try:
            generation = await self._order_manager.broker_connection_generation()
        except Exception:
            self._logger.exception(
                "Could not read the IB connection generation; reconnect "
                "detection is off for this pass"
            )
            generation = None
        reconnected = (
            generation is not None
            and self._last_execution_sweep_generation is not None
            and generation != self._last_execution_sweep_generation
        )
        last = self._last_execution_sweep_at
        if (
            not reconnected
            and last is not None
            and (now - last) < self._execution_sweep_interval_seconds
        ):
            return False
        self._last_execution_sweep_at = now
        self._last_execution_sweep_generation = generation
        session = await self._broker_session_generation()
        if (
            session is not None
            and self._last_session_generation is not None
            and session != self._last_session_generation
        ):
            # A fresh IB session: resolved once this pass (or a later one)
            # has read IB's executions on it (KAN-106).
            self._absent_resolution_pending = True
            self._absent_resolution_ready = False
        if session is not None:
            self._last_session_generation = session

        try:
            executions = await self._order_manager.recent_broker_executions()
            async with self._fill_lock:
                fresh = [
                    execution for execution in executions
                    if (execution.account_id, execution.execution_id)
                    not in self._handled_executions
                ]
                outcome = plan_sweep(fresh, self._order_ledger)
                self._commit_ledger()
                for fill in outcome.recovered:
                    await self._redis.publish(FILLS_STREAM, fill.to_stream_dict())
                    self._handled_executions.add(
                        (str(fill.account_id), str(fill.execution_id))
                    )
        except Exception as exc:
            try:
                self._order_ledger.session.rollback()
            except Exception:
                self._logger.exception("Rollback after a failed execution sweep")
            self._logger.exception("Execution sweep failed; retrying next interval")
            self._execution_sweep_failures += 1
            if self._execution_sweep_failures == EXECUTION_SWEEP_FAILURE_PAGE_AFTER:
                await self._publish_alert_best_effort(
                    event_type="execution_sweep_failed",
                    priority="high",
                    message=(
                        f"The execution sweep has failed "
                        f"{self._execution_sweep_failures} passes in a row "
                        f"({exc!r}). Fills the live callback missed are not "
                        "being recovered, and IB only serves today's "
                        "executions — a miss not read before midnight SGT "
                        "needs an IB statement."
                    ),
                    context={"consecutive_failures":
                             str(self._execution_sweep_failures)},
                )
            return True

        self._execution_sweep_failures = 0
        if self._absent_resolution_pending:
            # Ready only if the pass read executions on the session it started
            # on: a second reconnect during the pass means its read describes
            # a session that is gone, and the next pass (a reconnect) sweeps
            # the new one first.
            after = await self._read_connection_generation()
            if generation is not None and after == generation:
                self._absent_resolution_ready = True
                self._absent_resolution_generation = generation
            else:
                self._logger.info(
                    "IB session changed during the execution sweep; resolving "
                    "absent orders after the next pass",
                    swept_generation=generation,
                    current_generation=after,
                )
        await self._page_execution_sweep_outcome(outcome)
        log = self._logger.warning if outcome.recovered else self._logger.info
        log(
            "Execution sweep",
            fetched=len(executions),
            recovered=len(outcome.recovered),
            already_recorded=outcome.already_recorded
            + (len(executions) - len(fresh)),
            untracked=len(outcome.untracked),
            corrected=len(outcome.corrected),
            deferred=len(outcome.deferred),
            after_reconnect=reconnected,
        )
        return True

    async def _page_execution_sweep_outcome(self, outcome: Any) -> None:
        """Page what a sweep found; the observability stack reaches no one.

        A recovered fill means the live callback missed one — the condition
        that blocked every buy for five sessions in September — so it is
        worth a message even though the book is now right. An untracked
        execution is IB doing something the book will not record.

        One recovered page per pass (KAN-102): it also lists the fills the
        live path booked from this pass's ``reqExecutions`` reply, which the
        executor lets finish before the sweep books its own.
        """
        await self._page_recovered_fills(
            swept=list(outcome.recovered), live=self._take_unpaged_recoveries()
        )
        new_untracked = [
            order_id for order_id in outcome.untracked
            if order_id not in self._execution_sweep_untracked_paged
        ]
        if new_untracked:
            self._execution_sweep_untracked_paged.update(new_untracked)
            await self._publish_alert_best_effort(
                event_type="execution_sweep_untracked",
                priority="high",
                message=(
                    "IB reports executions for order id(s) "
                    f"{', '.join(new_untracked)} that the book has no intent "
                    "for (or holds terminal), so they will not be booked. "
                    "Reconciliation will see the position; check whether "
                    "these were manual orders."
                ),
                context={"ib_order_ids": ",".join(new_untracked)},
            )

    def _take_unpaged_recoveries(self) -> list[FillMessage]:
        taken, self._unpaged_recoveries = self._unpaged_recoveries, []
        self._last_recovery_queued_at = None
        return taken

    async def _page_recovered_fills(
        self, *, swept: list[FillMessage], live: list[FillMessage]
    ) -> None:
        """One ``execution_sweep_recovered`` page listing every fill given.

        ``swept`` are the sweep's own bookings; ``live`` are the fills the
        live callback first saw in IB's execution record. Best-effort.
        """
        fills = [*swept, *live]
        if not fills:
            return
        names = ", ".join(
            f"{fill.recommendation_id} ({fill.quantity:g} {fill.ticker})"
            for fill in fills
        )
        how = []
        if swept:
            how.append(
                f"The execution sweep booked {len(swept)} of them, which the "
                "live callback missed."
            )
        if live:
            how.append(
                f"{len(live)} reached the live callback only through IB's "
                "execution record: probably missed while execution's IB "
                "session was down."
            )
        await self._publish_alert_best_effort(
            event_type="execution_sweep_recovered",
            priority="medium",
            message=(
                f"{len(fills)} fill(s) were booked from IB's execution record "
                f"rather than live: {names}. {' '.join(how)} The book is now "
                "right; recurring pages mean execution keeps losing its IB "
                "session."
            ),
            context={
                "recovered": str(len(fills)),
                "swept": str(len(swept)),
                "live_path": str(len(live)),
                "execution_ids": ",".join(
                    str(fill.execution_id) for fill in fills
                ),
            },
        )

    async def maybe_page_recovered_fills(self, now: float) -> bool:
        """Page the live path's recovered fills no sweep pass has paged.

        ``now`` is the event loop's monotonic time. Waits until no fill has
        joined the batch for ``RECOVERY_PAGE_SETTLE_SECONDS``, so a burst
        becomes one page, and stays out of the way while a sweep pass runs —
        that pass pages them with its own. Returns True when a page went out.
        """
        if not self._unpaged_recoveries:
            return False
        task = self._execution_sweep_task
        if task is not None and not task.done():
            return False
        last = self._last_recovery_queued_at
        if last is not None and (now - last) < RECOVERY_PAGE_SETTLE_SECONDS:
            return False
        await self._page_recovered_fills(
            swept=[], live=self._take_unpaged_recoveries()
        )
        return True

    def _start_execution_sweep(self, now: float) -> bool:
        """Start the execution sweep as a task unless one is still running.

        Returns True when a task was started. The sweep decides for itself
        whether it is due, so a started task usually returns at once; only a
        due pass talks to IB. This takes only the sweep off the kill path
        (KAN-98): the loop's other IB steps still run inline.

        Running beside the loop, the sweep shares the ledger session. That is
        safe only under the runner's rule that no coroutine holds a
        transaction open across an await — the sweep's commit or rollback
        would otherwise land on another coroutine's pending work.
        """
        task = self._execution_sweep_task
        if task is not None and not task.done():
            return False
        self._execution_sweep_task = asyncio.create_task(
            self._execution_sweep_guarded(now)
        )
        return True

    async def _execution_sweep_guarded(self, now: float) -> None:
        try:
            await self.maybe_run_execution_sweep(now)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception("Execution sweep failed; continuing")
        # In the same task, strictly after the sweep (KAN-106): a fill IB
        # still serves is booked before the order could be expired, and like
        # the sweep it runs off the kill path.
        try:
            await self.maybe_resolve_absent_orders()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception(
                "Resolving orders absent after a reconnect failed; retrying "
                "after the next execution sweep"
            )

    async def maybe_resolve_absent_orders(self) -> bool:
        """Resolve tracked orders IB may have dropped while disconnected.

        KAN-106. On 2026-10-06 ARKW DAY order 241 expired while the Gateway
        was down; execution reconnected on its own but only warned, so the
        intent stayed SUBMITTED, reconciliation read ``order_missing_at_ib``
        and entries were disabled until execution was restarted. Its startup
        restore is what healed it, so this runs the same resolution — through
        ``OrderManager.resolve_tracked_order`` and
        ``IBExecutor.restore_order_by_ref`` — for every order the book still
        holds working: re-attached if IB still lists it open, its true status
        if IB's completed-order history has it, otherwise EXPIRED with
        ``ABSENT_AT_IB_REASON`` (KAN-96, which also pages a probable missed
        fill when IB holds shares the book does not).

        Runs only once a sweep pass that started on the new session has
        succeeded and ended on it (``_absent_resolution_ready``). Before each
        order it re-checks that the session is still the one swept, and
        defers the order — whatever IB would answer for it — while IB has
        served an execution the book has not applied, settled or not. Its
        snapshot of the intent rides on an absent expiry, which the status
        handler refuses if a fill or status landed in between. Anything
        failed, deferred, refused or interrupted keeps the resolution pending
        for the next successful pass — the next interval or reconnect.
        Returns True when a resolution pass ran.
        """
        if not (self._absent_resolution_pending and self._absent_resolution_ready):
            return False
        self._absent_resolution_ready = False
        resolve = getattr(
            type(self._order_manager), "resolve_tracked_order", None
        )
        if resolve is None or self._order_ledger is None:
            self._absent_resolution_pending = False
            return False

        generation = self._absent_resolution_generation
        outcomes: dict[str, str] = {}
        deferred: list[str] = []
        for recommendation_id in self._absent_resolution_candidates():
            if await self._read_connection_generation() != generation:
                outcomes["session"] = "stale"
                break
            snapshot = self._working_intent_snapshot(recommendation_id)
            if snapshot is None:
                continue  # terminal since the candidates were read
            order_id = snapshot["order_id"]
            if self._has_unbooked_served_fill(order_id, snapshot):
                deferred.append(order_id)
                continue
            outcome = await resolve(
                self._order_manager,
                recommendation_id,
                expected_generation=generation,
                intent_snapshot={
                    "status": snapshot["status"],
                    "filled_quantity": snapshot["filled_quantity"],
                },
            )
            if outcome == "resolved":
                after = self._working_intent_snapshot(recommendation_id)
                if after is not None and (
                    after["status"] != snapshot["status"]
                    or after["filled_quantity"] != snapshot["filled_quantity"]
                ):
                    # The intent moved under the resolution (a live fill or
                    # status) and the handler refused to expire it: look again
                    # next pass, from the new state.
                    outcome = "refused"
                elif after is not None:
                    # IB's status maps to no transition (e.g. a partly filled
                    # order IB reports Expired); asking again changes nothing.
                    outcome = "no_transition"
            outcomes[order_id] = outcome
            if outcome == "stale":
                break

        retry_outcomes = {"failed", "stale", "refused"}
        retry = sorted(
            order_id for order_id, outcome in outcomes.items()
            if outcome in retry_outcomes
        )
        self._absent_resolution_pending = bool(retry or deferred)
        await self._track_holdbacks(
            held={
                order_id for order_id in [*retry, *deferred]
                if order_id != "session"
            },
            settled={
                order_id for order_id, outcome in outcomes.items()
                if outcome not in retry_outcomes
            },
        )
        if outcomes or deferred:
            quiet = all(
                outcome in {"open", "resolved", "untracked"}
                for outcome in outcomes.values()
            )
            log = (
                self._logger.info
                if quiet and not deferred
                else self._logger.warning
            )
            log(
                "Resolved tracked orders after reconnect",
                outcomes=outcomes,
                retry=retry,
                deferred_unbooked_fills=sorted(deferred),
                retrying=self._absent_resolution_pending,
            )
        return True

    async def _track_holdbacks(self, *, held: set[str], settled: set[str]) -> None:
        """Page once for an order the resolution keeps holding back.

        A served execution that never settles or never projects, or a
        request IB keeps failing for one order, would otherwise keep the
        resolution pending and only log every interval while reconciliation
        blocks entries on the order.
        """
        for order_id in settled:
            self._absent_holdback_passes.pop(order_id, None)
            self._absent_holdback_paged.discard(order_id)
        for order_id in held:
            passes = self._absent_holdback_passes.get(order_id, 0) + 1
            self._absent_holdback_passes[order_id] = passes
            if (
                passes >= ABSENT_HOLDBACK_PAGE_AFTER
                and order_id not in self._absent_holdback_paged
            ):
                self._absent_holdback_paged.add(order_id)
                await self._publish_alert_best_effort(
                    event_type="absent_resolution_held_back",
                    priority="high",
                    message=(
                        f"IB order {order_id} has been held back from the "
                        f"post-reconnect resolution for {passes} passes: IB "
                        "served an execution the book has not applied, or "
                        "its order-state request keeps failing. It stays "
                        "working in the book, so reconciliation may block "
                        "entries. Check execution's logs ('Resolved tracked "
                        "orders after reconnect') and the fills DLQ."
                    ),
                    context={"ib_order_id": order_id, "passes": str(passes)},
                )

    def _absent_resolution_candidates(self) -> list[str]:
        """Recommendation ids of every intent the book holds working at IB.

        An order that is already terminal needs nothing from IB, and asking
        would cost a completed-order request each. Reads and releases the
        shared session before any await.
        """
        try:
            return [
                str(intent.recommendation_id)
                for intent in self._order_ledger.load_pending_orders()
                if intent.ib_order_id is not None
            ]
        finally:
            self._order_ledger.session.rollback()

    def _working_intent_snapshot(
        self, recommendation_id: str
    ) -> dict[str, Any] | None:
        """The intent's state now, or None once it is no longer working."""
        try:
            intent = self._order_ledger.get(recommendation_id)
            if (
                intent.ib_order_id is None
                or OrderStatus(intent.status) in TERMINAL_STATUSES
            ):
                return None
            return {
                "order_id": str(intent.ib_order_id),
                "status": str(intent.status),
                "filled_quantity": float(intent.filled_quantity or 0.0),
            }
        finally:
            self._order_ledger.session.rollback()

    def _has_unbooked_served_fill(
        self, order_id: str, snapshot: dict[str, Any]
    ) -> bool:
        """True while IB has served this order more than the book has applied.

        Counts executions whose commission report has not arrived, which the
        sweep cannot book yet: right after a reconnect that is the usual state
        of a fill that completed during the outage. Resolving such an order
        first — to an absent expiry, or to a terminal status from history —
        would leave a terminal intent the sweep then refuses to book onto.
        """
        read = getattr(
            type(self._order_manager), "broker_served_fill_quantities", None
        )
        if read is None:
            return False
        served = float(read(self._order_manager).get(order_id, 0.0))
        return served - snapshot["filled_quantity"] > 1e-9

    async def _read_connection_generation(self) -> int | None:
        try:
            return int(await self._order_manager.broker_connection_generation())
        except Exception:
            self._logger.exception("Could not read the IB connection generation")
            return None

    async def _broker_session_generation(self) -> int | None:
        read = getattr(
            type(self._order_manager), "broker_session_generation", None
        )
        if read is None:
            return None
        try:
            return int(await read(self._order_manager))
        except Exception:
            self._logger.exception("Could not read the IB session generation")
            return None

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

    async def maybe_run_halt_sweep(self, now: float) -> bool:
        """Run the post-halt reconcile sweep while a halt is active.

        ``now`` is a monotonic timestamp (seconds). Returns True when the
        sweep ran.

        KAN-12's pre-submit check narrows the window between "halt lands" and
        "order reaches IB" but cannot close it: the halt can activate after
        the check and before ``placeOrder`` returns. In that window an order
        is live at the broker and, until ``record_submission`` commits, has no
        ``ib_order_id`` in the ledger — invisible to every ledger-keyed
        lookup. This sweep exists to find and cancel exactly that order.

        Independent of ``_market_calendar`` by design; see
        ``_last_halt_sweep_at``. Between halts it makes no broker call at all,
        so the steady-state cost is one halt-latch read per loop iteration.
        """
        store = self._halt_store
        if store is None:
            return False
        if await self._load_active_halt(store) is None:
            # Not halted. Reset so the next halt gets an immediate sweep
            # instead of waiting out an interval left over from the last one.
            self._last_halt_sweep_at = None
            self._halt_sweep_alerted.clear()
            return False
        last = self._last_halt_sweep_at
        if last is not None and (now - last) < HALT_SWEEP_INTERVAL_SECONDS:
            return False
        self._last_halt_sweep_at = now
        await self._sweep_orders_open_during_halt()
        return True

    async def _sweep_orders_open_during_halt(self) -> None:
        """Cancel exposure-increasing orders that are live during a halt."""
        broker_orders = await self._order_manager.list_open_broker_orders()
        for action in self._plan_halt_sweep(broker_orders):
            if not action.cancel:
                self._logger.error(
                    "Halt sweep found a buy matching no order intent",
                    order_id=action.order_id,
                    order_ref=action.order_ref,
                    ticker=action.ticker,
                )
                await self._alert_halt_sweep_once(
                    action.order_id,
                    event_type="halt_sweep_unknown_order",
                    priority="high",
                    message=(
                        f"System halted — buy order {action.order_id} "
                        f"(ref {action.order_ref!r}) is live at the broker but "
                        "matches no order intent; left alone for the operator"
                    ),
                    context={
                        "order_id": action.order_id,
                        "order_ref": action.order_ref,
                        "ticker": action.ticker,
                    },
                )
                continue

            if action.record_submission:
                # Close the exact visibility hole this sweep exists for: the
                # broker id never reached the ledger, so the Cancelled
                # callback that follows would be unattributable and the intent
                # would strand in APPROVED holding its reservation forever.
                self._record_raced_submission(action)

            self._logger.critical(
                "Halt sweep cancelling an order live during a halt",
                order_id=action.order_id,
                order_ref=action.order_ref,
                ticker=action.ticker,
            )
            cancelled = await self._order_manager.cancel_broker_order(
                action.order_id
            )
            if cancelled:
                await self._alert_halt_sweep_once(
                    action.order_id,
                    event_type="halt_sweep_order_cancelled",
                    priority="high",
                    message=(
                        f"System halted — cancelled buy {action.quantity} "
                        f"{action.ticker} (order {action.order_id}) that "
                        "reached the broker inside the halt race window"
                    ),
                    context={
                        "order_id": action.order_id,
                        "order_ref": action.order_ref,
                        "ticker": action.ticker,
                    },
                )
                continue
            # Still live at IB. Retried on the next pass; the alert fires once
            # so a wedged cancel does not page every interval.
            await self._alert_halt_sweep_once(
                action.order_id,
                event_type="halt_sweep_cancel_failed",
                priority="critical",
                message=(
                    f"System halted — could NOT cancel buy order "
                    f"{action.order_id} ({action.ticker}); it may still be "
                    "working at the broker"
                ),
                context={
                    "order_id": action.order_id,
                    "order_ref": action.order_ref,
                    "ticker": action.ticker,
                },
            )

    def _plan_halt_sweep(self, broker_orders: Any) -> list[HaltSweepAction]:
        """Classify each live broker order into cancel / alert / leave alone.

        Synchronous on purpose: every ledger read finishes and the shared
        session's transaction is closed before the caller awaits anything on
        it.

        Sells are exempt whatever the ledger says. A halt is exactly when the
        risk service publishes liquidation sells, and execution's own kill
        path submits exits that never get an intent — cancelling the emergency
        flatten is the one catastrophic outcome available here, so direction
        is checked before identity (same reasoning as KAN-12's gate).
        """
        actions: list[HaltSweepAction] = []
        try:
            for broker_order in broker_orders or ():
                order_id = str(getattr(broker_order, "order_id", "") or "")
                order_ref = str(getattr(broker_order, "order_ref", "") or "")
                ticker = str(getattr(broker_order, "ticker", "") or "")
                quantity = float(
                    getattr(broker_order, "quantity", 0.0) or 0.0
                )
                action = str(
                    getattr(broker_order, "action", "") or ""
                ).upper()
                if action != "BUY":
                    self._logger.info(
                        "Halt sweep leaving a risk-reducing sell alone",
                        order_id=order_id,
                        order_ref=order_ref,
                        ticker=ticker,
                    )
                    continue
                intent = self._intent_for_ref(order_ref)
                actions.append(
                    HaltSweepAction(
                        order_id=order_id,
                        order_ref=order_ref,
                        ticker=ticker or getattr(intent, "symbol", "") or "",
                        quantity=quantity,
                        cancel=intent is not None,
                        record_submission=(
                            intent is not None
                            and intent.ib_order_id is None
                            and intent.status == OrderStatus.APPROVED.value
                        ),
                    )
                )
        finally:
            if self._order_ledger is not None:
                self._order_ledger.session.rollback()
        return actions

    def _intent_for_ref(self, order_ref: str):
        """Map a broker ``orderRef`` back to its intent, or None."""
        if self._order_ledger is None or not order_ref:
            return None
        try:
            return self._order_ledger.get(order_ref)
        except OrderIntentNotFound:
            return None

    def _record_raced_submission(self, action: HaltSweepAction) -> None:
        """Stamp a raced order's broker id onto its APPROVED intent."""
        if self._order_ledger is None:
            return
        try:
            self._order_ledger.record_submission(
                action.order_ref, action.order_id
            )
            self._commit_ledger()
        except Exception:
            self._order_ledger.session.rollback()
            self._logger.exception(
                "Halt sweep could not record a raced broker order id; "
                "cancelling anyway",
                order_id=action.order_id,
                order_ref=action.order_ref,
            )

    async def _alert_halt_sweep_once(
        self,
        order_id: str,
        *,
        event_type: str,
        priority: str,
        message: str,
        context: dict[str, Any],
    ) -> None:
        """Publish a sweep alert at most once per order per halt."""
        key = (order_id, event_type)
        if key in self._halt_sweep_alerted:
            return
        self._halt_sweep_alerted.add(key)
        await self._publish_alert(
            event_type=event_type,
            priority=priority,
            message=message,
            context=context,
        )

    async def shutdown(self) -> None:
        """Graceful shutdown: cancel our working orders, leave stops resting.

        A GTC stop is the protection designed to outlive this process — the
        KAN-18 spike confirmed one survives a Gateway restart — so cancelling
        them on every deploy would invert the point of placing them. Working
        entries and exits still go, because those *are* ours to orphan.
        """
        self._logger.info("Execution service shutting down")
        self._running = False
        task = self._execution_sweep_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        # Recovered fills still waiting for their batch page (KAN-102).
        await self._page_recovered_fills(
            swept=[], live=self._take_unpaged_recoveries()
        )
        await self._order_manager.cancel_all_orders(include_stops=False)
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
                # IB liveness first (KAN-94): every step below that talks to
                # the broker, and every fill callback, needs a live session.
                # Best-effort except for a wrong-account session, which the
                # check re-raises on purpose.
                await self.maybe_check_ib_connection(
                    asyncio.get_running_loop().time()
                )
                # Live-path recoveries no sweep pass paged, as one page per
                # burst (KAN-102). Before the sweep starts: a running pass
                # pages them itself, so this defers to one still running.
                try:
                    await self.maybe_page_recovered_fills(
                        asyncio.get_running_loop().time()
                    )
                except Exception:
                    self._logger.exception(
                        "Paging recovered fills failed; continuing"
                    )
                # In-service execution sweep (KAN-95), started in the
                # background so a hung reqExecutions cannot delay the kill
                # stream below (KAN-98). The other IB steps stay inline.
                self._start_execution_sweep(asyncio.get_running_loop().time())
                # Periodic unfilled-order sweep (best-effort — never tear down
                # the loop on a sweep failure).
                try:
                    await self.maybe_run_unfilled_sweep(
                        asyncio.get_running_loop().time()
                    )
                except Exception:
                    self._logger.exception("Unfilled-order sweep failed; continuing")
                # Post-halt reconcile sweep — its own timer, no calendar
                # dependency. Also best-effort: a sweep failure (including an
                # unreadable halt latch) must not tear down the loop that
                # still has to process liquidation sells.
                try:
                    await self.maybe_run_halt_sweep(
                        asyncio.get_running_loop().time()
                    )
                except Exception:
                    self._logger.exception(
                        "Post-halt order sweep failed; continuing"
                    )
                # Broker-stop verification (KAN-20) — the 30-minute scan that
                # keeps a placed stop true. Best-effort like the sweeps above:
                # it must never tear down the loop that still has to process
                # fills and liquidation sells.
                try:
                    await self.maybe_run_stop_verification(
                        asyncio.get_running_loop().time()
                    )
                except Exception:
                    self._logger.exception(
                        "Broker stop verification failed; continuing"
                    )

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
                # Deferred approved orders (KAN-112), each when due. After
                # the kill read, so a retry against a hung Gateway (bounded:
                # one unanswered probe per pass) delays the NEXT pass's kill
                # read at most, never this one's. Not a background task: it
                # would race the consume path on the same recommendation and
                # both could pass the in-memory idempotency check. Best-effort
                # like the sweeps.
                try:
                    await self.maybe_retry_deferred_orders(
                        asyncio.get_running_loop().time()
                    )
                except Exception:
                    self._logger.exception(
                        "Deferred-order retry failed; continuing"
                    )

        except KeyboardInterrupt:
            self._logger.info("Execution service interrupted")
        except Exception:
            # Anything reaching here stops the service (a wrong-account IB
            # session, by design): say why, with the cause, not "interrupted".
            self._logger.exception("Execution service stopped on an error")
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
            await self._handle_message(stream, msg, parser, handler)

    async def _handle_message(
        self, stream: str, msg: Any, parser: Any, handler: Any
    ) -> str:
        """Process one message and settle it: ``"processed"`` (acked),
        ``"retained"`` (halt latch unreadable), ``"deferred"`` (IB did not
        answer the idempotency probe, or another client has the order
        working — KAN-112) or ``"dead_lettered"``."""
        try:
            await handler(parser(msg.data))
        except HaltStateUnavailable as exc:
            await self._retain_for_unknown_halt_state(
                stream, msg.message_id, exc
            )
            return "retained"
        except (SubmissionDeferredError, OrderPlacedElsewhereError) as exc:
            self._defer_message(stream, msg, parser, handler, exc)
            return "deferred"
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
            return "dead_lettered"
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
        return "processed"


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
            executor.set_connectivity_alert_handler(
                runner.handle_ib_connectivity_alert
            )
            await runner.run()
        finally:
            session.close()
            await executor.disconnect()

    asyncio.run(main())
