from __future__ import annotations

import asyncio
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from services.execution.execution_sweep import (
    RECOVERY_SOURCE_SWEEP,
    SweptExecution,
    executions_from_ib_fills,
)
from shared.broker_state import commission_in_usd, optional_float, optional_str
from shared.logging import get_logger
from shared.order_ledger import ABSENT_AT_IB_REASON

logger = get_logger("ib_executor")

# IB API system codes for server-connectivity state (delivered via errorEvent).
IB_CONNECTIVITY_LOST = 1100  # connectivity between IB and the Gateway lost
# Restored — and the two codes are NOT interchangeable. 1102 keeps the open-order
# and market-data subscriptions alive; 1101 invalidates them, so IB pushes
# nothing further for orders tracked before the outage and the client must
# re-request them. Collapsing the two cost 15 unrecorded fills on 2026-09-18.
IB_CONNECTIVITY_RESTORED_DATA_LOST = 1101
IB_CONNECTIVITY_RESTORED_DATA_MAINTAINED = 1102

# File the host Gateway watchdog reads to learn about a 1100 — the API port
# stays open during a connectivity loss, so the watchdog's port check is blind
# to it. Written under ALGO_GATEWAY_STATE_DIR (a host-bind-mounted dir).
CONNECTIVITY_MARKER_NAME = "gateway_connectivity_lost"

# Bound on one ``reqExecutions`` round trip for the in-service sweep (KAN-95).
# A Gateway that accepts the request and never answers must cost the loop one
# failed pass, not the loop.
REQ_EXECUTIONS_TIMEOUT_SECONDS = 30

# Bounds on the two order-state reads that resolve a tracked order
# (``restore_order_by_ref``). A request that does not answer in time is a
# failure, never an empty answer: an order is expired only when IB has
# *answered* that it holds it neither open nor completed (KAN-106).
REQ_OPEN_ORDERS_TIMEOUT_SECONDS = 30
REQ_COMPLETED_ORDERS_TIMEOUT_SECONDS = 30

# How long the sweep waits, after IB's reply, for the fill deliveries that
# reply set off through the live callback (KAN-102). Deliveries that finish
# first are booked unstamped by the live path instead of being raced by the
# sweep's own booking. Bounded: a stuck delivery must not hold the sweep.
DELIVERY_DRAIN_TIMEOUT_SECONDS = 5.0

# Payload passed to the fill handler on every real IB fill (partial or full).
FillHandler = Callable[[dict[str, Any]], Awaitable[None]]
OrderStatusHandler = Callable[[dict[str, Any]], Awaitable[None]]
# Optional operator page for a connectivity condition the executor cannot
# resolve on its own (Error 1101). The executor has no alert publisher of its
# own; ExecutionServiceRunner wires this to its ``_publish_alert``.
ConnectivityAlertHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class OpenBrokerOrder:
    """One order live at the broker, carrying its stable ``orderRef``.

    Deliberately distinct from :class:`~services.execution.ib_account.
    BrokerOpenOrder`, which reads the account snapshot and does not capture
    ``orderRef``. The post-halt sweep can only identify an order that never
    reached the ledger by its ref, so it needs this view.
    """

    order_id: str
    order_ref: str
    action: str
    ticker: str
    quantity: float
    account_id: str | None = None
    # Where this order triggers, and how much of it is already done (KAN-20).
    # The verifier compares the level IB is actually holding against the one
    # the ledger recorded, and coverage is the *unfilled* remainder. Optional
    # so every existing construction still builds.
    aux_price: float | None = None
    filled_quantity: float = 0.0

    @property
    def remaining_quantity(self) -> float:
        """Shares this order can still sell — the coverage it actually gives."""
        return max(0.0, self.quantity - self.filled_quantity)


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

    async def submit_stop_order(
        self,
        ticker: str,
        quantity: float,
        stop_price: float,
        recommendation_id: str | None = None,
        *,
        tif: str = "GTC",
        outside_rth: bool = False,
    ) -> str:
        """Submit a protective stop order and return the order ID."""
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
        self,
        recommendation_id: str,
        expected_order_id: str,
        *,
        expected_generation: int | None = None,
        intent_snapshot: dict[str, Any] | None = None,
    ) -> bool | None:
        """Restore callbacks; false means completed, None means missing."""
        ...

    def served_fill_quantities(self) -> dict[str, float]:
        """Highest cumulative quantity per order id IB has served, settled or not."""
        ...

    async def list_open_orders(self) -> list[OpenBrokerOrder]:
        """Enumerate every order live at the broker, with its orderRef."""
        ...

    async def completed_order_states(self) -> dict[str, str]:
        """Terminal status per orderRef, from IB completed-order history."""
        ...

    async def cancel_broker_order(self, order_id: str) -> bool:
        """Cancel a live broker order, tracked by this process or not."""
        ...

    async def broker_position(self, con_id: int) -> float:
        """Net quantity the broker reports held for one contract."""
        ...

    async def ensure_connected(self) -> bool:
        """Reconnect if the session dropped; True when a reconnect happened."""
        ...

    async def recent_executions(self) -> list[SweptExecution]:
        """This session's executions IB still serves, normalized for the sweep."""
        ...

    def mark_executions_booked(self, execution_ids: Any) -> None:
        """Never hand these already booked executions to the fill handler."""
        ...

    @property
    def connection_generation(self) -> int:
        """Count of successful connects; changes on every reconnect."""
        ...

    @property
    def session_generation(self) -> int:
        """Count of fresh sessions; a same-socket Error 1101 leaves it alone."""
        ...


class NotConnectedError(RuntimeError):
    """Raised when an order operation is attempted without an IB connection."""


class WrongAccountTypeError(RuntimeError):
    """Raised when the Gateway session's account type contradicts the mode."""


class BrokerStateUnavailableError(RuntimeError):
    """IB did not answer an order-state request; nothing may be inferred."""


class BrokerSessionChangedError(RuntimeError):
    """The IB session changed under a resolution that expected another one."""


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
        state_dir: str | Path | None = None,
        account_id: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._allow_fractional = allow_fractional
        # The one account this executor may trade. Read from executor state at
        # submission time rather than passed per order, so the submit_*
        # signatures stay as they are.
        self._account_id = account_id
        self._ib = None  # Will hold ib_insync.IB instance
        self._trades: dict[str, Any] = {}  # order_id -> ib_insync.Trade
        self._trade_meta: dict[str, tuple[str, str]] = {}  # order_id -> (ticker, side)
        # Bumped on every successful connect, so the runner can tell a
        # reconnect happened — through any path — and sweep at once (KAN-95).
        self._connection_generation = 0
        # Bumped only by connect(): a fresh IB client, whose order state was
        # rebuilt from IB. A same-socket Error 1101 bumps the connection
        # generation (so the runner sweeps) but not this, so it never starts
        # an absent-order resolution (KAN-106).
        self._session_generation = 0
        # ib_insync keys reqOpenOrders and reqAllOpenOrders on one request
        # slot ('openOrders'); a second concurrent request orphans the first.
        self._open_orders_lock = asyncio.Lock()
        # KAN-98. IB answers reqExecutions with a commissionReport per
        # execution, and ib_insync emits it into the tracked trade's
        # commissionReportEvent — the live fill callback. Executions the fill
        # handler already booked are not handed over again, and one that
        # never arrived live is stamped as recovered, whichever path books it.
        #
        # KAN-102 closed the three windows where a live fill was still
        # stamped:
        # (a) a live execution answered first by an in-flight sweep's reply
        #     (ib_insync then fires no fillEvent for the live copy) — see
        #     _seen_live and _sweep_window;
        # (b) a live execDetails processed inside connectAsync, before the
        #     trade callbacks are re-bound — the client-wide execDetailsEvent
        #     is bound before connectAsync, so it is recorded anyway;
        # (c) a restored order's earlier, already booked executions replayed
        #     by the first sweep after a restart — the runner seeds them
        #     through mark_executions_booked.
        # What remains is narrower still: a live execDetails inside
        # connectAsync for an order the wrapper does not know yet, and a fill
        # the previous process published that the projector had not applied
        # by the restart. The page keeps saying "probably", not "did".
        self._reported_exec_ids: set[str] = set()
        self._live_exec_ids: set[str] = set()
        # Window (a). Bumped on every connectivity event (1100/1101/1102):
        # after one, IB may hold executions this session never heard of.
        # _exec_record_epoch is the epoch at which the wrapper last held
        # every execution IB serves (connect's own sync, or a full sweep
        # read). While the two match, an execution the wrapper did not know
        # when a sweep request went out happened during that request on a
        # healthy session: it was live, whichever message arrived first.
        self._connectivity_epoch = 0
        self._exec_record_epoch: int | None = None
        # (IB client, epoch, execIds the wrapper knew) for the in-flight sweep
        # request; set only when the record was complete at its start.
        self._sweep_window: tuple[Any, int, frozenset[str]] | None = None
        # A replay arriving while the first delivery of the same execution is
        # still in flight is held, not handed over: it becomes that delivery's
        # retry if it fails, and is dropped if it succeeds (KAN-102).
        self._delivering_exec_ids: set[str] = set()
        self._held_replays: dict[str, dict[str, Any]] = {}
        self._delivery_tasks: set[Any] = set()
        # Serializes reconnects (KAN-98): the execution sweep runs as a
        # background task beside the main loop, so two callers can find the
        # session down at once. Unserialized, one attempt's failure path sets
        # _ib = None under the other and strands a connected client on this
        # clientId, which every later reconnect then collides with.
        self._connect_lock = asyncio.Lock()
        # Retain references to fire-and-forget callback tasks so they are not
        # garbage-collected mid-flight and their exceptions are surfaced.
        self._pending_tasks: set[Any] = set()
        self._fill_handler: FillHandler | None = None
        self._order_status_handler: OrderStatusHandler | None = None
        self._connectivity_alert_handler: ConnectivityAlertHandler | None = None
        self._expect_paper: bool | None = None
        self._logger = get_logger("ib_executor")
        # Where to drop the connectivity-lost marker for the host watchdog.
        # Defaults to ALGO_GATEWAY_STATE_DIR; None disables the observer.
        if state_dir is None:
            state_dir = os.environ.get("ALGO_GATEWAY_STATE_DIR")
        self._conn_marker: Path | None = (
            Path(state_dir) / CONNECTIVITY_MARKER_NAME if state_dir else None
        )

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

    def _stamp_account(self, order: Any) -> None:
        """Bind the order to the configured account at the broker.

        Left untouched when unpinned, so the Gateway keeps picking the
        session's account exactly as it did before.
        """
        if self._account_id is not None:
            order.account = self._account_id

    @property
    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    @property
    def connection_generation(self) -> int:
        return self._connection_generation

    @property
    def session_generation(self) -> int:
        return self._session_generation

    def _on_ib_error(
        self, reqId: int, errorCode: int, errorString: str, contract: Any = None
    ) -> None:
        """ib_insync ``errorEvent`` handler: track server connectivity.

        Error 1100 means the Gateway lost its link to IB while the API socket
        (and port) stay up — invisible to the watchdog's port check. Drop a
        marker the host watchdog reads; clear it when connectivity is restored
        (1101/1102). Best-effort: marker I/O must never disturb order routing.

        1101 and 1102 both mean "restored" and both clear the marker, but only
        1102 means the subscriptions survived. A 1101 says IB threw them away,
        and because the socket never dropped, ``connect()`` — and with it
        :meth:`_reregister_open_trades` — never re-runs. Left alone the
        executor goes silently blind to every later fill on its tracked orders,
        which is what happened on 2026-09-18. So a 1101 re-requests open orders
        off the event loop; nothing here may raise into ib_insync's dispatch.
        """
        if errorCode in (
            IB_CONNECTIVITY_LOST,
            IB_CONNECTIVITY_RESTORED_DATA_LOST,
            IB_CONNECTIVITY_RESTORED_DATA_MAINTAINED,
        ):
            # IB may have executed while the Gateway was cut off (KAN-102).
            self._connectivity_epoch += 1
        if errorCode == IB_CONNECTIVITY_LOST:
            self._mark_connectivity_lost()
        elif errorCode == IB_CONNECTIVITY_RESTORED_DATA_MAINTAINED:
            self._clear_connectivity_marker()
            self._logger.info(
                "IB connectivity restored, data maintained (Error 1102) — "
                "subscriptions survived, nothing to re-request",
                error_code=errorCode,
            )
        elif errorCode == IB_CONNECTIVITY_RESTORED_DATA_LOST:
            self._clear_connectivity_marker()
            self._logger.warning(
                "IB connectivity restored but DATA LOST (Error 1101) — "
                "re-requesting open orders; fills that completed during the "
                "outage may be missing and need broker reconciliation",
                error_code=errorCode,
            )
            self._schedule_resubscribe_after_data_loss(errorCode)

    def _schedule_resubscribe_after_data_loss(self, error_code: int) -> None:
        """Hand the async recovery to the event loop, swallowing every failure.

        ``_on_ib_error`` is a synchronous ib_insync callback, so the work is
        fire-and-forget via :meth:`_spawn`. Outside a running loop (unit tests,
        a callback delivered off-loop) there is nothing to schedule — say so
        rather than raising into order routing.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._logger.error(
                "No running event loop — could not re-subscribe after IB "
                "Error 1101; verify fills via broker reconciliation",
                error_code=error_code,
            )
            return
        try:
            self._spawn(self._resubscribe_after_data_loss(error_code))
        except Exception:  # pragma: no cover - defensive
            self._logger.exception(
                "Could not schedule re-subscription after IB Error 1101"
            )

    async def _resubscribe_after_data_loss(self, error_code: int) -> None:
        """Rebuild the open-order subscription IB dropped across a 1101.

        ``reqOpenOrders`` is client-scoped and is what makes IB resume pushing
        status and execution updates for this session's orders; the trades it
        returns are IB's authoritative view of what is still working, which
        ``openTrades()`` is not — that reads ib_insync's local cache, where an
        order that filled unobserved is still sitting at ``Submitted``.

        Orders absent from that answer are logged by
        :meth:`_reregister_open_trades` and left tracked. They are NOT
        terminalized: an order that vanished across a 1101 may well have
        filled, and guessing "cancelled" is exactly how 15 filled positions
        became phantoms on 2026-09-18. The connection-generation bump below
        makes the runner sweep executions; the session generation is left
        alone, so a 1101 never starts the post-reconnect absent-order
        resolution (KAN-106) — that runs only after connect() has built a
        fresh client.
        """
        await self._page_connectivity_data_lost(error_code)
        if self._ib is None:
            return
        try:
            ib = self._ib
            open_trades = await self._open_orders_request(
                ib.reqOpenOrdersAsync, "open orders (1101)"
            )
        except Exception:
            self._logger.exception(
                "Failed to re-request open orders after IB Error 1101 — the "
                "executor may be blind to fills; verify via broker "
                "reconciliation",
                error_code=error_code,
            )
            return
        self._reregister_open_trades(open_trades=open_trades)
        # A 1101 is the blind-fill case (2026-09-18): treat it as a reconnect
        # so the execution sweep runs on the next loop iteration (KAN-95).
        self._connection_generation += 1

    async def _page_connectivity_data_lost(self, error_code: int) -> None:
        """Best-effort operator page; a dead alert path must not stop recovery."""
        if self._connectivity_alert_handler is None:
            return
        try:
            await self._connectivity_alert_handler({
                "error_code": error_code,
                "host": self._host,
                "port": self._port,
                "tracked_order_ids": sorted(self._trade_meta),
            })
        except Exception:
            self._logger.exception(
                "Failed to publish IB Error 1101 alert", error_code=error_code
            )

    def _mark_connectivity_lost(self) -> None:
        if self._conn_marker is None:
            return
        try:
            self._conn_marker.parent.mkdir(parents=True, exist_ok=True)
            # Line 1 is the bare loss epoch and must stay that way: every reader
            # of this file, including any deployed copy of the watchdog older
            # than KAN-63, parses it with a plain `head -1`. The `key=value`
            # tail is additive.
            #
            # Execution runs in a container and cannot see the host Gateway's
            # process identity, so it records only what it can observe: who
            # wrote the marker and which Gateway endpoint the 1100 came from.
            # The watchdog stamps `gateway_pid` / `gateway_started_at` on first
            # observation — it is the only party that can — and uses them to
            # drop a latch that has outlived the session that raised it.
            self._conn_marker.write_text(
                f"{int(time.time())}\n"
                f"writer=execution\n"
                f"gateway_endpoint={self._host}:{self._port}\n"
            )
            self._logger.warning(
                "IB connectivity lost (Error 1100) — wrote watchdog marker",
                marker=str(self._conn_marker),
            )
        except Exception:
            self._logger.exception("Failed to write connectivity-lost marker")

    def _clear_connectivity_marker(self) -> None:
        if self._conn_marker is None:
            return
        try:
            self._conn_marker.unlink(missing_ok=True)
        except Exception:
            self._logger.exception("Failed to clear connectivity-lost marker")

    def set_fill_handler(self, handler: FillHandler) -> None:
        """Register the async callback invoked on every real IB fill."""
        self._fill_handler = handler

    def set_order_status_handler(self, handler: OrderStatusHandler) -> None:
        """Register the callback for broker lifecycle status changes."""
        self._order_status_handler = handler

    def set_connectivity_alert_handler(
        self, handler: ConnectivityAlertHandler
    ) -> None:
        """Register the async callback that pages an operator on Error 1101."""
        self._connectivity_alert_handler = handler

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

        When ``account_id`` was configured, the session must additionally
        serve exactly that one account. The prefix guard proves the account
        *type*; only this proves its identity.
        """
        self._expect_paper = expect_paper
        try:
            from ib_insync import IB

            # The wrapper about to be built holds nothing yet: no sweep may
            # treat it as a complete record until this connect proves it
            # (window a). The epoch at the start tells whether a connectivity
            # event landed during the handshake.
            self._exec_record_epoch = None
            epoch_at_start = self._connectivity_epoch
            self._ib = IB()
            # Both bound before connectAsync, once per IB instance (each connect
            # builds a fresh one):
            # - server-connectivity events (Error 1100/1101/1102), so one
            #   during the handshake is counted and the execution record is not
            #   wrongly marked complete (KAN-102);
            # - the client-wide execDetailsEvent (KAN-102, window b): a live
            #   execution processed during connect's own sync fires the fresh
            #   Trade's fillEvent before our callbacks are re-bound to it, but
            #   always fires this one.
            self._ib.errorEvent += self._on_ib_error
            exec_details_event = getattr(self._ib, "execDetailsEvent", None)
            if exec_details_event is not None:
                exec_details_event += self._on_live_exec_details
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
            elif expect_paper is False:
                # Mirror guard for live: refuse a paper (DU) session on the live
                # port so a mis-login can never trade the wrong book.
                paper = [a for a in accounts if a.startswith("DU")]
                if paper:
                    self._ib.disconnect()
                    self._ib = None
                    raise WrongAccountTypeError(
                        f"Live mode but the Gateway session holds PAPER "
                        f"account(s) {paper} on port {self._port}. "
                        "Re-login the Gateway with the live credentials."
                    )

            if self._account_id is not None and list(accounts) != [
                self._account_id
            ]:
                # Exactly one, and exactly the right one. A second account in
                # the session is refused even when the configured one is
                # present: an ambiguous session is how orders reach the wrong
                # book (the same rule IBAccountReader.snapshot() enforces).
                self._ib.disconnect()
                self._ib = None
                raise WrongAccountTypeError(
                    f"Configured to trade account {self._account_id!r} but the "
                    f"Gateway session on port {self._port} serves "
                    f"{list(accounts)!r}. Re-point the Gateway at "
                    f"{self._account_id} or correct ib.account_id."
                )

            # A reconnect recreates the IB client, orphaning the fill/status
            # callbacks bound to the previous Trade objects. Re-register them for
            # every still-open tracked order so a mid-session reconnect keeps
            # delivering their fills. (First connect has no tracked trades →
            # no-op. Orders that completed during the outage are logged for
            # reconciliation — see _reregister_open_trades.)
            self._reregister_open_trades()
            self._connection_generation += 1
            self._session_generation += 1
            # connectAsync returns only after its own reqExecutions, so the
            # wrapper now holds every execution IB serves (window a) — unless
            # a connectivity event arrived during the handshake, in which case
            # the record stays incomplete until a full sweep read.
            if self._connectivity_epoch == epoch_at_start:
                self._exec_record_epoch = epoch_at_start

            # A healthy session proves server connectivity: clear any stale
            # lost-marker left by a socket that dropped without a 1102.
            self._clear_connectivity_marker()
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

    def _reregister_open_trades(self, open_trades: list[Any] | None = None) -> None:
        """Re-bind fill/status callbacks onto the fresh Trade objects after a
        reconnect, for every tracked order still open at IB.

        This recovers callbacks for orders that are STILL OPEN across the
        reconnect. An order that reached a terminal state *during* the outage no
        longer appears in ``openTrades()``, so its fill/status can't be replayed
        here — that divergence is caught by the daily broker reconciliation
        (``scripts/reconcile_paper.py``). Such orders are logged as a warning so
        the gap is visible rather than silent.

        ``open_trades`` lets the Error 1101 path pass the trades IB just
        returned from ``reqOpenOrders`` instead of the local ``openTrades()``
        cache. Over a socket that never dropped, that cache still shows an
        order that filled unobserved as open, which would hide exactly the gap
        this warns about.
        """
        if self._ib is None or not self._trade_meta:
            return
        if open_trades is None:
            try:
                open_trades = self._own_open_trades()
            except Exception:  # pragma: no cover - defensive
                self._logger.exception("Could not list open trades on reconnect")
                return
        open_trades = [t for t in open_trades if self._is_own(t)]
        open_ids = set()
        reattached = 0
        for trade in open_trades:
            order = getattr(trade, "order", None)
            order_id = str(getattr(order, "orderId", "") or "")
            meta = self._trade_meta.get(order_id)
            if meta is None:
                continue
            open_ids.add(order_id)
            if self._trades.get(order_id) is trade:
                # Same Python object: ib_insync keys its Trade cache on
                # (clientId, orderId), so a re-request over a live socket hands
                # back the object we are already bound to. eventkit invokes a
                # listener once per registration, so binding again would double
                # every subsequent fill.
                continue
            ticker, side = meta
            self._register_trade(order_id, trade, ticker, side)
            reattached += 1
        if reattached:
            self._logger.info(
                "Re-registered IB callbacks after reconnect", count=reattached
            )
        # Tracked orders that vanished across the reconnect may have completed
        # during the outage; their fills cannot be replayed via callbacks. The
        # runner resolves them through restore_order_by_ref once the
        # post-reconnect execution sweep has booked any fill IB still serves
        # (KAN-106) — not here, where neither the sweep nor the
        # recommendation_id each order needs has been seen.
        missing = [oid for oid in self._trade_meta if oid not in open_ids]
        if missing:
            self._logger.warning(
                "Tracked orders absent after reconnect — verify via broker "
                "reconciliation (fills may have completed during the outage); "
                "the ones the book still holds working are resolved against "
                "IB's order history after the execution sweep",
                order_ids=missing,
            )

    def _spawn(self, coro: Any) -> Any:
        """Schedule a fire-and-forget callback coroutine with a done-callback so
        its exception is logged instead of being swallowed by the event loop."""
        task = asyncio.ensure_future(coro)
        self._pending_tasks.add(task)

        def _done(t: Any) -> None:
            self._pending_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                self._logger.error(
                    "Async IB callback task failed", error=str(exc)
                )

        task.add_done_callback(_done)
        return task

    async def ensure_connected(self) -> bool:
        """Reconnect now if the Gateway dropped the session (KAN-94).

        The execution loop calls this on a timer so a Gateway restart is
        healed before the open, not at the next order. Every other caller
        reconnects lazily through :meth:`_ensure_connected`, which is why a
        restart between the 04:21 placement and the 21:30 SGT open on
        2026-09-25 left IB's fill for XLC talking to a dead socket.

        Returns True when a reconnect happened and False when the session was
        already up; a connected session costs one ``isConnected()`` read and
        no IB request. Raises what :meth:`_ensure_connected` raises.
        """
        if self.is_connected:
            return False
        await self._ensure_connected()
        return True

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
        async with self._connect_lock:
            # Another caller may have reconnected while this one waited.
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
        # Remember (ticker, side) so callbacks can be re-registered onto a fresh
        # Trade object after a reconnect recreates the IB client.
        self._trade_meta[order_id] = (ticker, side)

        def _on_live_fill(trade: Any, fill: Any) -> None:
            self._live_exec_ids.add(str(fill.execution.execId))

        fill_event = getattr(trade, "fillEvent", None)
        if fill_event is not None:
            fill_event += _on_live_fill

        def _on_commission_report(
            trade: Any, fill: Any, commission_report: Any
        ) -> None:
            exec_id = str(fill.execution.execId)
            if exec_id in self._reported_exec_ids:
                return  # a reqExecutions replay of one already handed over
            commission = float(
                getattr(commission_report, "commission", 0.0) or 0.0
            )
            commission_currency = str(
                getattr(commission_report, "currency", "") or ""
            )
            commission_fx_base_per_trading = (
                self._usd_exchange_rate() if commission_currency == "SGD" else None
            )
            payload = {
                "execution_id": str(fill.execution.execId),
                "account_id": str(fill.execution.acctNumber),
                "timestamp": fill.execution.time,
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
                "commission_trading": commission_in_usd(
                    commission,
                    commission_currency,
                    fx_base_per_trading=commission_fx_base_per_trading,
                ),
                "commission_fx_base_per_trading": (
                    commission_fx_base_per_trading
                ),
                "order_done": trade.isDone(),
            }
            if not self._seen_live(exec_id):
                payload["recovery_source"] = RECOVERY_SOURCE_SWEEP
            if self._fill_handler is None:
                self._logger.warning("IB fill received but no handler set", **payload)
                return
            if exec_id in self._delivering_exec_ids:
                # The first delivery is still in flight. Handing this copy
                # over too only reaches the runner's dedupe; hold it as that
                # delivery's retry instead (latest copy wins).
                self._held_replays[exec_id] = payload
                return
            self._deliver_fill(exec_id, payload, self._fill_handler)

        trade.commissionReportEvent += _on_commission_report

        def _on_status(trade: Any) -> None:
            if self._order_status_handler is None:
                return
            status = str(trade.orderStatus.status)
            reason = self._status_reason(trade)
            self._spawn(
                self._emit_order_status(
                    order_id,
                    status,
                    reason,
                    filled_quantity=float(trade.orderStatus.filled or 0.0),
                )
            )

        trade.statusEvent += _on_status

    def _on_live_exec_details(self, trade: Any, fill: Any) -> None:
        """ib_insync ``execDetailsEvent``: fired only for a live execution."""
        self._live_exec_ids.add(str(fill.execution.execId))

    def _seen_live(self, exec_id: str) -> bool:
        """Whether this execution reached the session live (KAN-98/102).

        Besides the execIds recorded live, an execution the wrapper first
        heard of during the in-flight sweep request counts as live, provided
        the wrapper's record was complete when the request went out, no
        connectivity event has happened since, and the callback comes from
        the same IB client. Any condition failing errs towards the stamp: a
        false "probably" page is the safe side, a silent miss is not.
        """
        if exec_id in self._live_exec_ids:
            return True
        window = self._sweep_window
        if window is None:
            return False
        ib, epoch, known = window
        return (
            ib is self._ib
            and epoch == self._connectivity_epoch
            and exec_id not in known
        )

    def _deliver_fill(
        self, exec_id: str, payload: dict[str, Any], handler: FillHandler
    ) -> None:
        """Hand one execution to the fill handler as a tracked task."""
        self._delivering_exec_ids.add(exec_id)

        async def _deliver() -> None:
            try:
                await handler(payload)
            except asyncio.CancelledError:
                # Teardown: never respawn a delivery into a shutdown. The held
                # copy goes too; IB keeps the execution, and the next process's
                # sweep reads it back.
                self._delivering_exec_ids.discard(exec_id)
                self._held_replays.pop(exec_id, None)
                raise
            except Exception:
                # A failed delivery: a copy held meanwhile is its retry. Only
                # one retry per held copy; a later replay gets through as usual.
                self._delivering_exec_ids.discard(exec_id)
                retry = self._held_replays.pop(exec_id, None)
                if retry is not None:
                    self._deliver_fill(exec_id, retry, handler)
                raise
            # Marked only once the handler succeeded: a duplicate delivery
            # after a failed publish must still get through, because it is
            # that fill's retry.
            self._reported_exec_ids.add(exec_id)
            self._delivering_exec_ids.discard(exec_id)
            self._held_replays.pop(exec_id, None)

        task = self._spawn(_deliver())
        self._delivery_tasks.add(task)
        task.add_done_callback(self._delivery_tasks.discard)

    async def _drain_fill_deliveries(self) -> None:
        """Wait, bounded, for fill deliveries already under way (KAN-102)."""
        pending = [task for task in self._delivery_tasks if not task.done()]
        if not pending:
            return
        _, still_running = await asyncio.wait(
            pending, timeout=DELIVERY_DRAIN_TIMEOUT_SECONDS
        )
        if still_running:
            self._logger.warning(
                "Fill deliveries still running after the execution sweep's "
                "reply; the sweep proceeds without them",
                still_running=len(still_running),
            )

    def mark_executions_booked(self, execution_ids: Any) -> None:
        """Treat these executions as already handed over (KAN-102, window c).

        For a restarted process: the runner seeds the execIds the book
        already holds for the orders it restores, so the first sweep's replay
        of a partly filled order's earlier executions is dropped here instead
        of being handed over stamped, only to be dropped by the runner's
        dedupe. Only durably booked executions may be passed.
        """
        self._reported_exec_ids.update(str(e) for e in execution_ids)

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

    async def completed_order_states(self) -> dict[str, str]:
        """Terminal status per ``orderRef``, from IB's completed-order history.

        The set IB calls "completed" is the set of orders that are *done* —
        ``Filled``, ``Cancelled``, ``ApiCancelled``, ``Expired`` — which makes
        it the only place that can tell a stop somebody cancelled from a stop
        that filled (KAN-20). :meth:`find_order_by_ref` deliberately collapses
        both into "an order exists", because its callers only need to avoid
        double-placing; a verifier deciding whether protection is *gone* needs
        the status itself.

        One account-wide request, so callers fetch it once per pass rather
        than once per order. Same-day only: IB rolls the history at the
        session boundary, so an order cancelled yesterday appears in neither
        this nor the open-order book.
        """
        await self._ensure_connected()
        completed = await self._ib.reqCompletedOrdersAsync(apiOnly=False)
        states: dict[str, str] = {}
        for trade in completed:
            ref = optional_str(getattr(trade.order, "orderRef", None))
            status = optional_str(
                getattr(getattr(trade, "orderStatus", None), "status", None)
            )
            if ref is not None and status is not None:
                states[ref] = status
        return states

    async def find_order_by_ref(self, recommendation_id: str) -> str | None:
        """Recover an IB-accepted order from its stable recommendation ref."""
        await self._ensure_connected()
        trades = self._own_open_trades()
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

    async def list_open_orders(self) -> list[OpenBrokerOrder]:
        """Enumerate every order live at the broker, with its ``orderRef``.

        Ledger-independent on purpose: the post-halt sweep exists to find an
        order whose broker id never reached the ledger, so no ledger-keyed
        lookup can see it. Nothing is registered as a tracked trade here —
        binding our fill/status callbacks to an order this process did not
        place would corrupt attribution.

        Scoped to this client's orders, as ``openTrades()`` always was: the
        KAN-106 resolution's ``reqAllOpenOrders`` leaves other clients'
        orders in that cache, and widening what the halt sweep may cancel is
        a decision of its own (see ``broker_stops`` ``_confirm_absent``).
        """
        await self._ensure_connected()
        orders: list[OpenBrokerOrder] = []
        for trade in self._own_open_trades():
            order = trade.order
            orders.append(
                OpenBrokerOrder(
                    order_id=str(order.orderId),
                    order_ref=str(getattr(order, "orderRef", "") or ""),
                    action=str(getattr(order, "action", "") or "").upper(),
                    ticker=str(getattr(trade.contract, "symbol", "") or ""),
                    quantity=float(getattr(order, "totalQuantity", 0.0) or 0.0),
                    account_id=(
                        str(getattr(order, "account", "") or "") or None
                    ),
                    aux_price=optional_float(getattr(order, "auxPrice", None)),
                    filled_quantity=float(
                        getattr(
                            getattr(trade, "orderStatus", None), "filled", 0.0
                        )
                        or 0.0
                    ),
                )
            )
        return orders

    async def cancel_broker_order(self, order_id: str) -> bool:
        """Cancel an order that is live at IB, tracked by this process or not.

        :meth:`cancel_order` can only cancel what this process placed — after
        a restart ``_trades`` is empty while the order is still working at the
        broker. A halt-safety path cannot depend on in-process memory, so this
        falls back to the order object IB itself reports as open.
        """
        await self._ensure_connected()
        trade = self._trades.get(order_id)
        if trade is None:
            trade = next(
                (
                    open_trade
                    for open_trade in self._own_open_trades()
                    if str(open_trade.order.orderId) == str(order_id)
                ),
                None,
            )
        if trade is None:
            self._logger.warning(
                "Cancel requested for an order not open at IB",
                order_id=order_id,
            )
            return False
        self._ib.cancelOrder(trade.order)
        self._logger.info("Order cancel requested", order_id=order_id)
        return True

    def _usd_exchange_rate(self) -> float | None:
        """IB's ``ExchangeRate`` account value for USD, or None when unusable.

        Exactly one USD row, parseable, finite and positive. None means
        "absent", never "one": a commission that cannot be translated is
        deferred by the sweep rather than booked wrong.
        """
        if self._ib is None:
            return None
        rows = [
            row
            for row in (self._ib.accountValues() or ())
            if getattr(row, "tag", None) == "ExchangeRate"
            and getattr(row, "currency", None) == "USD"
        ]
        if len(rows) != 1:
            return None
        try:
            candidate = float(rows[0].value)
        except (TypeError, ValueError):
            return None
        return candidate if math.isfinite(candidate) and candidate > 0 else None

    async def recent_executions(self) -> list[SweptExecution]:
        """The executions IB still serves to this session (KAN-95).

        ``reqExecutions`` is clientId-scoped, and this is the client that
        placed the orders, so — unlike the old 04:15 sweep on client 58 — it
        needs no Master API client ID. IB serves only the current day's
        executions; the runner calls this hourly and after every reconnect,
        so a fill is read the same evening it happens.
        """
        from ib_insync import ExecutionFilter

        await self._ensure_connected()
        ib = self._ib
        epoch = self._connectivity_epoch
        known = self._known_exec_ids(ib)
        window = None
        if self._exec_record_epoch == epoch:
            # Window (a): see _seen_live.
            window = (ib, epoch, known)
            self._sweep_window = window
        try:
            fills = await asyncio.wait_for(
                ib.reqExecutionsAsync(ExecutionFilter()),
                REQ_EXECUTIONS_TIMEOUT_SECONDS,
            )
        finally:
            if window is not None and self._sweep_window is window:
                self._sweep_window = None
        if self._ib is ib and self._connectivity_epoch == epoch:
            if window is not None:
                # New during the request on a healthy session, so live; their
                # commission reports may still be on the way.
                self._live_exec_ids.update(
                    str(fill.execution.execId)
                    for fill in fills
                    if str(fill.execution.execId) not in known
                )
            # The wrapper now holds IB's whole record, read after the last
            # connectivity event.
            self._exec_record_epoch = epoch
        # The reply's commission reports went to the live callback first; let
        # those deliveries finish so the live path, not the sweep, books them.
        await self._drain_fill_deliveries()
        # ib_insync returns a FRESH Fill with an empty CommissionReport for an
        # execution its wrapper already stored (connect's own startup sync
        # stores every one); IB's commissionReport only ever updates the
        # stored Fill. Read the stored one, and leave any execution whose
        # report has not arrived for the next pass — booking it now would
        # write commission 0 into the immutable execution_fills row.
        stored = getattr(getattr(self._ib, "wrapper", None), "fills", None) or {}
        settled = []
        for fill in fills:
            fill = stored.get(fill.execution.execId, fill)
            report = getattr(fill, "commissionReport", None)
            if not getattr(report, "execId", ""):
                continue
            settled.append(fill)
        return executions_from_ib_fills(
            settled, fx_base_per_trading=self._usd_exchange_rate()
        )

    @staticmethod
    def _known_exec_ids(ib: Any) -> frozenset[str]:
        fills = getattr(getattr(ib, "wrapper", None), "fills", None)
        if not isinstance(fills, dict):
            return frozenset()
        return frozenset(str(exec_id) for exec_id in fills)

    async def broker_position(self, con_id: int) -> float:
        """Net quantity IB reports held for ``con_id`` on this account.

        The broker is authoritative on what can be sold: the durable book
        lags every fill the projector has not applied yet, and an emergency
        sell sized off that lag is how an unlevered long-only account ends up
        short (KAN-10). Live-requested rather than read from ib_insync's
        cached ``positions()`` — the guard runs during exactly the incidents
        where a stale subscription is most likely.

        Scoped to the configured account (KAN-11): one Gateway session can
        report positions for several accounts, and counting a foreign one
        would license a sell this account cannot cover. With no account
        configured there is nothing to filter on, so what the session reports
        is the best available answer.
        """
        await self._ensure_connected()
        total = 0.0
        for item in await self._ib.reqPositionsAsync():
            if (
                self._account_id is not None
                and str(getattr(item, "account", "") or "") != self._account_id
            ):
                continue
            contract = getattr(item, "contract", None)
            if contract is None:
                continue
            if int(getattr(contract, "conId", 0) or 0) != int(con_id):
                continue
            total += float(getattr(item, "position", 0.0) or 0.0)
        return total

    async def restore_order_by_ref(
        self,
        recommendation_id: str,
        expected_order_id: str,
        *,
        expected_generation: int | None = None,
        intent_snapshot: dict[str, Any] | None = None,
    ) -> bool | None:
        """Reattach callbacks, or reconcile a terminal completed order.

        The one resolution path for a tracked order, run at startup
        (``OrderManager.restore_broker_tracking``) and after a reconnect
        (``OrderManager.resolve_tracked_order``, KAN-106): still open →
        callbacks attached, never twice; in completed-order history → its
        true terminal status; in neither → expired with
        ``ABSENT_AT_IB_REASON`` (KAN-96). Each outcome is logged.

        "Open" is IB's answer to ``reqAllOpenOrders``, never ib_insync's
        ``openTrades()`` cache: ``connectAsync`` gives its own open-orders
        request a few seconds and on a timeout only logs, so after a connect
        the cache can be empty or partial while the order is still working.
        The account-wide request also sees an order a repair tool placed on
        another client id. Either request failing or timing out raises
        :class:`BrokerStateUnavailableError` — an order is expired only on an
        answer, never on a silence.

        ``expected_generation`` (reconnect path) is the connection generation
        the caller's execution sweep ran on; if the session changes before an
        outcome is reported — including through this method's own
        reconnect — :class:`BrokerSessionChangedError` is raised and nothing
        is reported. ``intent_snapshot`` rides on an absent expiry so the
        status handler can refuse it when the intent moved since the caller
        read it.
        """
        await self._ensure_connected()
        self._require_generation(expected_generation)
        ib = self._ib
        open_trades = await self._open_orders_request(
            ib.reqAllOpenOrdersAsync, "open orders"
        )
        self._require_generation(expected_generation)
        for trade in open_trades:
            if str(getattr(trade.order, "orderRef", "")) != recommendation_id:
                continue
            order_id = str(trade.order.orderId)
            own = self._is_own(trade)
            if order_id != str(expected_order_id):
                if not own:
                    # Another client's order under the same ref is a
                    # different order (its ids are that client's), not a
                    # contradiction of ours — but someone placed an order
                    # for our recommendation, which must not pass silently.
                    self._logger.warning(
                        "Another client holds an order under this "
                        "recommendation's orderRef; it is not ours and is "
                        "ignored",
                        recommendation_id=recommendation_id,
                        expected_order_id=str(expected_order_id),
                        other_order_id=order_id,
                        other_client_id=getattr(trade.order, "clientId", None),
                    )
                    continue
                raise RuntimeError(
                    f"orderRef {recommendation_id} maps to broker order "
                    f"{order_id}, expected {expected_order_id}"
                )
            # Whatever its status (an Inactive order included), an order IB
            # still lists is not absent and is never expired here.
            action = str(getattr(trade.order, "action", "")).lower()
            # Same-object guard: a trade already bound (connect re-binds what
            # its own cache held) is never bound twice; a stale binding from a
            # previous client is replaced. Another client's order is left
            # unbound — its fills are not ours to attribute.
            reattached = own and self._trades.get(order_id) is not trade
            if reattached:
                self._register_trade(
                    order_id,
                    trade,
                    ticker=str(trade.contract.symbol),
                    side="buy" if action == "buy" else "sell",
                )
            self._logger.info(
                "Tracked order still open at IB",
                order_id=order_id,
                recommendation_id=recommendation_id,
                status=str(getattr(trade.orderStatus, "status", "") or ""),
                outcome=(
                    "reattached" if reattached
                    else "still_open" if own
                    else "open_on_another_client"
                ),
            )
            return True

        completed = await self._answer_or_raise(
            ib.reqCompletedOrdersAsync(apiOnly=False),
            REQ_COMPLETED_ORDERS_TIMEOUT_SECONDS,
            "completed orders",
        )
        self._require_generation(expected_generation)
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
            self._logger.info(
                "Tracked order resolved from IB completed-order history",
                order_id=str(expected_order_id),
                recommendation_id=recommendation_id,
                status=status,
                outcome="terminal_from_history",
            )
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
            self._forget_trade(str(expected_order_id))
            return False

        # Absent from both open orders and completed-order history. IB does
        # not retain order state across session boundaries, so a day order
        # that filled or expired before a restart is simply gone the next
        # session — the normal case, not a fault. Terminalize it (EXPIRED) via
        # the status handler so the ledger intent stops wedging restarts and
        # reconciliation. Position-level safety (a fill missed while
        # disconnected) is caught independently by the reconciler's
        # broker-vs-DB position comparison.
        # Without a handler there is no safe terminalization path, so preserve
        # the fail-closed None (the caller raises).
        if self._order_status_handler is not None:
            self._logger.warning(
                "Tracked order absent from IB open orders and completed-order "
                "history — expiring it",
                order_id=str(expected_order_id),
                recommendation_id=recommendation_id,
                reason=ABSENT_AT_IB_REASON,
                outcome="expired_absent",
            )
            payload: dict[str, Any] = {
                "order_id": str(expected_order_id),
                "status": "Expired",
                "reason": ABSENT_AT_IB_REASON,
                "order_absent_at_ib": True,
            }
            if intent_snapshot is not None:
                payload["intent_snapshot"] = dict(intent_snapshot)
            await self._order_status_handler(payload)
            self._forget_trade(str(expected_order_id))
            return False
        return None

    def _require_generation(self, expected: int | None) -> None:
        if expected is not None and self._connection_generation != expected:
            raise BrokerSessionChangedError(
                f"IB session changed (generation {self._connection_generation}, "
                f"expected {expected})"
            )

    async def _open_orders_request(
        self, request: Callable[[], Any], what: str
    ) -> list:
        """One open-orders request, serialized and bounded end to end.

        ib_insync keys reqOpenOrders and reqAllOpenOrders on one request slot,
        so they take turns under ``_open_orders_lock``. Waiting for the lock
        counts against the same bound as the request: a hung 1101
        re-subscription must cost the resolution one failed attempt, never
        the sweep task (which then never finishes, so no later pass starts).
        The request is issued only once the lock is held.
        """

        async def locked() -> list:
            async with self._open_orders_lock:
                return list(await request())

        return await self._answer_or_raise(
            locked(), REQ_OPEN_ORDERS_TIMEOUT_SECONDS, what
        )

    def _is_own(self, trade: Any) -> bool:
        """Whether this client placed ``trade``.

        ``reqAllOpenOrders`` leaves other clients' orders in ib_insync's
        trade cache as Trades that never get a status update on this
        (non-master) client, so every reader of ``openTrades()`` keeps to its
        own client's orders — what that cache held before KAN-106.
        """
        client_id = getattr(getattr(trade, "order", None), "clientId", None)
        if not isinstance(client_id, int):
            return True
        return client_id == self._client_id

    def _own_open_trades(self) -> list[Any]:
        return [t for t in self._ib.openTrades() if self._is_own(t)]

    @staticmethod
    async def _answer_or_raise(request: Any, timeout: float, what: str) -> list:
        try:
            return list(await asyncio.wait_for(request, timeout))
        except Exception as exc:
            raise BrokerStateUnavailableError(
                f"IB did not answer the {what} request: {exc!r}"
            ) from exc

    def _forget_trade(self, order_id: str) -> None:
        """Stop tracking an order IB reported terminal or no longer knows.

        Its callbacks stay bound to the trade object, so a late event on it
        is still delivered (they close over the order's identity and never
        read these maps); only the reconnect re-binding and the
        absent-after-reconnect warning stop carrying it.
        """
        self._trades.pop(order_id, None)
        self._trade_meta.pop(order_id, None)

    def served_fill_quantities(self) -> dict[str, float]:
        """Highest cumulative quantity per broker order id IB has served.

        Read from the wrapper's execution record — connect's own sync plus
        every ``reqExecutions`` reply on this client — *including* executions
        whose commission report has not arrived, which :meth:`recent_executions`
        leaves for a later pass. Right after a reconnect that is the usual
        state of a fill that completed during the outage, and resolving its
        order first (KAN-106) would expire it, or terminalize it from history
        so that the sweep could no longer book the fill.
        """
        fills = getattr(getattr(self._ib, "wrapper", None), "fills", None)
        if not isinstance(fills, dict):
            return {}
        served: dict[str, float] = {}
        for fill in list(fills.values()):
            execution = getattr(fill, "execution", None)
            if execution is None:
                continue
            if (
                self._account_id is not None
                and str(getattr(execution, "acctNumber", "") or "")
                != self._account_id
            ):
                continue
            order_id = str(getattr(execution, "orderId", "") or "")
            if not order_id:
                continue
            served[order_id] = max(
                served.get(order_id, 0.0),
                float(getattr(execution, "cumQty", 0.0) or 0.0),
            )
        return served

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
        from ib_insync import LimitOrder

        from shared.universe import make_stock_contract

        contract = make_stock_contract(ticker)
        # Explicit TIF: without it the order inherits the TWS desktop preset,
        # which can mutate/cancel API orders (Error 10349 observed).
        order = LimitOrder("BUY", quantity, limit_price, tif="DAY")
        if recommendation_id is not None:
            order.orderRef = recommendation_id
        self._stamp_account(order)
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
        from ib_insync import MarketOrder

        from shared.universe import make_stock_contract

        contract = make_stock_contract(ticker)
        order = MarketOrder("SELL", quantity, tif="DAY")
        if recommendation_id is not None:
            order.orderRef = recommendation_id
        self._stamp_account(order)
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

    async def submit_stop_order(
        self,
        ticker: str,
        quantity: float,
        stop_price: float,
        recommendation_id: str | None = None,
        *,
        tif: str = "GTC",
        outside_rth: bool = False,
    ) -> str:
        """Submit a protective sell stop that rests at the broker (KAN-19).

        A separate method rather than a parameter on the two above: both
        hardcode their action, and the existing callers pin their signatures.

        ``tif`` defaults to GTC because that is the property the whole design
        rests on — the spike watched a GTC stop survive a Gateway process
        restart unchanged, which makes it protection IB enforces when nothing
        of ours is running.

        ``outside_rth`` is an explicit parameter, not an inherited default,
        because IB's default (false) leaves the stop *dormant* outside regular
        hours: an overnight gap is uncovered until the open. The caller decides
        what that exposure should be.
        """
        await self._ensure_connected()
        quantity = self._effective_quantity(ticker, quantity)
        from ib_insync import StopOrder

        from shared.universe import make_stock_contract

        contract = make_stock_contract(ticker)
        order = StopOrder("SELL", quantity, stop_price, tif=tif)
        order.outsideRth = outside_rth
        if recommendation_id is not None:
            order.orderRef = recommendation_id
        self._stamp_account(order)
        trade = self._ib.placeOrder(contract, order)
        order_id = str(trade.order.orderId)
        self._register_trade(order_id, trade, ticker=ticker, side="sell")

        self._logger.info(
            "Stop order submitted",
            order_id=order_id,
            ticker=ticker,
            quantity=quantity,
            stop_price=stop_price,
            tif=tif,
            outside_rth=outside_rth,
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
