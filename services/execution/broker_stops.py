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
# IB statuses that mean a stop's protection is genuinely gone. "Filled" is
# deliberately absent: those shares were sold by the stop itself, and the fill
# handler owns that transition — see _confirm_absent.
_RELEASABLE_ORDER_STATES = frozenset(
    {"cancelled", "apicancelled", "expired", "inactive"}
)
# Consecutive scans that may fail to confirm before the operator is paged. One
# blip is noise; a Gateway outage silently freezes every release, and this
# module exists because of a Gateway outage.
_CONFIRM_FAILURES_BEFORE_ALERT = 2


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
    """What one verification pass changed, for the log and for the tests.

    ``placed`` and ``cancelled_order_ids`` are broker order ids;
    ``released_intents`` are recommendation ids. Named apart because they are
    different namespaces and a log line mixing them silently is unreadable at
    3am.
    """

    placed: list[str] = field(default_factory=list)
    cancelled_order_ids: list[str] = field(default_factory=list)
    released_intents: list[str] = field(default_factory=list)
    drifts: list[StopDrift] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(
            self.placed
            or self.cancelled_order_ids
            or self.released_intents
            or self.drifts
        )


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
        # (recommendation_id, reason) already paged — see _report_drift.
        self._drifts_alerted: set[tuple[str, str]] = set()
        self._drifts_seen_this_pass: set[tuple[str, str]] = set()
        # Refs absent from IB's book on the previous scan, and refs placed so
        # recently that absence proves nothing — both gate the release path.
        self._missing_last_pass: set[str] = set()
        self._missing_this_pass: set[str] = set()
        self._skip_release: set[str] = set()
        # IB's completed-order statuses for the pass in flight, fetched at most
        # once, and how many passes running have failed to fetch them.
        self._completed_states: dict[str, str] | None = None
        self._confirm_failures = 0
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
        stop_price: float | None = None,
    ) -> str | None:
        """Bring one position up to full stop coverage.

        Returns the broker order id of the stop placed, or None when nothing
        needed placing — or when placing it failed, which is recorded as a
        ``SUBMISSION_FAILED`` intent rather than reported as protection.

        ``stop_price`` overrides the level the IPS rule computes from
        ``reference_price``. The verifier passes one so a replacement can be
        clamped to the tighter of the computed level and what is already
        resting; nothing else should need it.
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
            if stop_price is None:
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

        Every placement is capped by what IB says the account actually holds.
        The ``positions`` row lags each fill until the projector applies it,
        and sizing a top-up off a stale row places protective sells for shares
        that are already gone — which is a short, not protection.
        """
        report = StopVerification()
        if not self._enabled:
            return report

        # Shares IB reports per contract, spent down as each scope claims its
        # share. Two sleeves can hold the same contract, and sizing both
        # against the full broker position would double-cover it.
        budget: dict[int, float | None] = {}

        # A stop approved but never submitted counts as coverage while nothing
        # rests at IB. The backfill settles those at startup; a process that
        # stays up for weeks needs the same sweep on the scan. Capped by the
        # broker like every other placement — a resume is a placement, and
        # the intent's requested quantity is as stale as any other row.
        try:
            resumed = await self._resume_unsubmitted_stops(budget=budget)
        except Exception:
            self._ledger.session.rollback()
            self._logger.exception("Could not resume unsubmitted stop intents")
            resumed = []

        # Set before the read that can return early: anything placed moments
        # ago is not "vanished" — the broker's open-order book may not list it
        # yet, and the adoption path in submit_stop can return an id sourced
        # from completed orders, which openTrades never shows. Releasing those
        # would terminalise a live stop and place a second for the same shares.
        # Assigning after an early return would leave this pass's refs
        # unguarded and the previous pass's refs guarded — both wrong.
        self._skip_release = set(resumed)
        self._drifts_seen_this_pass = set()
        self._missing_this_pass = set()
        self._completed_states = None

        try:
            live, trusted = await self._live_stops_by_ref()
        except Exception:
            # Blind, not clear. Placing on the assumption that nothing rests
            # would double-cover every position in the book. No observation
            # was made, so _missing_last_pass is cleared rather than carried:
            # stale evidence must not count toward the two-scan proof.
            self._logger.exception(
                "Could not read resting stops from the broker; skipping "
                "verification this cycle"
            )
            self._missing_last_pass = set()
            return report

        for scope in self._open_position_scopes():
            try:
                await self._verify_scope(scope, live, report, budget, trusted)
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
        # Exactly this pass's drifts stay suppressed: one that is still there
        # keeps quiet, and one that went away can page again if it returns.
        self._drifts_alerted = set(self._drifts_seen_this_pass)
        self._missing_last_pass = self._missing_this_pass
        return report

    async def _live_stops_by_ref(self) -> tuple[dict[str, Any], bool]:
        """Orders live at IB keyed by ref, and whether the read looked synced.

        An empty book is the shape a *failed* sync takes, not only that of a
        genuinely empty account: ``connectAsync`` gives ``reqOpenOrders`` four
        seconds and only **logs** the timeout, so a Gateway slow to answer — a
        data-farm reset, the reconnect ``_ensure_connected`` performs mid-scan
        — leaves an empty order book behind a connection that reports success.
        Read as authority, that terminalises every stop intent in the book and
        places a duplicate for each, while reconciliation sees the still-live
        originals as ``major`` and disables entries for the session.

        An empty book therefore does not *prove* absence; it only fails to
        show presence. Callers treat it as untrusted and make a vanished stop
        prove itself twice (see :meth:`_confirm_absent`).
        """
        orders = await self._order_manager.list_open_broker_orders()
        return (
            {
                str(order.order_ref): order
                for order in orders
                if getattr(order, "order_ref", None)
            },
            bool(orders),
        )

    async def _confirm_absent(
        self, claim: _StopClaim, scope: _PositionScope
    ) -> bool:
        """Whether this stop's protection is really gone, per IB's own status.

        The open-order book alone cannot answer it. A missing ref is a stop
        somebody cancelled, a stop that filled, or a stop resting under
        another ``clientId`` and therefore invisible to us — and the three
        want opposite handling. IB's completed-order history carries the
        status that separates them:

        * ``Cancelled`` / ``ApiCancelled`` / ``Expired`` / ``Inactive`` — the
          protection is gone and this is what AC1 exists to recreate.
        * ``Filled`` — the shares were sold by this very stop. Held, not
          released: the fill belongs to the fill handler, and a row released
          here would be recorded as cancelled when it was not. The position
          reads over-claimed until the fill lands, which reconciliation
          surfaces; releasing a live stop instead puts a naked short in the
          account. Between an accounting lag and a short, take the lag.
        * absent from the history too — the history is same-day, so a stop
          cancelled yesterday is in neither book. Nothing here contradicts the
          open-order read, so the caller's evidence stands: a trusted book
          releases at once, an untrusted one after the second consecutive
          scan. Requiring a positive status as well would mean a stop
          cancelled yesterday could never be recreated at all.

        **Known residual:** ``openTrades`` is ``clientId``-scoped, so a stop
        placed under a different client id (a changed ``ib.client_id``, a
        second instance on a fallback) is invisible in both books and reads as
        cancelled. Closing that needs ``reqAllOpenOrders``, which would also
        widen what KAN-13's halt sweep cancels — out of scope here. The
        broker-position ceiling bounds the damage to the held quantity rather
        than removing it.
        """
        states = await self._completed_order_states(scope)
        if states is None:
            return False
        status = states.get(claim.recommendation_id)
        if status is None or status.lower() in _RELEASABLE_ORDER_STATES:
            return True
        self._logger.warning(
            "A stop is gone from the open-order book but IB reports it "
            "%s; leaving it alone rather than re-covering the position",
            status,
            recommendation_id=claim.recommendation_id,
            symbol=scope.symbol,
            con_id=scope.con_id,
            broker_status=status,
        )
        return False

    async def _completed_order_states(
        self, scope: _PositionScope
    ) -> dict[str, str] | None:
        """The day's terminal order statuses, fetched once per pass.

        ``None`` when IB could not be asked, which holds every release: an
        unconfirmable absence must not free coverage. That is fail-closed in
        the safe direction, but it is *also* the state where a genuinely
        deleted stop stops being recreated — so it pages rather than sitting
        in a log, on the same channel as an unplaced stop.
        """
        if self._completed_states is not None:
            return self._completed_states
        reader = getattr(self._order_manager, "completed_order_states", None)
        if reader is None:
            # No such capability (older double, backtest stub). Absence from a
            # trusted open-order book is then the only evidence available.
            self._completed_states = {}
            return self._completed_states
        try:
            self._completed_states = dict(await reader())
        except Exception:
            self._confirm_failures += 1
            self._logger.exception(
                "Could not read completed orders to confirm a missing stop; "
                "no coverage will be released this cycle",
                consecutive_failures=self._confirm_failures,
            )
            if self._confirm_failures >= _CONFIRM_FAILURES_BEFORE_ALERT:
                await self._report_failure(
                    symbol=scope.symbol,
                    con_id=scope.con_id,
                    quantity=scope.quantity,
                    stop_price=None,
                    reason=(
                        "cannot confirm whether protective stops are still "
                        f"resting at IB ({self._confirm_failures} consecutive "
                        "scans); a stop deleted now would not be recreated"
                    ),
                )
                self._confirm_failures = 0
            return None
        self._confirm_failures = 0
        return self._completed_states

    async def _verify_scope(
        self,
        scope: _PositionScope,
        live: dict[str, Any],
        report: StopVerification,
        budget: dict[int, float | None],
        trusted: bool,
    ) -> None:
        claims = self._stop_claims(scope)

        resting: list[tuple[_StopClaim, Any]] = []
        missing: list[_StopClaim] = []
        drifts: list[StopDrift] = []
        for claim in claims:
            order = live.get(claim.recommendation_id)
            if order is None:
                missing.append(claim)
                continue
            drift = self._drift(claim, order, scope)
            if drift is not None:
                drifts.append(drift)
            resting.append((claim, order))

        if drifts:
            # Reported, not corrected — and the whole position is left exactly
            # as it is, because re-placing around a stop somebody moved would
            # destroy the state an operator has to look at to find out why.
            # Including the missing rows: releasing their coverage here would
            # drop protection quietly while the alert talks about a different
            # order, and no later pass would place the replacement either.
            for drift in drifts:
                # The number of uncovered siblings is part of the identity of
                # the alert: a frozen scope that then loses more coverage is a
                # worse situation than the one already paged for, and must not
                # be silenced by the dedup.
                await self._report_drift(drift, uncovered=len(missing))
                report.drifts.append(drift)
            # Claim what is still resting so another sleeve on this contract
            # cannot size itself against shares this one is already covering.
            await self._reserve_frozen_coverage(scope, resting, budget)
            return

        for claim in missing:
            if claim.recommendation_id in self._skip_release:
                # Placed this very pass. Absence proves nothing yet, and the
                # observation is not evidence either — banking it would let the
                # next pass release on a single untrusted read.
                continue
            if not trusted:
                # The book showed nothing at all, so this is the one
                # observation the two-scan rule counts. Its coverage still
                # stands meanwhile, so the shortfall below is zero and nothing
                # is placed on top of a stop that may well be resting.
                self._missing_this_pass.add(claim.recommendation_id)
                if claim.recommendation_id not in self._missing_last_pass:
                    self._logger.warning(
                        "A stop is missing from an order book that showed "
                        "nothing at all; waiting for a second scan before "
                        "releasing it",
                        recommendation_id=claim.recommendation_id,
                        symbol=scope.symbol,
                        con_id=scope.con_id,
                    )
                    continue
            if not await self._confirm_absent(claim, scope):
                continue
            self._release_vanished_stop(claim, scope, report)

        allowed = await self._protectable_quantity(scope, budget)
        if allowed is None:
            # No broker truth this cycle. Sizing off the durable row alone is
            # how a top-up ends up protecting shares that are already sold.
            return
        if allowed < scope.quantity - _QUANTITY_EPSILON:
            # The book and the broker disagree about the size of this position.
            # Logged, not paged: reconcile_paper already reports the divergence
            # as `major`, and the benign version of this (a fill the projector
            # has not applied) happens on every scan after a partial exit.
            self._logger.warning(
                "Broker holds fewer shares than the book; capping stop "
                "coverage at the broker's number",
                symbol=scope.symbol,
                con_id=scope.con_id,
                book_quantity=scope.quantity,
                protectable=allowed,
            )

        covered = sum(self._broker_quantity(order) for _, order in resting)
        levels = [self._broker_price(order) for _, order in resting]
        ips_level = self._ips_level(scope)

        over_covered = covered > allowed + _QUANTITY_EPSILON
        under_levelled = ips_level is not None and any(
            level is None or level < ips_level - _PRICE_EPSILON
            for level in levels
        )
        replacement_level = ips_level
        if over_covered or under_levelled:
            # A resize must not double as a loosening. The replacement sits at
            # the tighter of what the IPS rule wants and what is already
            # resting, so re-sizing after a fill can never widen live
            # protection (AC5 holds on this branch too).
            known = [level for level in levels if level is not None]
            if replacement_level is not None and known:
                replacement_level = max(replacement_level, *known)
            # Cancel first, then re-place whole: IB has no modify path here,
            # and ``ensure_coverage`` sizes against the ledger, so the old row
            # has to be terminal before the replacement can be sized at all.
            # One confirm cycle for all of them, not one each — the wait is up
            # to 3.5s of backoff and it runs on the loop that also drains the
            # kill stream.
            await self._retire_stops(
                [claim for claim, _ in resting], scope, report
            )

        order_id = await self.ensure_coverage(
            account_id=scope.account_id,
            portfolio=scope.portfolio,
            con_id=scope.con_id,
            symbol=scope.symbol,
            exchange=scope.exchange,
            currency=scope.currency,
            quantity=allowed,
            reference_price=scope.highest_price_since_entry,
            stop_price=replacement_level,
        )
        if order_id is not None:
            report.placed.append(order_id)
        self._spend_con_id_budget(scope.con_id, budget, allowed)

    async def _protectable_quantity(
        self, scope: _PositionScope, budget: dict[int, float | None]
    ) -> float | None:
        """Shares of this position a stop may cover, per the broker's own book.

        ``None`` when IB could not be asked — the caller then places nothing,
        because the alternative is sizing off a ``positions`` row that lags
        every fill the projector has not applied.
        """
        if scope.con_id not in budget:
            budget[scope.con_id] = await self._broker_held(scope.con_id)
        remaining = budget[scope.con_id]
        if remaining is None:
            return None
        return max(0.0, min(scope.quantity, remaining))

    async def _broker_held(self, con_id: int) -> float | None:
        """Net long quantity IB reports for this contract, or None if unasked."""
        try:
            held = await self._order_manager.broker_position(con_id)
        except Exception:
            self._logger.exception(
                "Could not read the broker position backing a stop; leaving "
                "coverage unchanged this cycle",
                con_id=con_id,
            )
            return None
        try:
            return max(0.0, float(held))
        except (TypeError, ValueError):
            self._logger.error(
                "Broker reported an unusable position for a stop",
                con_id=con_id,
                held=held,
            )
            return None

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
        report.released_intents.append(claim.recommendation_id)
        self._logger.warning(
            "Protective stop is gone from the broker; re-covering the position",
            recommendation_id=claim.recommendation_id,
            order_id=claim.order_id,
            symbol=scope.symbol,
            con_id=scope.con_id,
        )

    async def _retire_stops(
        self,
        claims: list[_StopClaim],
        scope: _PositionScope,
        report: StopVerification,
    ) -> None:
        """Cancel stops at IB and terminalise their rows, so they can be re-placed.

        Confirmed against IB's own book, not merely requested.
        ``cancelOrder`` is a request: a stop that refuses to cancel, or that
        triggers in the same instant, is still live. Terminalising on the
        request alone frees coverage the broker is still holding, and the
        replacement placed straight after double-covers the position — which
        sells it short on trigger. :meth:`OrderManager.cancel_working_orders`
        is the primitive that waits for the book to agree (KAN-10).
        """
        cancellable = [claim for claim in claims if claim.order_id is not None]
        if not cancellable:
            return
        still_live = set(
            await self._order_manager.cancel_working_orders(
                [
                    (claim.order_id, claim.recommendation_id)
                    for claim in cancellable
                ]
            )
        )
        for claim in cancellable:
            if claim.recommendation_id in still_live:
                self._logger.error(
                    "Could not confirm a stop cancel; leaving it resting and "
                    "retrying next cycle",
                    recommendation_id=claim.recommendation_id,
                    order_id=claim.order_id,
                    symbol=scope.symbol,
                )
                continue
            self._ledger.transition(
                claim.recommendation_id,
                OrderStatus.CANCELLED,
                reason="replaced by a re-sized or re-levelled stop",
            )
            self._ledger.session.commit()
            report.cancelled_order_ids.append(claim.order_id)

    async def _reserve_frozen_coverage(
        self,
        scope: _PositionScope,
        resting: list[tuple[_StopClaim, Any]],
        budget: dict[int, float | None],
    ) -> None:
        """Spend a drift-frozen scope's coverage out of the contract's budget.

        A frozen scope places nothing, but its stops are still resting against
        the contract. Leaving its share unspent lets the next sleeve on the
        same ``con_id`` size itself against the whole broker position and push
        total coverage past what is held.
        """
        if await self._protectable_quantity(scope, budget) is None:
            return
        self._spend_con_id_budget(
            scope.con_id,
            budget,
            sum(self._broker_quantity(order) for _, order in resting),
        )

    def _ips_level(self, scope: _PositionScope) -> float | None:
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

    async def _report_drift(self, drift: StopDrift, *, uncovered: int = 0) -> None:
        """Page: a resting stop was changed by something that is not us.

        Once per distinct drift, not once per scan. The verifier deliberately
        does not correct a drift, so an unresolved one is still there on every
        later pass — paging every 30 minutes forever is how an alert channel
        gets muted, and this is a channel that also carries "your position is
        unprotected". The same dedup the post-halt sweep uses.
        """
        self._logger.error(
            "Protective stop has drifted from its ledger intent",
            recommendation_id=drift.recommendation_id,
            order_id=drift.order_id,
            symbol=drift.symbol,
            con_id=drift.con_id,
            reason=drift.reason,
        )
        # ``uncovered`` is part of the identity: a drift freezes every
        # adjustment on its position, so a frozen scope that then loses a
        # second stop is a worse state than the one already paged for and must
        # not inherit its silence.
        key = (drift.recommendation_id, f"{drift.reason}|uncovered={uncovered}")
        self._drifts_seen_this_pass.add(key)
        if key in self._drifts_alerted or self._on_drift_detected is None:
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

        # Same broker ceiling the scan uses. Without it a stale APPROVED row
        # for a contract the account no longer holds resumes into a live
        # protective sell against a flat position — and nothing ever revisits
        # it, because the loop below only walks contracts that still have an
        # open Position row, which that one by definition does not.
        budget: dict[int, float | None] = {}
        await self._resume_unsubmitted_stops(budget=budget)

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

    async def _resume_unsubmitted_stops(
        self, *, budget: dict[int, float | None] | None = None
    ) -> list[str]:
        """Finish stop intents that were approved but never reached IB.

        A crash between the APPROVED commit and the placement (SIGKILL, OOM,
        the host going to sleep — this deployment has seen all three) leaves a
        row that counts as coverage forever while nothing rests at IB. The
        backfill would then read the position as protected and place nothing,
        and no later pass would ever notice.

        Re-driving the submission rather than terminalising the row: the
        submit path probes ``find_order_by_ref`` first, so an order that *did*
        reach IB is adopted instead of duplicated.

        ``budget`` caps each resume by what the broker says is held, the same
        ceiling every other placement obeys. A resume is a placement, and the
        row it re-drives can be days old: the position it was approved for may
        have been sold since, in which case the "resumed" stop is a naked
        short waiting for a trigger. Without a budget (the startup backfill,
        which then re-reads coverage per position) the row is re-driven as
        approved.
        """
        resumed: list[str] = []
        for intent in self._ledger.unsubmitted_stop_intents():
            recommendation_id = intent.recommendation_id
            symbol = intent.symbol
            quantity = float(intent.requested_quantity)
            stop_price = intent.limit_price
            con_id = intent.con_id
            self._ledger.session.rollback()
            if budget is not None and con_id is not None:
                held = await self._resumable_quantity(int(con_id), budget)
                if held is None:
                    continue
                if held < quantity - _QUANTITY_EPSILON:
                    # Terminalise rather than resume a smaller amount.
                    # ``requested_quantity`` is one of the ledger's immutable
                    # economic fields, so a clamped placement could never be
                    # recorded: the row would claim 100 while 60 rested, which
                    # the verifier reads as a stop something outside this
                    # system moved — freezing every adjustment on the position
                    # and paging, on every scan, for a mismatch this code
                    # created. ``ensure_coverage`` mints a correctly-sized
                    # intent for whatever is really held.
                    self._ledger.transition(
                        recommendation_id,
                        OrderStatus.SUBMISSION_FAILED,
                        reason=(
                            f"broker holds {held} of this contract, the row "
                            f"describes {quantity}; re-covering from scratch"
                        ),
                    )
                    self._ledger.session.commit()
                    self._logger.warning(
                        "Abandoned an unsubmitted stop that no longer matches "
                        "the position it was approved for",
                        recommendation_id=recommendation_id,
                        symbol=symbol,
                        con_id=con_id,
                        approved_quantity=quantity,
                        broker_held=held,
                    )
                    continue
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
                quantity=quantity,
            )
            if budget is not None and intent.con_id is not None:
                self._spend_con_id_budget(int(intent.con_id), budget, quantity)
            # Keyed by ref, which is how the verifier matches a resting stop.
            resumed.append(recommendation_id)
        return resumed

    async def _resumable_quantity(
        self, con_id: int, budget: dict[int, float | None]
    ) -> float | None:
        """Shares of ``con_id`` still unclaimed, per the broker. None if unasked."""
        if con_id not in budget:
            budget[con_id] = await self._broker_held(con_id)
        return budget[con_id]

    def _spend_con_id_budget(
        self, con_id: int, budget: dict[int, float | None], claimed: float
    ) -> None:
        remaining = budget.get(con_id)
        if remaining is not None:
            budget[con_id] = max(0.0, remaining - claimed)

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
