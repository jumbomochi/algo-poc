#!/usr/bin/env python3
"""Re-project a fill the portfolio-accounting projector rejected (KAN-108).

WHAT A "BURNED" FILL IS
-----------------------
``FillProjector.apply`` commits the immutable ``execution_fills`` row BEFORE
it validates the fill against sleeve state. When validation fails -- on
2026-10-07, ARKW order 272 in thematic_momentum: 11 @ 165.00 against a sleeve
holding $24.83, "fill would make sleeve cash negative" -- the row survives
with ``projection_applied=false``, the error goes to ``stream:fills:dlq``, and
the book does not hold a position IB does. Reconciliation reads ``major``
(``unapplied_execution_fill`` plus ``missing_in_db``) and entries stay
disabled.

WHY NOTHING ELSE CAN REPAIR IT
------------------------------
* Replaying the DLQ message onto ``stream:fills`` is a silent no-op: the
  projector finds the row in ``_existing_fill`` and returns False.
* ``reconcile_paper.py --apply-plan`` refuses it as
  ``recorded_fill_not_projected``.
* ``restore_missed_entries.py`` restores executions ABSENT from
  ``execution_fills``; this one is present.

WHAT THIS DOES
--------------
It runs the projector's own code path against the row that is already there:
``FillProjector.project_recorded`` -> ``_project`` -> ``_validate``,
``PaperTradingState._apply_fill_accounting`` and ``_advance_intent``, the same
``_project`` that ``apply()`` runs for a fresh message. No accounting is
reimplemented here.

* **Dry-run by default.** Prints each fill, its intent, the sleeve's cash
  before and after (the exact delta), and every pre-check: the projector's
  ``_validate`` itself, then the knowable rejections inside
  ``_apply_fill_accounting`` that ``restore_missed_entries.preflight`` also
  mirrors -- sleeve cash, contract identity, account ownership (including an
  open position on the contract with a NULL ``account_id``), and for a sell
  the held quantity. Planning issues SELECTs only, takes no row locks, and on
  Postgres runs inside a ``READ ONLY`` transaction, so it cannot write even by
  mistake.
* **``--apply``** takes explicit ``--execution-id`` values and ``--account``
  only (``--all-unapplied`` is a dry-run survey: a fill nobody looked at, or
  a burned row on another account, must not ride along). It needs an
  interactive TTY and the exact phrase ``REPROJECT BURNED FILLS``, and proves
  the artifact directory writable before the first transaction. Each fill is
  ONE transaction: un-expire (below), project, set
  ``projection_applied=true``, commit. Any failure -- a projector refusal or
  an unexpected database error -- rolls all of that fill back (the row stays
  re-projectable), is recorded, and stops the run. The artifact is written in
  a ``finally``, so it always records what did commit. Afterwards the book is
  re-read and each sleeve's cash and position quantity compared with the
  dry-run prediction.
* **Stale marks.** A re-projected buy opens its position with
  ``current_price``, ``peak_price`` and ``highest_price_since_entry`` at the
  fill price; the next mark-to-market (the next paper run) corrects them.
* **Un-expiry.** An intent EXPIRED with ``ABSENT_AT_IB_REASON`` was a guess
  ("the order vanished from IB") that the recorded execution refutes; it is
  restored to SUBMITTED with ``OrderLedger.restore_absent_terminalization``,
  the same rule ``plan_sweep`` applies, and the fill then terminalizes it.
  Any other terminal intent (FILLED, CANCELLED, EXPIRED for a real reason,
  ...) is refused: overturning it is a decision, not a repair.
* **Audit artifact** under ``output/reconciliation`` (relocated out of a
  linked worktree by ``durable_artifact_dir``; an existing file is never
  overwritten): what was applied, the intent before and after, the cash
  before and after, any failure, and the ``equity_snapshots`` rows that
  valued the sleeve WITHOUT the fill. Those rows are not repainted -- the
  same limitation ``backfill_restored_equity.py`` documents -- so the
  artifact dates the discontinuity instead.

THE DLQ ENTRY (documented, deliberately not automated)
------------------------------------------------------
The original message stays in ``stream:fills:dlq`` after a successful
repair. It is inert: nothing consumes that stream, and replaying it would be
the no-op described above. It only inflates the DLQ depth the evidence
digest and the risk service's ``dlq_backlog`` check report. Draining it is
left to the operator, with the commands printed by this tool, because:

* it cannot be in the same transaction as the repair -- an XDEL that
  succeeds after a rollback, or fails after a commit, is a second partial
  state for this tool to own;
* it would give a database tool a Redis credential and a second side effect
  that the dry run cannot prove harmless;
* a wrong XDEL destroys the only copy of a message, while leaving one there
  costs nothing.

After ``--apply`` reports success (from the deploy clone; the password is
read from the login keychain, never typed or put in argv)::

    export REDISCLI_AUTH="$(security find-generic-password -s algo-poc -a REDIS_PASSWORD -w)"
    docker exec -e REDISCLI_AUTH algo-poc-redis-1 redis-cli --no-auth-warning \\
        XRANGE stream:fills:dlq - +
    # find the entry whose execution_id is the re-projected one, then:
    docker exec -e REDISCLI_AUTH algo-poc-redis-1 redis-cli --no-auth-warning \\
        XDEL stream:fills:dlq <entry-id>
    unset REDISCLI_AUTH

Usage (dry run first, always). ``python`` alone imports ``services``/``shared``
from another checkout (the editable install points elsewhere), so set
``PYTHONPATH`` to the tree you mean; the database comes from
``ALGO_DATABASE_URL`` (or ``--database-url``) like the other ops tools::

    PYTHONPATH=$PWD .venv/bin/python scripts/ops/reproject_fill.py \\
        --execution-id 0000e0d5.6ac63cba.01.01 --account DUN551088
    PYTHONPATH=$PWD .venv/bin/python scripts/ops/reproject_fill.py \\
        --execution-id 0000e0d5.6ac63cba.01.01 --account DUN551088 --apply
    # survey every unprojected row for the account (dry run only):
    ... --all-unapplied --account DUN551088

Run the dry run IMMEDIATELY before ``--apply``: the cash check is only as
fresh as the plan.

Then ``scripts/reconcile_paper.py --report`` and confirm severity ok.

The operator runs ``--apply``. Agents run the dry-run at most.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isclose
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.portfolio_accounting.projector import (  # noqa: E402
    FillProjectionError,
    FillProjector,
)
from shared.artifact_dir import durable_artifact_dir  # noqa: E402
from shared.config import load_config  # noqa: E402
from shared.models import (  # noqa: E402
    EquitySnapshot,
    ExecutionFill,
    OrderIntent,
    OrderStatus,
    PortfolioConfig,
    Position,
)
from shared.order_ledger import (  # noqa: E402
    ABSENT_AT_IB_REASON,
    InvalidOrderTransition,
    OrderLedger,
)
from shared.redis_client import DEAD_LETTER_SUFFIX  # noqa: E402

#: Typed by the operator, in full, to apply.
CONFIRMATION = "REPROJECT BURNED FILLS"

DEFAULT_ARTIFACT_DIR = _REPO_ROOT / "output" / "reconciliation"

#: Where the projector's runner parked the rejected message.
DLQ_STREAM = "stream:fills" + DEAD_LETTER_SUFFIX

#: The statuses a fill can advance. Mirrors ``execution_sweep``'s
#: ``_FILLABLE_STATUSES``: everything else is terminal for a reason this tool
#: does not overturn (except the absent-at-IB expiry, which it undoes first).
_FILLABLE = (OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value)

_EPS = 1e-9


class ReprojectRefusedError(RuntimeError):
    """The repair cannot be applied safely. Every one of these is deliberate."""


# --------------------------------------------------------------------------
# Planning (read-only)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class FillPlan:
    """One recorded-but-unprojected execution, and what projecting it does."""

    row_id: int
    execution_id: str
    account_id: str
    ib_order_id: str
    recommendation_id: str | None
    portfolio: str | None
    symbol: str
    con_id: int
    exchange: str
    side: str
    quantity: float
    price: float
    commission: float
    commission_currency: str | None
    commission_trading: float | None
    cumulative_quantity: float | None
    executed_at: datetime
    intent_status: str | None = None
    intent_reason: str | None = None
    intent_requested: float | None = None
    intent_filled: float | None = None
    unexpire: bool = False
    status_after: str | None = None
    filled_after: float | None = None
    cash_before: float | None = None
    cash_delta: float | None = None
    cash_after: float | None = None
    #: The sleeve's open quantity on this contract before and after, as
    #: predicted; ``verify`` compares the book against ``position_after``.
    position_before: float | None = None
    position_after: float | None = None
    checks: tuple[Check, ...] = ()

    @property
    def problems(self) -> tuple[str, ...]:
        return tuple(
            f"{check.name}: {check.detail}" for check in self.checks
            if not check.ok
        )


@dataclass(frozen=True)
class ReprojectionPlan:
    fills: tuple[FillPlan, ...] = ()
    #: Requested by ``--execution-id`` but already projected: success, not
    #: a refusal -- this is what a re-run after ``--apply`` sees.
    already_projected: tuple[str, ...] = ()
    #: Requested by ``--execution-id`` and absent (in this account).
    not_found: tuple[str, ...] = ()

    @property
    def problems(self) -> tuple[str, ...]:
        missing = tuple(
            f"{execution_id}: no execution_fills row (in the selected account)"
            for execution_id in self.not_found
        )
        return missing + tuple(
            f"{fill.execution_id}: {problem}"
            for fill in self.fills for problem in fill.problems
        )


def _order_done(cumulative: float, requested: float) -> bool:
    """IB's ``isDone()`` for the original message, which the row never kept.

    The rule ``execution_sweep._to_fill_message`` uses for every recovered
    fill: done when the only shortfall is whole-share rounding (placement
    truncates 11.1691 to 11). The projector's own ``rounding_complete`` guard
    (0 < shortfall < 1) is what stops a material partial being completed.
    """
    return float(requested) - float(cumulative) < 1.0


def _status_after(cumulative: float, requested: float, order_done: bool) -> str:
    """What ``FillProjector._advance_intent`` will set. Display only."""
    shortfall = float(requested) - cumulative
    if isclose(cumulative, float(requested)) or (
        order_done and 0 < shortfall < 1.0
    ):
        return OrderStatus.FILLED.value
    return OrderStatus.PARTIALLY_FILLED.value


def _select_rows(
    session: Session,
    *,
    execution_ids: Sequence[str],
    all_unapplied: bool,
    account: str | None,
) -> tuple[list[ExecutionFill], list[str], list[str]]:
    rows: dict[int, ExecutionFill] = {}
    already: list[str] = []
    missing: list[str] = []
    for execution_id in dict.fromkeys(execution_ids):
        stmt = select(ExecutionFill).where(
            ExecutionFill.execution_id == execution_id
        )
        if account is not None:
            stmt = stmt.where(ExecutionFill.account_id == account)
        matches = list(session.scalars(stmt))
        if not matches:
            missing.append(execution_id)
        for row in matches:
            if row.projection_applied:
                already.append(execution_id)
            else:
                rows[row.id] = row
    if all_unapplied:
        stmt = select(ExecutionFill).where(
            ExecutionFill.projection_applied.is_(False)
        )
        if account is not None:
            stmt = stmt.where(ExecutionFill.account_id == account)
        for row in session.scalars(stmt):
            rows[row.id] = row
    # Row id is insertion order: the order the projector first saw them.
    # Projecting a later partial before an earlier one would fail _validate's
    # monotonic check, so the order matters.
    return [rows[key] for key in sorted(rows)], already, missing


def _intent_view(intent: OrderIntent, **overrides: Any) -> SimpleNamespace:
    """A detached copy of the intent, as it will stand when projected.

    ``_validate`` only reads attributes, so the dry run can ask it about the
    un-expired intent (and about the second of two fills on one order)
    without touching the ORM row -- which would be a write.
    """
    values = {
        column.name: getattr(intent, column.name)
        for column in OrderIntent.__table__.columns
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _plan_fill(
    session: Session,
    projector: FillProjector,
    row: ExecutionFill,
    *,
    cash_overlay: dict[str, float],
    filled_overlay: dict[str, float],
    position_overlay: dict[tuple[str, int], float],
) -> FillPlan:
    side = row.side.lower()
    base = dict(
        row_id=row.id,
        execution_id=row.execution_id,
        account_id=row.account_id,
        ib_order_id=str(row.ib_order_id),
        recommendation_id=row.recommendation_id,
        portfolio=row.portfolio,
        symbol=row.symbol,
        con_id=int(row.con_id),
        exchange=row.exchange,
        side=row.side.upper(),
        quantity=float(row.quantity),
        price=float(row.price),
        commission=float(row.commission),
        commission_currency=row.commission_currency,
        commission_trading=(
            None if row.commission_trading is None
            else float(row.commission_trading)
        ),
        cumulative_quantity=(
            None if row.cumulative_quantity is None
            else float(row.cumulative_quantity)
        ),
        executed_at=row.executed_at,
    )
    checks: list[Check] = []

    paper = str(row.account_id).startswith("DU")
    checks.append(Check(
        "paper account", paper,
        "" if paper else (
            f"account {row.account_id!r} is not an IB paper account; a live "
            "ledger is never repaired by a script"
        ),
    ))

    intent = (
        session.scalar(select(OrderIntent).where(
            OrderIntent.recommendation_id == row.recommendation_id
        ))
        if row.recommendation_id else None
    )
    if intent is None:
        checks.append(Check(
            "intent", False,
            f"no order_intents row for recommendation "
            f"{row.recommendation_id!r}; the projector would refuse it as "
            "unattributed",
        ))
        return FillPlan(**base, checks=tuple(checks))

    unexpire = (
        intent.status == OrderStatus.EXPIRED.value
        and intent.reason == ABSENT_AT_IB_REASON
    )
    status_now = OrderStatus.SUBMITTED.value if unexpire else intent.status
    fillable = status_now in _FILLABLE
    checks.append(Check(
        "intent status", fillable,
        "" if fillable else (
            f"intent {intent.recommendation_id} is {intent.status} (reason "
            f"{intent.reason!r}). Only SUBMITTED, PARTIALLY_FILLED, or "
            f"EXPIRED with reason {ABSENT_AT_IB_REASON!r} -- the reversible "
            "guess plan_sweep also undoes -- can be re-projected; anything "
            "else is a decision this tool does not make"
        ),
    ))

    filled_before = filled_overlay.get(
        intent.recommendation_id, float(intent.filled_quantity)
    )
    cumulative_guess = (
        float(row.cumulative_quantity) if row.cumulative_quantity is not None
        else filled_before + float(row.quantity)
    )
    order_done = _order_done(cumulative_guess, intent.requested_quantity)
    view = _intent_view(
        intent,
        status=status_now,
        reason=None if unexpire else intent.reason,
        filled_quantity=filled_before,
    )
    cumulative: float | None
    try:
        cumulative = projector.check_recorded(row, view, order_done=order_done)
    except FillProjectionError as exc:
        cumulative = None
        checks.append(Check("projector validation", False, str(exc)))
    else:
        checks.append(Check("projector validation", True))

    # -- the knowable rejections inside _apply_fill_accounting -------------
    portfolio = intent.portfolio
    config = session.scalar(
        select(PortfolioConfig).where(PortfolioConfig.portfolio == portfolio)
    )
    commission = (
        float(row.commission_trading) if row.commission_trading is not None
        else float(row.commission)
    )
    candidates = list(session.scalars(
        select(Position).where(
            Position.con_id == intent.con_id, Position.status == "open"
        )
    ))
    unowned = [p for p in candidates if p.account_id is None]
    checks.append(Check(
        "position account ownership", not unowned,
        "" if not unowned else (
            f"con_id {intent.con_id} has {len(unowned)} open position(s) with "
            "no account_id: the projector would refuse \"position account "
            "ownership is unresolved\". Resolve the ownership first"
        ),
    ))
    owned = [
        p for p in candidates
        if p.account_id == intent.account_id and p.portfolio == portfolio
    ]
    checks.append(Check(
        "position uniqueness", len(owned) <= 1,
        "" if len(owned) <= 1 else (
            f"{len(owned)} open {portfolio} positions on con_id "
            f"{intent.con_id}: \"position account ownership is ambiguous\""
        ),
    ))
    existing = owned[0] if len(owned) == 1 else None
    if existing is None and len(owned) <= 1:
        # Nothing held to conflict with: a buy opens a new position.
        checks.append(Check("contract identity", True))
    if existing is not None:
        same_ticker = existing.ticker == intent.symbol
        checks.append(Check(
            "position ticker", same_ticker,
            "" if same_ticker else (
                f"the open position is {existing.ticker!r}, the intent "
                f"{intent.symbol!r}: \"position broker ticker identity "
                "conflicts\""
            ),
        ))
        held = (existing.con_id, existing.exchange, existing.currency)
        incoming = (intent.con_id, intent.exchange, intent.currency)
        checks.append(Check(
            "contract identity", held == incoming,
            "" if held == incoming else (
                f"the open position is {held}, the intent {incoming}: "
                "\"position broker contract identity conflicts\""
            ),
        ))

    position_key = (portfolio, int(intent.con_id))
    position_before = position_overlay.get(
        position_key, float(existing.quantity) if existing is not None else 0.0
    )
    if side == "buy":
        position_after = position_before + float(row.quantity)
    else:
        position_after = max(position_before - float(row.quantity), 0.0)
    position_overlay[position_key] = position_after

    cash_before = cash_delta = cash_after = None
    if config is None:
        checks.append(Check(
            "sleeve cash", False,
            f"sleeve {portfolio!r} has no portfolio_config row, so the "
            "projector has no cash to move",
        ))
    else:
        cash_before = cash_overlay.get(portfolio, float(config.cash))
        if side == "buy":
            cash_delta = -(float(row.price) * float(row.quantity) + commission)
        else:
            held_qty = position_before
            covered = held_qty > 0 and float(row.quantity) <= held_qty + _EPS
            checks.append(Check(
                "position quantity", covered,
                "" if covered else (
                    f"selling {row.quantity:g} against {held_qty:g} held: "
                    "\"sell fill exceeds open position quantity\""
                ),
            ))
            sold = min(float(row.quantity), held_qty)
            cash_delta = float(row.price) * sold - commission
        cash_after = cash_before + cash_delta
        funded = side != "buy" or cash_after >= -_EPS
        checks.append(Check(
            "sleeve cash", funded,
            "" if funded else (
                f"sleeve {portfolio!r} has cash {cash_before:,.6f}; this fill "
                f"costs {-cash_delta:,.6f}, leaving {cash_after:,.6f}: the "
                "projector would refuse \"fill would make sleeve cash "
                "negative\". Fund the sleeve first, then re-run"
            ),
        ))
        cash_overlay[portfolio] = cash_after

    status_after = filled_after = None
    if cumulative is not None:
        filled_overlay[intent.recommendation_id] = cumulative
        filled_after = max(filled_before, cumulative)
        status_after = _status_after(
            cumulative, intent.requested_quantity, order_done
        )

    return FillPlan(
        **base,
        intent_status=intent.status,
        intent_reason=intent.reason,
        intent_requested=float(intent.requested_quantity),
        intent_filled=float(intent.filled_quantity),
        unexpire=unexpire,
        status_after=status_after,
        filled_after=filled_after,
        cash_before=cash_before,
        cash_delta=cash_delta,
        cash_after=cash_after,
        position_before=position_before,
        position_after=position_after,
        checks=tuple(checks),
    )


def plan_reprojection(
    session: Session,
    *,
    execution_ids: Sequence[str] = (),
    all_unapplied: bool = False,
    account: str | None = None,
) -> ReprojectionPlan:
    """Work out what re-projecting each selected row would do. SELECTs only.

    Sleeve cash, an order's filled quantity and the sleeve's position are
    carried forward between fills in the plan, so a second burned fill on the same sleeve or order is
    judged against the book as the first one will leave it.
    """
    rows, already, missing = _select_rows(
        session,
        execution_ids=execution_ids,
        all_unapplied=all_unapplied,
        account=account,
    )
    projector = FillProjector(session)
    cash_overlay: dict[str, float] = {}
    filled_overlay: dict[str, float] = {}
    position_overlay: dict[tuple[str, int], float] = {}
    fills = tuple(
        _plan_fill(
            session, projector, row,
            cash_overlay=cash_overlay,
            filled_overlay=filled_overlay,
            position_overlay=position_overlay,
        )
        for row in rows
    )
    return ReprojectionPlan(
        fills=fills,
        already_projected=tuple(already),
        not_found=tuple(missing),
    )


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AppliedFill:
    execution_id: str
    account_id: str
    ib_order_id: str
    recommendation_id: str
    portfolio: str
    symbol: str
    con_id: int
    side: str
    quantity: float
    price: float
    commission: float
    commission_currency: str | None
    commission_trading: float | None
    executed_at: str
    intent_status_before: str
    intent_reason_before: str | None
    unexpired: bool
    intent_status_after: str
    intent_filled_quantity_after: float
    cash_before: float
    cash_after: float

    @property
    def cash_delta(self) -> float:
        return self.cash_after - self.cash_before


@dataclass(frozen=True)
class ReprojectionResult:
    applied: tuple[AppliedFill, ...] = ()
    #: ``(execution_id, reason)`` for the one that failed. At most one: the
    #: loop stops there. Its row is untouched and still re-projectable.
    failed: tuple[tuple[str, str], ...] = ()


def _sleeve_cash(session: Session, portfolio: str) -> float:
    return float(session.scalar(
        select(PortfolioConfig.cash).where(PortfolioConfig.portfolio == portfolio)
    ))


def _apply_one(session: Session, fill: FillPlan) -> AppliedFill | None:
    """Re-project one row in ONE transaction. ``None`` if already projected.

    Everything is re-read under row locks rather than trusted from the plan:
    the plan is what the operator approved, the locked rows are what is
    written against. Any exception leaves the ``with`` and rolls back the
    un-expiry, the accounting and the flag together.
    """
    with session.begin():
        row = session.scalar(
            select(ExecutionFill)
            .where(
                ExecutionFill.account_id == fill.account_id,
                ExecutionFill.execution_id == fill.execution_id,
            )
            .with_for_update()
        )
        if row is None:
            raise ReprojectRefusedError(
                f"{fill.execution_id}: the execution_fills row has gone"
            )
        if row.projection_applied:
            return None
        intent = session.scalar(
            select(OrderIntent)
            .where(OrderIntent.recommendation_id == row.recommendation_id)
            .with_for_update()
        ) if row.recommendation_id else None
        if intent is None:
            raise ReprojectRefusedError(
                f"{fill.execution_id}: no order_intents row for "
                f"{row.recommendation_id!r}"
            )
        status_before, reason_before = intent.status, intent.reason
        unexpired = False
        if (
            intent.status == OrderStatus.EXPIRED.value
            and intent.reason == ABSENT_AT_IB_REASON
        ):
            intent = OrderLedger(session).restore_absent_terminalization(
                intent.recommendation_id
            )
            unexpired = True
        if intent.status not in _FILLABLE:
            raise ReprojectRefusedError(
                f"{fill.execution_id}: intent {intent.recommendation_id} is "
                f"{intent.status} (reason {intent.reason!r}); refusing to "
                "overturn it"
            )

        cash_before = _sleeve_cash(session, intent.portfolio)
        cumulative = (
            float(row.cumulative_quantity)
            if row.cumulative_quantity is not None
            else float(intent.filled_quantity) + float(row.quantity)
        )
        FillProjector(session).project_recorded(
            row, intent,
            order_done=_order_done(cumulative, intent.requested_quantity),
        )
        return AppliedFill(
            execution_id=row.execution_id,
            account_id=row.account_id,
            ib_order_id=str(row.ib_order_id),
            recommendation_id=intent.recommendation_id,
            portfolio=intent.portfolio,
            symbol=row.symbol,
            con_id=int(row.con_id),
            side=row.side.upper(),
            quantity=float(row.quantity),
            price=float(row.price),
            commission=float(row.commission),
            commission_currency=row.commission_currency,
            commission_trading=row.commission_trading,
            executed_at=_iso(row.executed_at),
            intent_status_before=status_before,
            intent_reason_before=reason_before,
            unexpired=unexpired,
            intent_status_after=intent.status,
            intent_filled_quantity_after=float(intent.filled_quantity),
            cash_before=cash_before,
            cash_after=_sleeve_cash(session, intent.portfolio),
        )


def apply_reprojection(
    session: Session,
    plan: ReprojectionPlan,
    *,
    confirm: str,
    applied_sink: list[AppliedFill] | None = None,
) -> ReprojectionResult:
    """Re-project every planned fill, one transaction each, stopping at a
    failure. Refuses a plan that carries any problem: a dry run that listed a
    refusal is not something to apply around.

    ANY exception from a fill -- a projector refusal, but equally a lock
    timeout, a dropped connection or an IntegrityError -- is recorded as that
    fill's failure and stops the loop. It is never allowed to escape past
    fills that already COMMITTED: those are real changes to the book, and a
    raw traceback would leave the operator without the artifact and the
    verify that say what they were. ``applied_sink``, when given, receives
    each fill the moment it commits, so a caller's ``finally`` can record
    them even if something below ``Exception`` (an interrupt) unwinds this.
    """
    if confirm != CONFIRMATION:
        raise ReprojectRefusedError(
            f"exact confirmation required: expected {CONFIRMATION!r}"
        )
    if plan.problems:
        raise ReprojectRefusedError(
            "the plan carries refusals; resolve them and re-run the dry run:\n  "
            + "\n  ".join(plan.problems)
        )
    # The planning reads opened a transaction; each fill needs its own.
    if session.in_transaction():
        session.rollback()

    applied = applied_sink if applied_sink is not None else []
    for fill in plan.fills:
        try:
            record = _apply_one(session, fill)
        except (
            FillProjectionError, ReprojectRefusedError, InvalidOrderTransition
        ) as exc:
            return ReprojectionResult(
                applied=tuple(applied), failed=((fill.execution_id, str(exc)),)
            )
        except Exception as exc:  # noqa: BLE001 -- see the docstring
            _discard(session)
            return ReprojectionResult(
                applied=tuple(applied),
                failed=((
                    fill.execution_id,
                    f"unexpected {type(exc).__name__}: {exc}",
                ),),
            )
        if record is not None:
            applied.append(record)
    return ReprojectionResult(applied=tuple(applied))


def _discard(session: Session) -> None:
    """Best-effort rollback on a session whose connection may be gone."""
    try:
        session.rollback()
    except Exception:  # noqa: BLE001 -- nothing useful to do with it
        pass


@dataclass(frozen=True)
class Verification:
    lines: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


def verify(
    session: Session, result: ReprojectionResult, plan: ReprojectionPlan
) -> Verification:
    """Re-read the book and compare it with what the plan predicted.

    Success is not reported unless every applied row reads projected, and
    each sleeve's cash and each position's quantity match the dry run the
    operator approved. A mismatch is not undone -- the fills are committed --
    but it is said out loud, because it means the book moved between the
    plan and the write.
    """
    planned = {fill.execution_id: fill for fill in plan.fills}
    # The LAST applied fill per sleeve / per contract is the one whose
    # prediction the book should now equal.
    cash_expected: dict[str, float] = {}
    position_expected: dict[tuple[str, str, int], float] = {}
    for record in result.applied:
        fill = planned.get(record.execution_id)
        if fill is None:
            continue
        if fill.cash_after is not None:
            cash_expected[record.portfolio] = fill.cash_after
        if fill.position_after is not None:
            position_expected[
                (record.account_id, record.portfolio, record.con_id)
            ] = fill.position_after

    lines: list[str] = []
    problems: list[str] = []
    try:
        session.expire_all()
        for record in result.applied:
            row = session.scalar(select(ExecutionFill).where(
                ExecutionFill.account_id == record.account_id,
                ExecutionFill.execution_id == record.execution_id,
            ))
            projected = row is not None and bool(row.projection_applied)
            lines.append(
                f"  {record.execution_id}: projection_applied="
                f"{str(projected).lower()}"
            )
            if not projected:
                problems.append(f"{record.execution_id} does not read projected")
        for portfolio, expected in sorted(cash_expected.items()):
            actual = _sleeve_cash(session, portfolio)
            ok = isclose(actual, expected, rel_tol=0, abs_tol=1e-6)
            lines.append(
                f"  sleeve {portfolio} cash {actual:,.6f} "
                f"(planned {expected:,.6f}){'' if ok else '  MISMATCH'}"
            )
            if not ok:
                problems.append(
                    f"sleeve {portfolio} cash is {actual:,.6f}, the plan "
                    f"predicted {expected:,.6f}"
                )
        for (account_id, portfolio, con_id), expected in sorted(
            position_expected.items()
        ):
            actual = sum(
                float(quantity) for quantity in session.scalars(
                    select(Position.quantity).where(
                        Position.account_id == account_id,
                        Position.portfolio == portfolio,
                        Position.con_id == con_id,
                        Position.status == "open",
                    )
                )
            )
            ok = isclose(actual, expected, rel_tol=0, abs_tol=1e-9)
            lines.append(
                f"  position {portfolio} con_id {con_id} quantity {actual:g} "
                f"(planned {expected:g}){'' if ok else '  MISMATCH'}"
            )
            if not ok:
                problems.append(
                    f"{portfolio} con_id {con_id} holds {actual:g}, the plan "
                    f"predicted {expected:g}"
                )
    except Exception as exc:  # noqa: BLE001 -- verification must not mask
        problems.append(
            f"could not re-read the book to verify: {type(exc).__name__}: {exc}"
        )
    finally:
        _discard(session)
    return Verification(lines=tuple(lines), problems=tuple(problems))


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def dlq_instructions(execution_ids: Iterable[str]) -> str:
    ids = ", ".join(execution_ids) or "<execution_id>"
    return "\n".join([
        f"DLQ: the original message for {ids} is still parked in "
        f"{DLQ_STREAM}.",
        "  It is inert (nothing consumes it; a replay is a no-op) and only "
        "counts toward DLQ depth.",
        "  Drain it by hand AFTER --apply succeeds, from the deploy clone:",
        '    export REDISCLI_AUTH="$(security find-generic-password -s '
        'algo-poc -a REDIS_PASSWORD -w)"',
        "    docker exec -e REDISCLI_AUTH algo-poc-redis-1 redis-cli "
        f"--no-auth-warning XRANGE {DLQ_STREAM} - +",
        "    # pick the entry whose execution_id matches, then:",
        "    docker exec -e REDISCLI_AUTH algo-poc-redis-1 redis-cli "
        f"--no-auth-warning XDEL {DLQ_STREAM} <entry-id>",
        "    unset REDISCLI_AUTH",
    ])


def _money(value: float | None) -> str:
    return "-" if value is None else f"{value:,.6f}"


def render(plan: ReprojectionPlan) -> str:
    lines: list[str] = []
    for fill in plan.fills:
        lines.append(
            f"execution_fills id={fill.row_id}  {fill.execution_id}  "
            f"account {fill.account_id}"
        )
        commission_usd = (
            "" if fill.commission_trading is None
            else f" (USD {fill.commission_trading:,.6f})"
        )
        lines.append(
            f"  fill    {fill.side} {fill.quantity:g} {fill.symbol} "
            f"(con_id {fill.con_id}, {fill.exchange}) @ {fill.price:,.4f}  "
            f"commission {fill.commission:,.6f} "
            f"{fill.commission_currency or '?'}{commission_usd}  "
            f"order {fill.ib_order_id}  executed {_iso(fill.executed_at)}"
        )
        if fill.intent_status is None:
            lines.append(f"  intent  {fill.recommendation_id}: NOT FOUND")
        else:
            lines.append(
                f"  intent  {fill.recommendation_id}\n"
                f"          {fill.intent_status} (reason "
                f"{fill.intent_reason!r})  requested "
                f"{fill.intent_requested:g}  filled {fill.intent_filled:g}"
            )
            if fill.unexpire:
                lines.append(
                    "          -> will UN-EXPIRE to SUBMITTED: the absent-at-IB "
                    "expiry is refuted by this execution"
                )
            if fill.status_after is not None:
                lines.append(
                    f"          -> then {fill.status_after} with filled "
                    f"{fill.filled_after:g}"
                )
        lines.append(
            f"  sleeve  {fill.portfolio}: cash {_money(fill.cash_before)} -> "
            f"{_money(fill.cash_after)}  (delta {_money(fill.cash_delta)})"
        )
        if fill.position_after is not None:
            lines.append(
                f"          {fill.symbol} position {fill.position_before:g} -> "
                f"{fill.position_after:g}"
            )
        lines.append("  pre-checks:")
        for check in fill.checks:
            mark = "ok  " if check.ok else "FAIL"
            suffix = "" if check.ok else f" -- {check.detail}"
            lines.append(f"    [{mark}] {check.name}{suffix}")
        lines.append("")
    for execution_id in plan.already_projected:
        lines.append(f"{execution_id}: already projected -- nothing to do")
    for execution_id in plan.not_found:
        lines.append(
            f"{execution_id}: no execution_fills row (in the selected "
            "account) -- refused"
        )
    if not plan.fills and not plan.not_found:
        lines.append("No unprojected fills selected.")
    lines.append(
        f"{len(plan.fills)} to re-project, {len(plan.already_projected)} "
        f"already projected, {len(plan.not_found)} not found, "
        f"{len(plan.problems)} refusal(s)"
    )
    return "\n".join(lines)


def write_exclusive(directory: Path, stem: str, payload: Mapping) -> Path:
    """Write ``payload`` as JSON to a NEW file; never replace an existing one."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, default=str) + "\n"
    for n in range(1000):
        path = directory / (f"{stem}.json" if n == 0 else f"{stem}-{n}.json")
        try:
            with path.open("x") as handle:
                handle.write(body)
        except FileExistsError:
            continue
        return path
    raise ReprojectRefusedError(f"could not find a free artifact name for {stem}")


def ensure_writable(directory: Path) -> Path:
    """Prove the artifact can be written BEFORE anything commits.

    The artifact is the only durable record of what a partial run changed,
    so a directory that cannot take it is a refusal, not a warning after
    the fact.
    """
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=directory, prefix=".reproject-fill-probe-"
        ) as probe:
            probe.write(b"ok")
            probe.flush()
    except OSError as exc:
        raise ReprojectRefusedError(
            f"the audit artifact directory {directory} is not writable "
            f"({exc}). Nothing has been written to the database. Fix it or "
            "pass --artifact-dir."
        ) from exc
    return directory


def _unrepainted_snapshots(
    session: Session, result: ReprojectionResult, *, repaired_at: datetime
) -> list[dict]:
    """``equity_snapshots`` rows that valued a sleeve without its fill.

    Bounded on both sides: written at or after the fill executed, and before
    the repair -- anything written after ``repaired_at`` already sees it.
    Each valued the sleeve without the position it held: cash overstated and
    market value understated by the cost of a buy (or the reverse for a sell).
    """
    rows: list[dict] = []
    for portfolio in sorted({record.portfolio for record in result.applied}):
        since = min(
            datetime.fromisoformat(record.executed_at)
            for record in result.applied if record.portfolio == portfolio
        )
        for snapshot in session.scalars(
            select(EquitySnapshot)
            .where(EquitySnapshot.portfolio == portfolio)
            .order_by(EquitySnapshot.date)
        ):
            created = snapshot.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if not since <= created < repaired_at:
                continue
            rows.append({
                "portfolio": portfolio,
                "date": snapshot.date.isoformat(),
                "session_date": (
                    snapshot.session_date.isoformat()
                    if snapshot.session_date else None
                ),
                "created_at": _iso(created),
                "equity": snapshot.equity,
                "cash": snapshot.cash,
                "market_value": snapshot.market_value,
            })
    return rows


def mark_note(record: AppliedFill) -> str | None:
    """What a re-projected buy leaves stale until the next mark-to-market."""
    if record.side != "BUY":
        return None
    return (
        f"{record.symbol} current_price, peak_price and "
        "highest_price_since_entry start at the fill price "
        f"({record.price:,.4f}) and are corrected at the next mark-to-market "
        "(the next paper run)."
    )


def build_artifact(
    session: Session,
    result: ReprojectionResult,
    *,
    applied_at: datetime,
    verification: Verification | None = None,
) -> dict:
    """The audit record. Never raises on a database error: it is written
    from a ``finally``, possibly after the connection died, and must not
    mask what committed."""
    snapshot_error = None
    try:
        snapshots = _unrepainted_snapshots(
            session, result, repaired_at=applied_at
        )
    except Exception as exc:  # noqa: BLE001
        snapshots = None
        snapshot_error = f"{type(exc).__name__}: {exc}"
    finally:
        _discard(session)
    return {
        "repair": "KAN-108 reproject_fill",
        "applied_at": applied_at.isoformat(),
        "applied": [
            {
                **record.__dict__,
                "cash_delta": record.cash_delta,
                "mark_note": mark_note(record),
            }
            for record in result.applied
        ],
        "failed": [
            {"execution_id": execution_id, "reason": reason}
            for execution_id, reason in result.failed
        ],
        "verification": (
            None if verification is None else {
                "lines": list(verification.lines),
                "problems": list(verification.problems),
            }
        ),
        "dlq": {
            "stream": DLQ_STREAM,
            "execution_ids": [record.execution_id for record in result.applied],
            "drain": (
                "not drained by this tool; see the module docstring of "
                "scripts/ops/reproject_fill.py for the XRANGE/XDEL commands"
            ),
        },
        "equity_snapshots_not_repainted": snapshots,
        "equity_snapshots_error": snapshot_error,
        "equity_series_note": (
            "The equity_snapshots rows listed above were recorded after the "
            "fill executed and before this repair, so they value the sleeve "
            "without it: for a buy, cash overstated and market value "
            "understated by the cost basis (the reverse for a sell). They are "
            "NOT rewritten: correcting them needs per-position daily marks "
            "that were never stored (the same limitation "
            "scripts/ops/backfill_restored_equity.py documents). The series is "
            "correct from the next snapshot after applied_at; the step "
            "between is this repair, not a trade."
        ),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Re-project execution_fills rows the projector recorded but "
            "rejected (projection_applied=false). Dry-run by default."
        )
    )
    parser.add_argument(
        "--execution-id", action="append", default=[], dest="execution_ids",
        help="an execution to re-project (repeatable; the only selector "
             "--apply accepts)",
    )
    parser.add_argument(
        "--all-unapplied", action="store_true",
        help="dry-run survey of every execution_fills row with "
             "projection_applied=false (refused with --apply)",
    )
    parser.add_argument(
        "--account", default=None,
        help="restrict to this IB account (required with --apply)",
    )
    parser.add_argument(
        "--database-url", default=None,
        help="defaults to ALGO_DATABASE_URL / config/default.yaml",
    )
    parser.add_argument(
        "--artifact-dir", default=None,
        help="where the audit record goes; defaults to output/reconciliation "
             "(relocated out of a linked git worktree)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="write the repair (default: dry-run report only)",
    )
    args = parser.parse_args(argv)
    if not args.execution_ids and not args.all_unapplied:
        parser.error("select fills with --execution-id and/or --all-unapplied")

    try:
        if args.apply and args.all_unapplied:
            # A survey is not a work order. One burned row on another account,
            # or a fill the operator never looked at, must not ride along with
            # the one they meant -- and N fills confirmed against one dry run
            # are N chances for the cash figure to have gone stale.
            raise ReprojectRefusedError(
                "--apply takes explicit --execution-id values only. Use "
                "--all-unapplied for the dry-run survey, then apply each fill "
                "by id."
            )
        if args.apply and not args.account:
            raise ReprojectRefusedError(
                "--apply requires --account: name the IB account this repair "
                "is for."
            )
        return _run(args)
    except ReprojectRefusedError as exc:
        print(f"\nRefused: {exc}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace) -> int:
    url = args.database_url or load_config(
        str(_REPO_ROOT / "config" / "default.yaml")
    ).database.url
    engine = create_engine(url)
    with Session(engine) as session:
        if not args.apply and engine.dialect.name == "postgresql":
            # Must be the transaction's first statement. Any write after it,
            # by this tool or by a bug in it, is refused by the server.
            session.execute(text("SET TRANSACTION READ ONLY"))
        plan = plan_reprojection(
            session,
            execution_ids=args.execution_ids,
            all_unapplied=args.all_unapplied,
            account=args.account,
        )
        session.rollback()
        print(render(plan))

        ids = [fill.execution_id for fill in plan.fills]
        if plan.problems:
            print(
                "\nRefusing to apply -- the projector would reject these (or "
                "they are not this tool's to decide):"
            )
            for problem in plan.problems:
                print(f"  - {problem}")
            print(
                "\nNothing is burned by a refusal here: the rows stay "
                "unprojected and re-projectable once the cause is fixed."
            )
            return 1
        if not plan.fills:
            print("\nNothing to re-project.")
            return 0
        if not args.apply:
            print(
                "\nDry-run only. To write it, re-run IMMEDIATELY with --apply, "
                "the same --execution-id value(s) and --account.\n"
            )
            print(dlq_instructions(ids))
            return 0

        if not sys.stdin.isatty():
            raise ReprojectRefusedError("--apply requires an interactive TTY")
        directory = ensure_writable(durable_artifact_dir(
            args.artifact_dir or DEFAULT_ARTIFACT_DIR,
            explicit=args.artifact_dir is not None,
        ))
        answer = input(
            f"\nType {CONFIRMATION} to re-project {len(plan.fills)} fill(s): "
        )
        if answer.strip() != CONFIRMATION:
            raise ReprojectRefusedError(
                f"exact confirmation required: expected {CONFIRMATION!r}"
            )

        started = datetime.now(timezone.utc)
        committed: list[AppliedFill] = []
        result = ReprojectionResult()
        verification: Verification | None = None
        try:
            try:
                result = apply_reprojection(
                    session, plan, confirm=answer.strip(),
                    applied_sink=committed,
                )
            except BaseException as exc:
                # An interrupt between commits: record what DID commit.
                result = ReprojectionResult(
                    applied=tuple(committed),
                    failed=((
                        "<interrupted>", f"{type(exc).__name__}: {exc}",
                    ),),
                )
                raise
            verification = verify(session, result, plan)
        finally:
            if result.applied or result.failed:
                artifact_path = write_exclusive(
                    directory,
                    f"reproject-fill-{started:%Y%m%dT%H%M%SZ}",
                    build_artifact(
                        session, result,
                        applied_at=started, verification=verification,
                    ),
                )
                print(f"\nAudit artifact: {artifact_path}")

        for record in result.applied:
            print(
                f"\nRe-projected {record.execution_id} ({record.symbol}): "
                f"intent {record.intent_status_before} -> "
                f"{record.intent_status_after}, sleeve {record.portfolio} cash "
                f"{record.cash_before:,.6f} -> {record.cash_after:,.6f}"
            )
            note = mark_note(record)
            if note:
                print(f"  note: {note}")
        if verification is not None and verification.lines:
            print("\nPost-apply check (book vs plan):")
            for line in verification.lines:
                print(line)

        problems = verification.problems if verification is not None else ()
        if result.failed or problems:
            if result.applied:
                print(
                    f"\n{len(result.applied)} fill(s) above COMMITTED before "
                    "the failure and stay applied."
                )
            for execution_id, reason in result.failed:
                print(
                    f"\n🚨 {execution_id} FAILED: {reason}\n"
                    "   Its transaction rolled back completely: the row is "
                    "still unprojected and the intent is as it was. Fix the "
                    "cause and re-run the dry run."
                )
            for problem in problems:
                print(f"🚨 {problem}")
            return 1

        applied_ids = [record.execution_id for record in result.applied]
        if applied_ids:
            print("\n" + dlq_instructions(applied_ids))
        print(
            "\nNow run:  scripts/reconcile_paper.py --report\n"
            "and confirm severity: ok / entries_allowed: true."
        )
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
