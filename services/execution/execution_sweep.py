"""Re-read from IB the executions the live callback never delivered.

Every order the daily run places is submitted ~21 minutes after the US
close and rests until the next open. The process that placed it is long
gone by then, so ``execDetails`` fires into nothing and the fill is never
booked. Position-level reconciliation notices that *something* changed but
cannot recover *what* — so the position becomes a phantom, blocks every
buy, and the repair tool writes its value off (KAN-85).

IB's own execution record survives the session boundary that the order
state does not. That asymmetry is what this module exploits. It decides
only; fetching and publishing live at the edges (Task 4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from shared.order_ledger import InvalidOrderTransition, OrderLedger
from shared.schemas.messages import FillMessage

#: Written to ``ExecutionFill.recovery_source`` for every fill this module
#: recovers, so the 04:52 digest can count them. A sweep that silently
#: recovers fills every day is hiding a worsening upstream problem.
RECOVERY_SOURCE_SWEEP = "ib_execution_sweep"

#: The states the projector can advance to FILLED/PARTIALLY_FILLED.
#: Mirrors ``ALLOWED_TRANSITIONS`` in ``shared.order_ledger``.
_FILLABLE_STATUSES = ("SUBMITTED", "PARTIALLY_FILLED")


@dataclass(frozen=True)
class SweptExecution:
    """One broker execution, normalized off ``ib_insync``'s Fill."""

    execution_id: str
    account_id: str
    ib_order_id: str
    con_id: int
    ticker: str
    exchange: str
    currency: str
    side: str
    quantity: float
    cumulative_quantity: float
    price: float
    commission: float
    commission_currency: str | None
    executed_at: datetime


@dataclass(frozen=True)
class SweepOutcome:
    """What the sweep decided. Counts are reported even when zero."""

    recovered: tuple[FillMessage, ...] = ()
    already_recorded: int = 0
    untracked: tuple[str, ...] = ()
    corrected: tuple[str, ...] = ()


def plan_sweep(
    executions: Sequence[SweptExecution], ledger: OrderLedger
) -> SweepOutcome:
    """Decide what to do with each execution IB reports.

    Four outcomes, in the order they are tested:

    - already in ``execution_fills`` -> counted, nothing published (AC2);
    - no ``order_intents`` row for the broker order id -> named in
      ``untracked``, never projected (AC3);
    - the intent was terminalized EXPIRED *on absence* -> restored so the
      ordinary fill path can terminalize it properly (AC4), then published;
    - otherwise -> published (AC1).
    """
    recovered: list[FillMessage] = []
    untracked: list[str] = []
    corrected: list[str] = []
    already_recorded = 0

    for execution in executions:
        if ledger.execution_fill_exists(
            execution.account_id, execution.execution_id
        ):
            already_recorded += 1
            continue

        intent = ledger.get_by_ib_order_id(
            execution.ib_order_id, account_id=execution.account_id
        )
        if intent is None:
            untracked.append(execution.ib_order_id)
            continue

        try:
            restored = ledger.restore_absent_terminalization(
                intent.recommendation_id
            )
        except InvalidOrderTransition:
            # Either it was never terminalized (the normal case — nothing
            # to correct) or it expired for a real reason, which this must
            # not overturn. Both are decided by the guard below.
            pass
        else:
            corrected.append(restored.recommendation_id)
            intent = restored

        if intent.status not in _FILLABLE_STATUSES:
            # Terminal for a reason the broker record does not refute.
            # Report it as untracked rather than forcing a projection the
            # ledger would reject anyway.
            untracked.append(execution.ib_order_id)
            continue

        recovered.append(_to_fill_message(execution, intent))

    return SweepOutcome(
        recovered=tuple(recovered),
        already_recorded=already_recorded,
        untracked=tuple(untracked),
        corrected=tuple(corrected),
    )


def _to_fill_message(
    execution: SweptExecution, intent: object
) -> FillMessage:
    """Build the same message the live callback would have published.

    Every field the projector's ``_REQUIRED_IDENTITY_FIELDS`` demands is
    present: execution_id, account_id, portfolio, con_id, exchange,
    currency. A fill missing any of them is dead-lettered (the 2026-08-01
    DLQ backlog), so this is not optional enrichment.
    """
    return FillMessage(
        ticker=execution.ticker,
        timestamp=execution.executed_at,
        side=execution.side,
        quantity=execution.quantity,
        fill_price=execution.price,
        commission=execution.commission,
        commission_currency=execution.commission_currency,
        recommendation_id=intent.recommendation_id,
        order_id=execution.ib_order_id,
        execution_id=execution.execution_id,
        account_id=execution.account_id,
        cumulative_quantity=execution.cumulative_quantity,
        portfolio=intent.portfolio,
        con_id=execution.con_id,
        exchange=execution.exchange,
        currency=execution.currency,
        # NOTE: the field is `requested_quantity` on OrderIntent — there is
        # no `intent.quantity`. `quantity` belongs to ExecutionFill.
        order_done=(
            execution.cumulative_quantity >= float(intent.requested_quantity)
        ),
        recovery_source=RECOVERY_SOURCE_SWEEP,
    )


#: IB reports the side of an execution as BOT/SLD, not buy/sell.
_IB_SIDE = {"BOT": "buy", "SLD": "sell"}


def executions_from_ib_fills(fills: Sequence[object]) -> list[SweptExecution]:
    """Normalize ``ib_insync`` Fill objects, skipping anything unusable.

    Mirrors the payload the live callback builds (``ib_executor.py:536-559``)
    so a recovered fill is indistinguishable from the one that should have
    arrived. Duck-typed and import-free by design: the caller (Task 4, in
    ``scripts/run_paper.py``) is the only place that touches ``ib_insync``.
    """
    swept: list[SweptExecution] = []
    for fill in fills:
        execution = fill.execution
        side = _IB_SIDE.get(str(execution.side).upper())
        if side is None:
            continue
        report = getattr(fill, "commissionReport", None)
        swept.append(
            SweptExecution(
                execution_id=str(execution.execId),
                account_id=str(execution.acctNumber),
                ib_order_id=str(execution.orderId),
                con_id=int(fill.contract.conId),
                ticker=str(fill.contract.symbol),
                exchange=fill.contract.exchange or "SMART",
                currency=fill.contract.currency or "USD",
                side=side,
                quantity=float(execution.shares),
                cumulative_quantity=float(execution.cumQty),
                price=float(execution.price),
                commission=float(getattr(report, "commission", 0.0) or 0.0),
                commission_currency=(
                    str(getattr(report, "currency", "") or "") or None
                ),
                executed_at=execution.time,
            )
        )
    return swept
