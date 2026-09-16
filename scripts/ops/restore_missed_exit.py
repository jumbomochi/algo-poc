#!/usr/bin/env python3
"""Put back an exit that was written off instead of recorded.

KAN-85, slice one. ``reconcile_paper.py::_apply_action`` has exactly one
repair, ``set_position_quantity -> 0``, which sets the quantity, the status and
``closed_at`` and touches nothing else: no trade, no realised P&L, no cash. When
the position was really SOLD at IB and only the fill was missed, that deletes a
completed round trip and the sale proceeds together.

It has run twice:

    PANW  con_id 110619459  sector_rotation  2026-08-28  equity -3,476.99
    LLY   con_id 9160       sector_rotation  2026-09-14  equity -2,242.52

Each drop is exactly ``quantity x current_price`` — the whole mark value gone,
no cash credited — and ``trades`` carries neither round trip. PANW was a +21.9%
winner, so this is not a rounding argument: the book understates equity by
**-5,719.51** and the gate evidence has an unexplained step in it.

WHY THIS IS NOT A TWO-LINE SCRIPT
---------------------------------
Three things make the obvious version wrong, and every one of them is a refusal
below rather than a cleverness.

**1. The residue trap.** Both positions held MORE than their whole-share sell
covered — PANW held 9.0819 and sold 9, LLY held 2.0100 and sold 2, because the
account has no fractional API support. Reopen the held quantity, apply the sold
quantity, and the remainder stays OPEN; reconciliation flags it as
``missing_in_ib`` on the next run and fail-closes every buy in all six sleeves.
So a sell that does not account for the entire holding is refused, and the
refusal names the residue.

**2. Missed exit versus phantom.** That distinction is the whole of KAN-85. A
position with a filled BUY in ``execution_fills`` and no offsetting SELL was
real; one with no fill history may never have existed, and writing a sell for it
would invent a round trip — worse than the hole it fills. No entry fill, no
restore.

**3. The projector will not finish the job.** ``_advance_intent``
(projector.py:414) returns early when the intent is already terminal, so an
order terminalized ``EXPIRED`` by the session-boundary path stays EXPIRED even
once its fill is recorded. UNH advanced cleanly on 2026-09-16 only because its
intent was still ``SUBMITTED``. PANW's order 130 and LLY's order 139 are both
EXPIRED, so this tool corrects them itself.

WHAT IT DOES NOT DO
-------------------
It never guesses a price. The exit price comes from an IB Account Management
statement, supplied by the operator; a wrong price is a wrong P&L forever, and
defaulting to the entry price would book a plausible-looking zero. It refuses
rather than defaults, everywhere.

The restored trade is marked as a reconstruction in ``exit_reason``. Evidence
that was rebuilt from a statement weeks later must never be indistinguishable
from evidence that was observed on the day.

Usage (dry run first, always):

    python scripts/ops/restore_missed_exit.py --con-id 110619459 \\
        --portfolio sector_rotation --held-quantity 9.0819 \\
        --sold-quantity 9.0819 --price 382.85 \\
        --executed-at 2026-08-20T13:30:00Z \\
        --execution-id statement-panw-20260820 --commission 1.05
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.portfolio_accounting.projector import FillProjector  # noqa: E402
from shared.config import load_config  # noqa: E402
from shared.models.order_ledger import (  # noqa: E402
    ExecutionFill,
    OrderIntent,
    OrderStatus,
)
from shared.models.portfolio import Position, Trade  # noqa: E402
from shared.schemas.messages import FillMessage  # noqa: E402

#: Typed by the operator, in full, to apply. Same shape as the repair tool's
#: "APPLY PAPER REPAIR" — a confirmation you can produce by accident is not one.
CONFIRMATION = "RESTORE MISSED EXIT"

#: Marks the trade as rebuilt rather than observed. A gate reviewer must be able
#: to tell the two apart at a glance.
EXIT_REASON = "reconciliation repair: exit reconstructed from IB statement"

#: Share tolerance. Tight enough that a genuine fractional residue (0.01 shares
#: on LLY, the smallest real case) is refused rather than absorbed.
QUANTITY_TOLERANCE = 1e-6


class RestoreRefusedError(RuntimeError):
    """The restore cannot be made safely. Every one of these is deliberate."""


@dataclass(frozen=True)
class RestorePlan:
    """What would be written, computed without touching anything."""

    position_id: int
    ticker: str
    portfolio: str
    con_id: int
    account_id: str
    held_quantity: float
    sold_quantity: float
    price: float
    executed_at: datetime
    execution_id: str
    commission: float
    entry_price: float
    opened_at: datetime
    intent_id: int | None
    intent_status: str | None
    recommendation_id: str | None
    # The IB order id, not the intent's DB id. The projector validates the
    # fill against the durable intent and rejects a mismatch with
    # UnattributedFillError("fill conflicts with durable intent on: order").
    ib_order_id: str | None

    @property
    def estimated_pnl(self) -> float:
        return (self.price - self.entry_price) * self.sold_quantity

    @property
    def estimated_proceeds(self) -> float:
        return self.price * self.sold_quantity - self.commission


def plan_restore(
    session: Session,
    *,
    con_id: int,
    portfolio: str,
    held_quantity: float,
    sold_quantity: float,
    price: float,
    executed_at: datetime,
    execution_id: str,
    commission: float = 0.0,
) -> RestorePlan:
    """Check every precondition and compute the plan. Mutates nothing."""
    if not (price > 0) or math.isnan(price) or math.isinf(price):
        raise RestoreRefusedError(
            f"exit price {price!r} is not a usable price. It comes from the IB "
            "statement for this trade date and is never defaulted -- a wrong "
            "price is a wrong P&L on the gate record forever."
        )
    if not (held_quantity > 0) or not (sold_quantity > 0):
        raise RestoreRefusedError("held and sold quantities must both be positive")

    positions = list(session.scalars(
        select(Position).where(
            Position.con_id == con_id,
            Position.portfolio == portfolio,
        )
    ))
    if len(positions) != 1:
        raise RestoreRefusedError(
            f"expected exactly one position for con_id {con_id} in {portfolio!r}; "
            f"found {len(positions)}. With no position there is nothing to "
            "restore, and with several the target is ambiguous."
        )
    position = positions[0]

    if position.status != "closed" or abs(float(position.quantity)) > QUANTITY_TOLERANCE:
        raise RestoreRefusedError(
            f"position {position.id} is {position.status!r} at quantity "
            f"{position.quantity}; this tool restores a position that was "
            "written off (closed at zero). An open position is reconciliation's "
            "job, and applying here would sell it twice."
        )
    if not (position.account_id or "").startswith("DU"):
        raise RestoreRefusedError(
            f"account {position.account_id!r} is not an IB paper account. A live "
            "ledger is never reconstructed by a script."
        )

    # The missed-exit / phantom test. A filled BUY proves the position was real.
    entry_fills = session.scalar(
        select(func.count()).select_from(ExecutionFill).where(
            ExecutionFill.con_id == con_id,
            ExecutionFill.account_id == position.account_id,
            func.lower(ExecutionFill.side) == "buy",
        )
    ) or 0
    if entry_fills == 0:
        raise RestoreRefusedError(
            f"{position.ticker} has no entry fill in execution_fills, so it may "
            "be a phantom rather than a missed exit. Writing a sell for a "
            "position that never existed invents a round trip, which is worse "
            "than the hole it fills."
        )

    if session.scalar(
        select(ExecutionFill).where(
            ExecutionFill.account_id == position.account_id,
            ExecutionFill.execution_id == execution_id,
        )
    ) is not None:
        raise RestoreRefusedError(
            f"execution {execution_id!r} is already recorded; restoring it again "
            "would double-count the P&L."
        )

    if session.scalar(
        select(Trade).where(
            Trade.ticker == position.ticker,
            Trade.portfolio == portfolio,
            Trade.side == "sell",
        )
    ) is not None:
        raise RestoreRefusedError(
            f"a sell trade for {position.ticker} in {portfolio!r} is already on "
            "the record; a second one would double-count the P&L."
        )

    # The residue trap. Refused, never absorbed -- see the module docstring.
    residue = held_quantity - sold_quantity
    if abs(residue) > QUANTITY_TOLERANCE:
        raise RestoreRefusedError(
            f"the sale of {sold_quantity} does not account for the whole holding "
            f"of {held_quantity}: a remainder/residue of {residue:.4f} shares "
            f"would be left OPEN, and reconciliation flags that as missing_in_ib "
            f"on the next run -- re-blocking every buy in all six sleeves. Bring "
            f"the IB statement for every execution against this position: either "
            f"a further sale accounts for the residue, or the holding was not "
            f"what this book believed."
        )

    intent = session.scalars(
        select(OrderIntent)
        .where(
            OrderIntent.con_id == con_id,
            OrderIntent.portfolio == portfolio,
            OrderIntent.action == "SELL",
        )
        .order_by(OrderIntent.created_at.desc())
    ).first()

    return RestorePlan(
        position_id=position.id,
        ticker=position.ticker,
        portfolio=portfolio,
        con_id=con_id,
        account_id=position.account_id,
        held_quantity=float(held_quantity),
        sold_quantity=float(sold_quantity),
        price=float(price),
        executed_at=executed_at,
        execution_id=execution_id,
        commission=float(commission),
        entry_price=float(position.avg_entry_price),
        opened_at=position.opened_at,
        intent_id=intent.id if intent else None,
        intent_status=intent.status if intent else None,
        recommendation_id=intent.recommendation_id if intent else None,
        ib_order_id=intent.ib_order_id if intent else None,
    )


def _reopen(session: Session, plan: RestorePlan) -> None:
    position = session.get(Position, plan.position_id)
    position.quantity = plan.held_quantity
    position.status = "open"
    position.closed_at = None
    session.commit()


def _reclose(session: Session, plan: RestorePlan) -> None:
    """Compensation: put the write-off back exactly as it was."""
    session.rollback()
    position = session.get(Position, plan.position_id)
    position.quantity = 0.0
    position.status = "closed"
    position.closed_at = position.closed_at or datetime.now(timezone.utc)
    session.commit()


def apply_restore(session: Session, plan: RestorePlan, *, confirm: str) -> None:
    """Reopen, project the real fill, correct the intent, verify.

    Three transactions rather than one, because ``FillProjector.apply`` owns
    its own (``with self.session.begin()``) and refuses a session that already
    carries pending work. So a failure is handled by compensation instead: the
    position is put back exactly as the write-off left it. A reopened position
    left behind would be read as a NEW phantom by the next reconciliation and
    would fail-close the book -- the outcome this whole tool exists to avoid.
    """
    if confirm != CONFIRMATION:
        raise RestoreRefusedError(
            f"exact confirmation required: expected {CONFIRMATION!r}"
        )

    _reopen(session, plan)
    try:
        fill = FillMessage(
            ticker=plan.ticker,
            timestamp=plan.executed_at,
            side="sell",
            quantity=plan.sold_quantity,
            cumulative_quantity=plan.sold_quantity,
            fill_price=plan.price,
            commission=plan.commission,
            commission_currency="USD",
            commission_trading=plan.commission,
            recommendation_id=plan.recommendation_id or f"restore-{plan.execution_id}",
            order_id=plan.ib_order_id or plan.execution_id,
            execution_id=plan.execution_id,
            account_id=plan.account_id,
            portfolio=plan.portfolio,
            con_id=plan.con_id,
            exchange="SMART",
            currency="USD",
            order_done=True,
        )
        FillProjector(session).apply(fill)
    except Exception:
        # The projector owns its transaction and rolled its own work back, so
        # the only thing left over is the reopen. Put it back or the next
        # reconciliation reads a NEW phantom and fail-closes the book.
        _reclose(session, plan)
        raise

    # Past this point the accounting is COMMITTED and correct. A failure below
    # must never roll it back -- it would delete a true trade to tidy up a
    # bookkeeping field. Report and let the operator finish it by hand.
    try:
        # The projector will not do this: _advance_intent returns early on a
        # terminal status, so an EXPIRED order stays EXPIRED even once its fill
        # is recorded. That is KAN-87 AC4, and until it ships this tool closes
        # the gap for the rows it touches.
        if plan.intent_id is not None:
            intent = session.get(OrderIntent, plan.intent_id)
            intent.status = OrderStatus.FILLED.value
            intent.filled_quantity = max(
                float(intent.filled_quantity), plan.sold_quantity
            )
            intent.updated_at = datetime.now(timezone.utc)

        # Mark the trade as reconstructed. Done here rather than through the
        # fill because FillMessage carries no exit_reason -- the projector takes
        # it from the intent, which for a write-off says nothing useful.
        trade = session.scalars(
            select(Trade)
            .where(Trade.ticker == plan.ticker, Trade.side == "sell")
            .order_by(Trade.id.desc())
        ).first()
        if trade is not None:
            trade.exit_reason = EXIT_REASON
        session.commit()
    except Exception as exc:  # noqa: BLE001 -- the accounting already landed
        session.rollback()
        raise RestoreRefusedError(
            "the fill was projected and the accounting is CORRECT, but the "
            f"intent/exit_reason update failed: {exc!r}. Do NOT re-run this "
            "tool -- it would refuse as already-recorded, which is right. "
            "Finish the intent by hand."
        ) from exc

    _verify(session, plan)


def _verify(session: Session, plan: RestorePlan) -> None:
    """Refuse to report success unless the end state is exactly right."""
    session.expire_all()
    position = session.get(Position, plan.position_id)
    problems = []
    # paper_state.py:296 DELETES a fully-sold position rather than closing it,
    # so gone is the expected end state. Still open is the failure that matters:
    # reconciliation would read it as a new phantom.
    if position is not None and position.status == "open":
        problems.append(
            f"position {position.id} is still open at {position.quantity}"
        )
    if session.scalar(
        select(Trade).where(Trade.ticker == plan.ticker, Trade.side == "sell")
    ) is None:
        problems.append("no sell trade was written")
    if problems:
        raise RestoreRefusedError(
            "restore applied but the end state is wrong: " + "; ".join(problems)
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restore an exit a repair wrote off instead of recording."
    )
    parser.add_argument("--con-id", type=int, required=True)
    parser.add_argument("--portfolio", required=True)
    parser.add_argument(
        "--held-quantity", type=float, required=True,
        help="shares the book held before the write-off (equity drop / mark price)",
    )
    parser.add_argument(
        "--sold-quantity", type=float, required=True,
        help="shares the IB statement says were sold; must account for the whole "
             "holding or the restore is refused",
    )
    parser.add_argument(
        "--price", type=float, required=True,
        help="executed price from the IB statement; never defaulted",
    )
    parser.add_argument("--executed-at", required=True, help="ISO-8601, e.g. 2026-08-20T13:30:00Z")
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--commission", type=float, default=0.0)
    parser.add_argument("--database-url", default=None)
    parser.add_argument(
        "--apply", action="store_true",
        help="Write the restore (default: dry-run report only).",
    )
    args = parser.parse_args(argv)

    url = args.database_url or load_config("config/default.yaml").database.url
    engine = create_engine(url)
    with Session(engine) as session:
        plan = plan_restore(
            session,
            con_id=args.con_id,
            portfolio=args.portfolio,
            held_quantity=args.held_quantity,
            sold_quantity=args.sold_quantity,
            price=args.price,
            executed_at=datetime.fromisoformat(
                args.executed_at.replace("Z", "+00:00")
            ),
            execution_id=args.execution_id,
            commission=args.commission,
        )
        print(f"  position   {plan.position_id}  {plan.ticker}  {plan.portfolio}")
        print(f"  held       {plan.held_quantity}  @ entry {plan.entry_price}")
        print(f"  sell       {plan.sold_quantity}  @ {plan.price}  "
              f"commission {plan.commission}")
        print(f"  executed   {plan.executed_at.isoformat()}  ({plan.execution_id})")
        print(f"  intent     {plan.intent_id} ({plan.intent_status})")
        print(f"  => trade pnl {plan.estimated_pnl:+,.2f}, "
              f"proceeds {plan.estimated_proceeds:,.2f}")
        if not args.apply:
            print("\nDry-run only. Re-run with --apply to write it.")
            return 0
        if not sys.stdin.isatty():
            raise RestoreRefusedError("--apply requires an interactive TTY")
        answer = input(f"\nType {CONFIRMATION} to write this exit: ")
        apply_restore(session, plan, confirm=answer.strip())
        print("Restored. Run scripts/reconcile_paper.py --report to confirm the "
              "book still reconciles.")
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
