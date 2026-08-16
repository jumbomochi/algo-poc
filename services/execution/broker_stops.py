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

What this module does **not** do, all of it KAN-20's "verification and
adjustment" and all of it a reason the flag stays off until KAN-20 lands:

* **The level is set once, not trailed.** The stop is priced from the high
  known at placement and never revised, so as the high rises the resting stop
  is looser than the IPS rule the risk engine evaluates. The two agree on the
  day the stop is placed and drift apart after.
* **Nothing reduces or cancels a stop when shares leave.** Coverage is only
  ever brought *up*. A position sold outside this service — manually, from
  TWS, by another client — leaves its stop resting, and the oversell guard is
  what stands between that and a short.
* **A resting stop blocks the software exit path.** To
  ``outstanding_sell_quantity`` a full-coverage stop is a working sell for the
  whole position, so the KAN-10 guard sizes a flattening exit to zero and
  refuses it. Safe (never two live sells against the same shares) but it means
  a stopped position cannot be exited by software, including by a kill whose
  stop-cancel IB has not yet confirmed.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import func, select

from shared.logging import get_logger
from shared.models import OrderStatus, Position
from shared.order_ledger import BROKER_STOP_ORDER_TYPE, OrderLedger

# Smallest price increment IB accepts for a US equity stop. A sub-penny
# auxPrice is rejected outright, which would leave the position unprotected.
_TICK = 0.01


def ips_stop_price(reference_price: float, trailing_pct: float) -> float:
    """The IPS stop level: ``trailing_pct`` below ``reference_price``.

    The same arithmetic :meth:`RiskEngine.check_stop_loss` applies, given the
    same reference. It is **not** a trailing stop: see the module docstring.
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
    # round(x, 2) rather than round(x / _TICK) * _TICK: the latter reintroduces
    # binary float error (33.33 -> 28.330000000000002) and IB rejects a price
    # off the minimum variation with error 110, so the stop is never placed.
    return round(stop, 2)


@dataclass(frozen=True)
class _PositionScope:
    """One position at the grain stop coverage is tracked at."""

    account_id: str
    portfolio: str
    con_id: int
    symbol: str
    exchange: str | None
    currency: str | None
    quantity: float
    highest_price_since_entry: float


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
        whole_shares: bool = True,
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
        self._whole_shares = whole_shares
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

        try:
            covered = self._ledger.open_stop_quantity(
                account_id, portfolio, con_id
            )
        finally:
            # Never hold a read transaction across the await below: this
            # session is shared with the IB callbacks.
            self._ledger.session.rollback()

        shortfall = self._placeable(float(quantity) - covered)
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
            # Unprotected is unprotected, whatever the reason — this pages for
            # the same reason a broker refusal does.
            await self._report_failure(
                symbol=symbol,
                con_id=con_id,
                quantity=shortfall,
                stop_price=None,
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
            # The stop level, parked in limit_price. It makes the intent
            # self-describing — enough on its own to re-drive a placement that
            # died before reaching IB — and it reserves nothing, because
            # create_intent only reserves notional for a BUY.
            limit_price=stop_price,
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
            # The order may have reached IB before the error did: placeOrder
            # is not the last statement in the submit path. Terminalising
            # without checking would mint a fresh id next time and place a
            # SECOND stop for the same shares — over-coverage, which sells the
            # account short on trigger — while orphaning the first, unledgered.
            adopted = await self._adopt_if_live(proposal.recommendation_id)
            if adopted is not None:
                self._logger.warning(
                    "Stop submission errored but the order is live at IB; "
                    "adopting it",
                    order_id=adopted,
                    symbol=symbol,
                    recommendation_id=proposal.recommendation_id,
                    reason=str(exc),
                )
                order_id = adopted
            else:
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

    def _placeable(self, shortfall: float) -> float:
        """The part of ``shortfall`` this account can actually be sold.

        The executor truncates to whole shares when the account has no
        fractional permission, so ledgering the untruncated number would leave
        a fraction of phantom coverage forever and trip reconciliation's
        remaining-quantity comparison. Truncate here instead, where the ledger
        row is written.
        """
        if not self._whole_shares:
            return shortfall
        return float(math.floor(shortfall))

    async def _adopt_if_live(self, recommendation_id: str) -> str | None:
        """The broker order for this ref, if one is live despite the error."""
        finder = getattr(self._order_manager, "find_stop_order", None)
        if finder is None:
            return None
        try:
            found = await finder(recommendation_id)
        except Exception:
            self._logger.exception(
                "Could not check whether a failed stop reached IB",
                recommendation_id=recommendation_id,
            )
            return None
        return str(found) if found is not None else None

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

        await self._resume_unsubmitted_stops()

        placed: list[str] = []
        for scope in self._open_position_scopes():
            order_id = await self.ensure_coverage(
                account_id=scope.account_id,
                portfolio=scope.portfolio,
                con_id=scope.con_id,
                symbol=scope.symbol,
                exchange=scope.exchange,
                currency=scope.currency,
                quantity=scope.quantity,
                # The IPS stop trails the high, not the last price.
                reference_price=scope.highest_price_since_entry,
            )
            if order_id is not None:
                placed.append(order_id)
        return placed

    async def _resume_unsubmitted_stops(self) -> list[str]:
        """Finish stop intents that were approved but never reached IB.

        A crash between the APPROVED commit and the placement (SIGKILL, OOM,
        the host going to sleep — this deployment has seen all three) leaves a
        row that counts as coverage forever while nothing rests at IB. The
        backfill would then read the position as protected and place nothing,
        and no later pass would ever notice.

        Re-driving the submission rather than terminalising the row: the
        submit path probes ``find_order_by_ref`` first, so an order that *did*
        reach IB is adopted instead of duplicated.
        """
        resumed: list[str] = []
        for intent in self._ledger.unsubmitted_stop_intents():
            recommendation_id = intent.recommendation_id
            symbol = intent.symbol
            quantity = float(intent.requested_quantity)
            stop_price = intent.limit_price
            self._ledger.session.rollback()
            if stop_price is None:
                # Pre-dates the stop level being recorded on the intent, so it
                # cannot be re-placed as approved. Terminalise it so the
                # backfill below covers the position afresh.
                self._ledger.transition(
                    recommendation_id,
                    OrderStatus.SUBMISSION_FAILED,
                    reason="stop level not recorded; re-covering from scratch",
                )
                self._ledger.session.commit()
                continue
            try:
                order_id = await self._order_manager.submit_stop(
                    ticker=symbol,
                    quantity=quantity,
                    stop_price=float(stop_price),
                    recommendation_id=recommendation_id,
                    tif=self._tif,
                    outside_rth=self._outside_rth,
                )
            except Exception as exc:
                self._ledger.session.rollback()
                self._ledger.transition(
                    recommendation_id,
                    OrderStatus.SUBMISSION_FAILED,
                    reason=str(exc),
                )
                self._ledger.session.commit()
                self._logger.error(
                    "Could not resume an unsubmitted stop; the position will "
                    "be re-covered from scratch",
                    recommendation_id=recommendation_id,
                    symbol=symbol,
                    reason=str(exc),
                )
                continue
            self._ledger.record_submission(recommendation_id, order_id)
            self._ledger.session.commit()
            self._logger.warning(
                "Resumed a stop left unsubmitted by a prior session",
                order_id=order_id,
                symbol=symbol,
                recommendation_id=recommendation_id,
            )
            resumed.append(order_id)
        return resumed

    def _open_position_scopes(self) -> list[_PositionScope]:
        """Open positions aggregated to the grain coverage is tracked at.

        ``positions`` has no unique key on ``{account, portfolio, con_id}`` and
        ``reconcile_paper`` explicitly handles more than one row per contract.
        Sizing per row would compute the second row's shortfall against the
        first row's coverage and leave the difference silently unprotected.
        """
        scopes: dict[tuple[str, str, int], _PositionScope] = {}
        for position in self._open_positions():
            if position.con_id is None:
                self._logger.warning(
                    "Open position has no contract id — cannot assert stop "
                    "coverage for it",
                    symbol=position.ticker,
                    portfolio=position.portfolio,
                )
                continue
            key = (
                str(position.account_id),
                position.portfolio,
                int(position.con_id),
            )
            existing = scopes.get(key)
            quantity = float(position.quantity)
            high = float(position.highest_price_since_entry)
            if existing is None:
                scopes[key] = _PositionScope(
                    account_id=key[0],
                    portfolio=key[1],
                    con_id=key[2],
                    symbol=position.ticker,
                    exchange=position.exchange,
                    currency=position.currency,
                    quantity=quantity,
                    highest_price_since_entry=high,
                )
                continue
            scopes[key] = replace(
                existing,
                quantity=existing.quantity + quantity,
                # The higher high gives the tighter stop, which is the
                # conservative reading of one position held in two rows.
                highest_price_since_entry=max(
                    existing.highest_price_since_entry, high
                ),
            )
        return list(scopes.values())

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
