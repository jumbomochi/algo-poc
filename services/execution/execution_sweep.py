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

from shared.broker_state import commission_in_usd
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
    #: IB's ``ExchangeRate`` account value (base-currency units per USD),
    #: needed only when the commission is not already USD. It can only be
    #: read where IB is, so the edge reads it and carries it here; this
    #: module never reaches for it.
    commission_fx_base_per_trading: float | None = None


@dataclass(frozen=True)
class SweepOutcome:
    """What the sweep decided. Counts are reported even when zero."""

    recovered: tuple[FillMessage, ...] = ()
    already_recorded: int = 0
    untracked: tuple[str, ...] = ()
    corrected: tuple[str, ...] = ()
    #: Execution ids this run declined to publish because the message would
    #: have been rejected by ``FillProjector`` — today, only a commission
    #: that cannot be translated to USD. Deliberately NOT published: the
    #: projector writes its immutable ``execution_fills`` audit row *before*
    #: it validates, so a rejected message makes ``execution_fill_exists``
    #: True forever and the execution is never retried. A deferral leaves
    #: the ledger untouched, so the next sweep can decide it again.
    deferred: tuple[str, ...] = ()


def plan_sweep(
    executions: Sequence[SweptExecution], ledger: OrderLedger
) -> SweepOutcome:
    """Decide what to do with each execution IB reports.

    Five outcomes, in the order they are tested:

    - already in ``execution_fills`` -> counted, nothing published (AC2);
    - no ``order_intents`` row for the broker order id -> named in
      ``untracked``, never projected (AC3);
    - the commission cannot be expressed in USD -> named in ``deferred``
      and left entirely alone, because the message would be rejected and
      the rejection is permanent (see ``SweepOutcome.deferred``);
    - the intent was terminalized EXPIRED *on absence* -> restored so the
      ordinary fill path can terminalize it properly (AC4), then published;
    - otherwise -> published (AC1).

    The commission check sits *before* the AC4 correction on purpose: an
    execution this run is not going to publish must not un-expire an intent
    it leaves unfilled.
    """
    recovered: list[FillMessage] = []
    untracked: list[str] = []
    corrected: list[str] = []
    deferred: list[str] = []
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

        commission_trading = commission_in_usd(
            execution.commission,
            execution.commission_currency or "",
            fx_base_per_trading=execution.commission_fx_base_per_trading,
        )
        if commission_trading is None or commission_trading < 0:
            deferred.append(execution.execution_id)
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

        recovered.append(
            _to_fill_message(execution, intent, commission_trading)
        )

    return SweepOutcome(
        recovered=tuple(recovered),
        already_recorded=already_recorded,
        untracked=tuple(untracked),
        corrected=tuple(corrected),
        deferred=tuple(deferred),
    )


def _to_fill_message(
    execution: SweptExecution, intent: object, commission_trading: float
) -> FillMessage:
    """Build the same message the live callback would have published.

    Every field the projector's ``_REQUIRED_IDENTITY_FIELDS`` demands is
    present: execution_id, account_id, portfolio, con_id, exchange,
    currency. A fill missing any of them is dead-lettered (the 2026-08-01
    DLQ backlog), so this is not optional enrichment.

    ``commission_trading`` is passed in already translated rather than
    computed here, because the caller has to decide whether it *can* be
    translated before it decides to publish at all.
    """
    # Mirrors the projector's own terminalization rule: FILLED when the fill
    # reaches the requested quantity, or when the only shortfall is
    # whole-share rounding. ``fractional_orders: false`` truncates 10.4 to 10
    # at placement while ``requested_quantity`` keeps the fraction, so a
    # strict ``>=`` would strand the intent PARTIALLY_FILLED forever with
    # IB's order record already gone — a permanent reservation leak on a BUY,
    # and a permanently muted stop-loss on a SELL (``shared/order_ledger.py``
    # documents ``has_active_sell``). The projector's own
    # ``rounding_complete`` guard (0 < shortfall < 1.0) is what stops this
    # mis-terminalizing a *material* partial.
    shortfall = float(intent.requested_quantity) - execution.cumulative_quantity
    return FillMessage(
        ticker=execution.ticker,
        timestamp=execution.executed_at,
        side=execution.side,
        quantity=execution.quantity,
        fill_price=execution.price,
        commission=execution.commission,
        commission_currency=execution.commission_currency,
        commission_trading=commission_trading,
        commission_fx_base_per_trading=execution.commission_fx_base_per_trading,
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
        order_done=shortfall < 1.0,
        recovery_source=RECOVERY_SOURCE_SWEEP,
    )


#: IB reports the side of an execution as BOT/SLD, not buy/sell.
_IB_SIDE = {"BOT": "buy", "SLD": "sell"}


def executions_from_ib_fills(
    fills: Sequence[object], *, fx_base_per_trading: float | None = None
) -> list[SweptExecution]:
    """Normalize ``ib_insync`` Fill objects, skipping anything unusable.

    Mirrors the payload the live callback builds (``ib_executor``'s
    ``_on_commission_report``) so a recovered fill is indistinguishable from
    the one that should have arrived. Duck-typed and import-free by design:
    the caller (Task 4, in ``scripts/run_paper.py``) is the only place that
    touches ``ib_insync``.

    ``fx_base_per_trading`` is IB's ``ExchangeRate`` account value, read by
    that caller, and is attached only to a non-USD commission — recording a
    rate against a USD one would put a number in the audit row that was
    never used in the conversion.
    """
    swept: list[SweptExecution] = []
    for fill in fills:
        execution = fill.execution
        side = _IB_SIDE.get(str(execution.side).upper())
        if side is None:
            continue
        report = getattr(fill, "commissionReport", None)
        currency = fill.contract.currency or "USD"
        # ``CommissionReport.currency`` defaults to ``''`` and
        # ``reqExecutionsAsync`` resolves on ``execDetailsEnd``, which TWS
        # sends before the commission reports are guaranteed to have landed.
        # Left blank, the projector rejects the fill outright ("unsupported
        # fill commission currency") and — because it writes the audit row
        # before validating — burns the execution permanently. The contract's
        # own currency is the truthful fallback: it is what the commission on
        # a US equity is denominated in.
        commission_currency = (
            str(getattr(report, "currency", "") or "").strip() or currency
        )
        swept.append(
            SweptExecution(
                execution_id=str(execution.execId),
                account_id=str(execution.acctNumber),
                ib_order_id=str(execution.orderId),
                con_id=int(fill.contract.conId),
                ticker=str(fill.contract.symbol),
                exchange=fill.contract.exchange or "SMART",
                currency=currency,
                side=side,
                quantity=float(execution.shares),
                cumulative_quantity=float(execution.cumQty),
                price=float(execution.price),
                commission=float(getattr(report, "commission", 0.0) or 0.0),
                commission_currency=commission_currency,
                executed_at=execution.time,
                commission_fx_base_per_trading=(
                    None if commission_currency == "USD" else fx_base_per_trading
                ),
            )
        )
    return swept
