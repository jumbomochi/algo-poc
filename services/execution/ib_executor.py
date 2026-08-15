from __future__ import annotations

import asyncio
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from shared.logging import get_logger

logger = get_logger("ib_executor")

# IB API system codes for server-connectivity state (delivered via errorEvent).
IB_CONNECTIVITY_LOST = 1100  # connectivity between IB and the Gateway lost
IB_CONNECTIVITY_RESTORED = (1101, 1102)  # restored (data lost / data maintained)

# File the host Gateway watchdog reads to learn about a 1100 — the API port
# stays open during a connectivity loss, so the watchdog's port check is blind
# to it. Written under ALGO_GATEWAY_STATE_DIR (a host-bind-mounted dir).
CONNECTIVITY_MARKER_NAME = "gateway_connectivity_lost"

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

    async def list_open_orders(self) -> list[OpenBrokerOrder]:
        """Enumerate every order live at the broker, with its orderRef."""
        ...

    async def cancel_broker_order(self, order_id: str) -> bool:
        """Cancel a live broker order, tracked by this process or not."""
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
        # Retain references to fire-and-forget callback tasks so they are not
        # garbage-collected mid-flight and their exceptions are surfaced.
        self._pending_tasks: set[Any] = set()
        self._fill_handler: FillHandler | None = None
        self._order_status_handler: OrderStatusHandler | None = None
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

    def _on_ib_error(
        self, reqId: int, errorCode: int, errorString: str, contract: Any = None
    ) -> None:
        """ib_insync ``errorEvent`` handler: track server connectivity.

        Error 1100 means the Gateway lost its link to IB while the API socket
        (and port) stay up — invisible to the watchdog's port check. Drop a
        marker the host watchdog reads; clear it when connectivity is restored
        (1101/1102). Best-effort: marker I/O must never disturb order routing.
        """
        if errorCode == IB_CONNECTIVITY_LOST:
            self._mark_connectivity_lost()
        elif errorCode in IB_CONNECTIVITY_RESTORED:
            self._clear_connectivity_marker()

    def _mark_connectivity_lost(self) -> None:
        if self._conn_marker is None:
            return
        try:
            self._conn_marker.parent.mkdir(parents=True, exist_ok=True)
            self._conn_marker.write_text(str(int(time.time())))
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

            self._ib = IB()
            await self._ib.connectAsync(
                self._host, self._port, clientId=self._client_id
            )
            # Observe server-connectivity events (Error 1100/1101/1102). The IB
            # instance is recreated per connect, so re-attach every time.
            self._ib.errorEvent += self._on_ib_error
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

    def _reregister_open_trades(self) -> None:
        """Re-bind fill/status callbacks onto the fresh Trade objects after a
        reconnect, for every tracked order still open at IB.

        This recovers callbacks for orders that are STILL OPEN across the
        reconnect. An order that reached a terminal state *during* the outage no
        longer appears in ``openTrades()``, so its fill/status can't be replayed
        here — that divergence is caught by the daily broker reconciliation
        (``scripts/reconcile_paper.py``). Such orders are logged as a warning so
        the gap is visible rather than silent.
        """
        if self._ib is None or not self._trade_meta:
            return
        try:
            open_trades = list(self._ib.openTrades())
        except Exception:  # pragma: no cover - defensive
            self._logger.exception("Could not list open trades on reconnect")
            return
        open_ids = set()
        reattached = 0
        for trade in open_trades:
            order = getattr(trade, "order", None)
            order_id = str(getattr(order, "orderId", "") or "")
            meta = self._trade_meta.get(order_id)
            if meta is None:
                continue
            open_ids.add(order_id)
            ticker, side = meta
            self._register_trade(order_id, trade, ticker, side)
            reattached += 1
        if reattached:
            self._logger.info(
                "Re-registered IB callbacks after reconnect", count=reattached
            )
        # Tracked orders that vanished across the reconnect may have completed
        # during the outage; their fills cannot be replayed via callbacks.
        missing = [oid for oid in self._trade_meta if oid not in open_ids]
        if missing:
            self._logger.warning(
                "Tracked orders absent after reconnect — verify via broker "
                "reconciliation (fills may have completed during the outage)",
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
        # Remember (ticker, side) so callbacks can be re-registered onto a fresh
        # Trade object after a reconnect recreates the IB client.
        self._trade_meta[order_id] = (ticker, side)

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
            self._spawn(self._fill_handler(payload))

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

    async def list_open_orders(self) -> list[OpenBrokerOrder]:
        """Enumerate every order live at the broker, with its ``orderRef``.

        Account-wide and ledger-independent on purpose: the post-halt sweep
        exists to find an order whose broker id never reached the ledger, so
        no ledger-keyed lookup can see it. Nothing is registered as a tracked
        trade here — an order returned by this call may belong to another
        client id or to a manual TWS session, and binding our fill/status
        callbacks to someone else's order would corrupt attribution.
        """
        await self._ensure_connected()
        orders: list[OpenBrokerOrder] = []
        for trade in list(self._ib.openTrades()):
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
                    for open_trade in self._ib.openTrades()
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

        # Absent from both open trades and completed-order history. IB does not
        # retain order state across session boundaries, so a day order that
        # filled or expired before a restart is simply gone the next session —
        # the normal case, not a fault. Terminalize it (EXPIRED) via the status
        # handler so the ledger intent stops wedging restarts and reconciliation.
        # Position-level safety (a fill missed while disconnected) is caught
        # independently by the reconciler's broker-vs-DB position comparison.
        # Without a handler there is no safe terminalization path, so preserve
        # the fail-closed None (the caller raises).
        if self._order_status_handler is not None:
            await self._order_status_handler({
                "order_id": str(expected_order_id),
                "status": "Expired",
                "reason": "order absent from IB after session boundary",
                "order_absent_at_ib": True,
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
