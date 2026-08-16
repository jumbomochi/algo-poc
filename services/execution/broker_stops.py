"""Protective stops resting at the broker (KAN-19).

A software stop needs the whole stack alive to fire: the risk container
running, Redis reachable, Postgres up, the host awake. The 2026-07-30 Gateway
outage cost two hours of unprotected trading for exactly that reason. A GTC
stop resting at IB is enforced by IB whether or not any of that is true — the
KAN-18 spike watched one survive a Gateway *process* restart with every field
intact, which is what makes this the primary protection rather than a nicety.

Two properties this module exists to hold:

* **Ledgered.** Every stop gets an ``OrderIntent``. Not for tidiness: an
  unledgered broker order makes ``reconcile_paper`` report ``major`` and
  disables entries for the session. The spike demonstrated it accidentally on
  the live pipeline.
* **Whole-position.** Stops are sized against what is *already* covered, so
  the total per ``{account, portfolio, con_id}`` converges on the held
  quantity and never exceeds it. A partially-protected position is the failure
  mode this design is meant to remove.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from shared.logging import get_logger
from shared.models import OrderStatus, Position
from shared.order_ledger import BROKER_STOP_ORDER_TYPE, OrderLedger

# Smallest price increment IB accepts for a US equity stop. A sub-penny
# auxPrice is rejected outright, which would leave the position unprotected.
_TICK = 0.01


def ips_stop_price(reference_price: float, trailing_pct: float) -> float:
    """The IPS stop level: ``trailing_pct`` below the high since entry.

    The same rule :meth:`RiskEngine.check_stop_loss` fires on, evaluated
    against the same ``highest_price_since_entry`` — so the resting stop and
    the software stop describe one policy rather than two that drift apart.
    """
    if not reference_price or reference_price <= 0:
        raise ValueError(
            f"cannot size a stop from reference price {reference_price!r}"
        )
    stop = reference_price * (1.0 - trailing_pct / 100.0)
    if stop <= 0:
        raise ValueError(
            f"trailing {trailing_pct}% of {reference_price} is not a price"
        )
    return round(stop / _TICK) * _TICK


@dataclass(frozen=True)
class _StopProposal:
    """The shape :meth:`OrderLedger.create_intent` reads."""

    recommendation_id: str
    account_id: str
    mode: str
    portfolio: str
    con_id: int
    symbol: str
    exchange: str
    currency: str
    quantity: float
    action: str = "SELL"
    limit_price: float | None = None
    order_type: str = BROKER_STOP_ORDER_TYPE


class BrokerStopManager:
    """Places and ledgers the GTC stops that protect open positions."""

    def __init__(
        self,
        *,
        order_manager: Any,
        order_ledger: OrderLedger,
        mode: str,
        account_id: str | None,
        trailing_pct: float,
        enabled: bool = False,
        tif: str = "GTC",
        outside_rth: bool = False,
        on_placement_failed: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._order_manager = order_manager
        self._ledger = order_ledger
        self._mode = mode
        self._account_id = account_id
        self._trailing_pct = trailing_pct
        self._enabled = enabled
        self._tif = tif
        self._outside_rth = outside_rth
        self._on_placement_failed = on_placement_failed
        self._logger = get_logger("broker_stops")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def ensure_coverage(
        self,
        *,
        account_id: str,
        portfolio: str,
        con_id: int,
        symbol: str,
        exchange: str | None,
        currency: str | None,
        quantity: float,
        reference_price: float,
    ) -> str | None:
        """Bring one position up to full stop coverage.

        Returns the broker order id of the stop placed, or None when nothing
        needed placing — or when placing it failed, which is recorded as a
        ``SUBMISSION_FAILED`` intent rather than reported as protection.
        """
        if not self._enabled:
            return None

        covered = self._ledger.open_stop_quantity(account_id, portfolio, con_id)
        shortfall = float(quantity) - covered
        if shortfall <= 0:
            self._logger.debug(
                "Position already covered by a resting stop",
                symbol=symbol,
                con_id=con_id,
                quantity=quantity,
                covered=covered,
            )
            return None

        try:
            stop_price = ips_stop_price(reference_price, self._trailing_pct)
        except ValueError as exc:
            self._logger.error(
                "Cannot size a protective stop",
                symbol=symbol,
                con_id=con_id,
                reference_price=reference_price,
                reason=str(exc),
            )
            return None

        proposal = _StopProposal(
            recommendation_id=self._next_stop_id(account_id, portfolio, con_id),
            account_id=account_id,
            mode=self._mode,
            portfolio=portfolio,
            con_id=con_id,
            symbol=symbol,
            exchange=exchange or "SMART",
            currency=currency or "USD",
            quantity=shortfall,
        )

        # Ledger first, submit second. The reverse order can leave a stop
        # resting at IB that no intent describes, and reconciliation reads an
        # unledgered broker order as a `major` divergence that disables entries
        # for the whole session.
        self._ledger.create_intent(proposal)
        self._ledger.transition(
            proposal.recommendation_id, OrderStatus.APPROVED
        )
        self._ledger.session.commit()

        try:
            order_id = await self._order_manager.submit_stop(
                ticker=symbol,
                quantity=shortfall,
                stop_price=stop_price,
                recommendation_id=proposal.recommendation_id,
                tif=self._tif,
                outside_rth=self._outside_rth,
            )
        except Exception as exc:
            self._ledger.session.rollback()
            self._ledger.transition(
                proposal.recommendation_id,
                OrderStatus.SUBMISSION_FAILED,
                reason=str(exc),
            )
            self._ledger.session.commit()
            self._logger.error(
                "Protective stop was not placed — position is unprotected",
                symbol=symbol,
                con_id=con_id,
                quantity=shortfall,
                stop_price=stop_price,
                reason=str(exc),
            )
            await self._report_failure(
                symbol=symbol,
                con_id=con_id,
                quantity=shortfall,
                stop_price=stop_price,
                reason=str(exc),
            )
            return None

        self._ledger.record_submission(proposal.recommendation_id, order_id)
        self._ledger.session.commit()

        self._logger.info(
            "Protective stop resting at broker",
            order_id=order_id,
            symbol=symbol,
            con_id=con_id,
            quantity=shortfall,
            stop_price=stop_price,
            tif=self._tif,
            recommendation_id=proposal.recommendation_id,
        )
        return order_id

    async def _report_failure(self, **context: Any) -> None:
        """Page the operator: an unprotected position must not be silent.

        A stop that was never placed looks exactly like one that was, from
        anywhere but this log line — and the position it should have been
        protecting is the one carrying the risk.
        """
        if self._on_placement_failed is None:
            return
        try:
            await self._on_placement_failed(**context)
        except Exception:
            self._logger.exception("Could not alert on an unplaced stop")

    async def backfill_open_positions(self) -> list[str]:
        """Cover every open position that has no resting stop (AC2).

        Runs at startup, because the reasons a position ends up uncovered —
        a crash between the fill and the placement, a session where the flag
        was off, a stop that filled or was cancelled — are all invisible until
        something looks.
        """
        if not self._enabled:
            return []

        placed: list[str] = []
        for position in self._open_positions():
            if position.con_id is None:
                self._logger.warning(
                    "Open position has no contract id — cannot assert stop "
                    "coverage for it",
                    symbol=position.ticker,
                    portfolio=position.portfolio,
                )
                continue
            order_id = await self.ensure_coverage(
                account_id=str(position.account_id),
                portfolio=position.portfolio,
                con_id=int(position.con_id),
                symbol=position.ticker,
                exchange=position.exchange,
                currency=position.currency,
                quantity=float(position.quantity),
                # The IPS stop trails the high, not the last price.
                reference_price=float(position.highest_price_since_entry),
            )
            if order_id is not None:
                placed.append(order_id)
        return placed

    def durable_quantity(
        self, account_id: str, portfolio: str, con_id: int
    ) -> float:
        """Open shares the durable book records for one position.

        Zero when the projector has not written the row yet, which is normal
        seconds after a fill — the caller overlays its unprojected fills.
        """
        stmt = select(func.coalesce(func.sum(Position.quantity), 0.0)).where(
            Position.account_id == account_id,
            Position.portfolio == portfolio,
            Position.con_id == con_id,
            Position.status == "open",
        )
        try:
            return float(self._ledger.session.scalar(stmt) or 0.0)
        finally:
            self._ledger.session.rollback()

    def _open_positions(self) -> list[Position]:
        stmt = (
            select(Position)
            .where(
                Position.status == "open",
                Position.quantity > 0,
                Position.account_id.is_not(None),
            )
            .order_by(Position.id)
        )
        if self._account_id is not None:
            # KAN-11: this service trades one account, and a stop on a foreign
            # account's shares protects nothing it is responsible for.
            stmt = stmt.where(Position.account_id == self._account_id)
        positions = list(self._ledger.session.scalars(stmt))
        self._ledger.session.rollback()
        return positions

    def _next_stop_id(
        self, account_id: str, portfolio: str, con_id: int
    ) -> str:
        """A fresh id per placement, in one family per position.

        A position legitimately gets more than one stop over its life — topped
        up, re-covered after a cancel, re-attempted after a refusal — and
        ``recommendation_id`` is unique, so a fixed id would collide with the
        first attempt forever.
        """
        prefix = f"stop-{account_id}-{portfolio}-{con_id}-"
        return f"{prefix}{self._ledger.count_intents_with_id_prefix(prefix)}"
