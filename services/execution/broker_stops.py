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

:meth:`BrokerStopManager.verify_coverage` (KAN-20) is what makes a placed stop
*stay* protection. Run on the 30-minute scan, it holds each stop intent against
what IB is actually resting and corrects the three ways they legitimately
diverge — the order is gone, the position shrank, the trailing high rose. Any
other disagreement between the broker and the ledger row describing it is a
stop something changed outside this system; that is reported and left exactly
as it is, because correcting it silently would erase the only evidence of
whatever did it.

What this module still does **not** do:

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
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import func, select

from shared.logging import get_logger
from shared.models import OrderStatus, Position
from shared.order_ledger import BROKER_STOP_ORDER_TYPE, OrderLedger

# Smallest price increment IB accepts for a US equity stop. A sub-penny
# auxPrice is rejected outright, which would leave the position unprotected.
_TICK = 0.01
# Half a tick: two prices that round to the same cent are the same stop.
_PRICE_EPSILON = _TICK / 2
# Share counts are floats only because fractional accounts exist; this is float
# noise, not a real difference in coverage.
_QUANTITY_EPSILON = 1e-6


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
class StopDrift:
    """A resting stop that no longer matches the intent describing it.

    Not one of the three benign divergences the verifier corrects: those are
    the *expectation* moving away from a ledger the broker still agrees with.
    This is the broker and the ledger disagreeing, which nothing in this
    system does — so something outside it moved the order.
    """

    recommendation_id: str
    order_id: str
    symbol: str
    con_id: int
    expected_quantity: float
    broker_quantity: float
    expected_price: float | None
    broker_price: float | None
    reason: str


@dataclass
class StopVerification:
    """What one verification pass changed, for the log and for the tests."""

    placed: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    vanished: list[str] = field(default_factory=list)
    drifts: list[StopDrift] = field(default_factory=list)


@dataclass(frozen=True)
class _StopClaim:
    """One ledger row's claim to be protecting shares right now."""

    recommendation_id: str
    order_id: str | None
    quantity: float
    stop_price: float | None


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
        on_drift_detected: Callable[[StopDrift], Awaitable[None]] | None = None,
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
        self._on_drift_detected = on_drift_detected
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

    async def verify_coverage(self) -> StopVerification:
        """Check every open position's stop against what IB is resting (KAN-20).

        The scan that turns a placed stop into protection that stays true.
        Three divergences are corrected in silence, because each has a known
        and benign cause:

        * the order is no longer at IB — a cancel-all reached further than
          intended, or a Gateway restart dropped it. Recreated.
        * the position is smaller than its coverage — shares left on a partial
          fill. Resized, because the excess opens a short when it triggers.
        * the trailing high has risen above the resting level. Re-levelled up,
          never down.

        Anything else is :class:`StopDrift`: reported, and left alone.
        """
        report = StopVerification()
        if not self._enabled:
            return report

        try:
            live = await self._live_stops_by_ref()
        except Exception:
            # Blind, not clear. Placing on the assumption that nothing rests
            # would double-cover every position in the book.
            self._logger.exception(
                "Could not read resting stops from the broker; skipping "
                "verification this cycle"
            )
            return report

        for scope in self._open_position_scopes():
            try:
                await self._verify_scope(scope, live, report)
            except Exception:
                # One position's verification must not abandon the rest, and a
                # half-applied ledger transaction on this shared session would
                # poison every IB callback that follows.
                self._ledger.session.rollback()
                self._logger.exception(
                    "Could not verify stop coverage for a position",
                    symbol=scope.symbol,
                    con_id=scope.con_id,
                )
        return report

    async def _live_stops_by_ref(self) -> dict[str, Any]:
        """Every order live at IB, keyed by the ref our intents are named for."""
        orders = await self._order_manager.list_open_broker_orders()
        return {
            str(order.order_ref): order
            for order in orders
            if getattr(order, "order_ref", None)
        }

    async def _verify_scope(
        self,
        scope: _PositionScope,
        live: dict[str, Any],
        report: StopVerification,
    ) -> None:
        claims = self._stop_claims(scope)

        resting: list[tuple[_StopClaim, Any]] = []
        drifted = False
        for claim in claims:
            order = live.get(claim.recommendation_id)
            if order is None:
                self._release_vanished_stop(claim, scope, report)
                continue
            drift = self._drift(claim, order, scope)
            if drift is not None:
                report.drifts.append(drift)
                await self._report_drift(drift)
                drifted = True
            resting.append((claim, order))

        if drifted:
            # Reported, not corrected — and the whole position is left alone,
            # because re-placing around a stop somebody moved would destroy
            # the state an operator has to look at to find out why.
            return

        covered = sum(self._broker_quantity(order) for _, order in resting)
        desired_price = self._desired_stop_price(scope)

        over_covered = covered > scope.quantity + _QUANTITY_EPSILON
        under_levelled = desired_price is not None and any(
            self._broker_price(order) is None
            or self._broker_price(order) < desired_price - _PRICE_EPSILON
            for _, order in resting
        )
        if over_covered or under_levelled:
            # Cancel first, then re-place whole: IB has no modify path here,
            # and ``ensure_coverage`` sizes against the ledger, so the old row
            # has to be terminal before the replacement can be sized at all.
            for claim, _ in resting:
                await self._retire_stop(claim, scope, report)

        order_id = await self.ensure_coverage(
            account_id=scope.account_id,
            portfolio=scope.portfolio,
            con_id=scope.con_id,
            symbol=scope.symbol,
            exchange=scope.exchange,
            currency=scope.currency,
            quantity=scope.quantity,
            reference_price=scope.highest_price_since_entry,
        )
        if order_id is not None:
            report.placed.append(order_id)

    def _stop_claims(self, scope: _PositionScope) -> list[_StopClaim]:
        """The ledger rows claiming to protect this position, read and released."""
        try:
            return [
                _StopClaim(
                    recommendation_id=intent.recommendation_id,
                    order_id=(
                        str(intent.ib_order_id)
                        if intent.ib_order_id is not None
                        else None
                    ),
                    quantity=float(intent.requested_quantity)
                    - float(intent.filled_quantity),
                    stop_price=(
                        float(intent.limit_price)
                        if intent.limit_price is not None
                        else None
                    ),
                )
                for intent in self._ledger.open_stop_intents(
                    scope.account_id, scope.portfolio, scope.con_id
                )
            ]
        finally:
            # Never hold a read transaction across the awaits that follow:
            # this session is shared with the IB callbacks.
            self._ledger.session.rollback()

    def _release_vanished_stop(
        self,
        claim: _StopClaim,
        scope: _PositionScope,
        report: StopVerification,
    ) -> None:
        """Terminalise a stop that is no longer at IB, freeing its coverage.

        Left alone, the row keeps counting toward ``open_stop_quantity``
        forever: the position reads as fully protected while nothing rests
        against it, and no later pass ever places a replacement. This is the
        state AC1 exists to break, and terminalising is what breaks it.
        """
        if claim.order_id is None:
            # Never bound to a broker order — that is
            # ``_resume_unsubmitted_stops``'s row, not a vanished one.
            return
        self._ledger.transition(
            claim.recommendation_id,
            OrderStatus.CANCELLED,
            reason="stop is no longer resting at the broker",
        )
        self._ledger.session.commit()
        report.vanished.append(claim.recommendation_id)
        self._logger.warning(
            "Protective stop is gone from the broker; re-covering the position",
            recommendation_id=claim.recommendation_id,
            order_id=claim.order_id,
            symbol=scope.symbol,
            con_id=scope.con_id,
        )

    async def _retire_stop(
        self,
        claim: _StopClaim,
        scope: _PositionScope,
        report: StopVerification,
    ) -> None:
        """Cancel a stop at IB and terminalise its row, so it can be re-placed."""
        if claim.order_id is None:
            return
        cancelled = await self._order_manager.cancel_broker_order(claim.order_id)
        if not cancelled:
            # Still live at IB. Terminalising anyway would free coverage the
            # broker is still holding, and the replacement would double-cover
            # the position — a short on trigger. Leave it for the next cycle.
            self._logger.error(
                "Could not cancel a stop for replacement; leaving it resting",
                recommendation_id=claim.recommendation_id,
                order_id=claim.order_id,
                symbol=scope.symbol,
            )
            return
        self._ledger.transition(
            claim.recommendation_id,
            OrderStatus.CANCELLED,
            reason="replaced by a re-sized or re-levelled stop",
        )
        self._ledger.session.commit()
        report.cancelled.append(claim.order_id)

    def _desired_stop_price(self, scope: _PositionScope) -> float | None:
        """The level the IPS rule wants right now, or None if it cannot be sized.

        A reference price that cannot produce a stop is not silently ignored —
        ``ensure_coverage`` reaches the same arithmetic below and pages.
        """
        try:
            return ips_stop_price(
                scope.highest_price_since_entry, self._trailing_pct
            )
        except ValueError:
            return None

    def _drift(
        self, claim: _StopClaim, order: Any, scope: _PositionScope
    ) -> StopDrift | None:
        """The broker disagreeing with the ledger row that describes this stop."""
        broker_quantity = self._broker_quantity(order)
        broker_price = self._broker_price(order)

        reasons: list[str] = []
        if abs(broker_quantity - claim.quantity) > _QUANTITY_EPSILON:
            reasons.append(
                f"broker holds {broker_quantity} unfilled shares, "
                f"the ledger records {claim.quantity}"
            )
        if claim.stop_price is not None and (
            broker_price is None
            or abs(broker_price - claim.stop_price) > _PRICE_EPSILON
        ):
            reasons.append(
                f"broker stop level is {broker_price}, "
                f"the ledger records {claim.stop_price}"
            )
        if not reasons:
            return None
        return StopDrift(
            recommendation_id=claim.recommendation_id,
            order_id=str(getattr(order, "order_id", claim.order_id or "")),
            symbol=scope.symbol,
            con_id=scope.con_id,
            expected_quantity=claim.quantity,
            broker_quantity=broker_quantity,
            expected_price=claim.stop_price,
            broker_price=broker_price,
            reason="; ".join(reasons),
        )

    @staticmethod
    def _broker_quantity(order: Any) -> float:
        remaining = getattr(order, "remaining_quantity", None)
        if remaining is None:
            remaining = float(getattr(order, "quantity", 0.0) or 0.0) - float(
                getattr(order, "filled_quantity", 0.0) or 0.0
            )
        return max(0.0, float(remaining))

    @staticmethod
    def _broker_price(order: Any) -> float | None:
        price = getattr(order, "aux_price", None)
        return None if price is None else float(price)

    async def _report_drift(self, drift: StopDrift) -> None:
        """Page: a resting stop was changed by something that is not us."""
        self._logger.error(
            "Protective stop has drifted from its ledger intent",
            recommendation_id=drift.recommendation_id,
            order_id=drift.order_id,
            symbol=drift.symbol,
            con_id=drift.con_id,
            reason=drift.reason,
        )
        if self._on_drift_detected is None:
            return
        try:
            await self._on_drift_detected(drift)
        except Exception:
            self._logger.exception("Could not alert on a drifting stop")

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
