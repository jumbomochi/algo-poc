from __future__ import annotations

# ruff: noqa: E402 -- direct-script execution needs the repo root first.

import argparse
import asyncio
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from scripts.paper_state import PaperTradingState
from scripts.run_paper import dump_paper_state
from shared.artifact_dir import durable_artifact_dir
from services.execution.ib_account import IBAccountReader
from services.execution.reconciliation import (
    MissedExitRepair,
    PositionReconciler,
    ReconciliationResult,
    RepairAction,
    RepairPlan,
    UnresolvedRepair,
    build_repair_plan,
)
from shared.config import load_config
from shared.models import (
    ExecutionFill,
    OrderIntent,
    OrderStatus,
    Position,
    ReconciliationReport,
)
from shared.order_ledger import ABSENT_AT_IB_REASON


class RepairRefusedError(RuntimeError):
    """Raised before a repair whenever an operator safety guard fails."""


class MissingExitPriceError(RepairRefusedError):
    """A missed exit has no exit price, and one is never guessed (KAN-85)."""


#: Marks a trade the repair tool reconstructed, so a gate reviewer can tell it
#: from a signal-driven exit. Same prefix as scripts/ops/restore_missed_exit.py.
MISSED_EXIT_REASON = "reconciliation repair: missed exit booked by reconcile_paper"

#: Stamped on the ``execution_fills`` row a missed-exit repair writes, as both
#: its ``ib_order_id`` (the real order id is not on the statement row) and its
#: ``recovery_source``.
REPAIR_SOURCE = "reconcile_paper_repair"

#: Share tolerance for "the sale accounts for the whole holding".
_QUANTITY_TOLERANCE = 1e-6

_PLAN_FIELDS = {"account_id", "created_at", "actions", "unresolved"}
_ACTION_FIELDS = {
    "action", "account_id", "portfolio", "con_id", "quantity"
}
_MISSED_EXIT_FIELDS = _ACTION_FIELDS | {
    "price", "executed_at", "execution_id", "commission",
    "classification", "evidence",
}
_UNRESOLVED_FIELDS = {
    "reason", "con_id", "ib_order_id",
    "candidate_recommendation_id", "candidate_portfolio",
}

#: How far back an intent expired on absence still counts as the candidate
#: owner of a ``missing_in_db`` position (KAN-96). The XLC case took 5 days
#: from fill to expiry; ten covers a long weekend on top with room to spare,
#: and is still short enough that an old, unrelated expiry is not named.
ABSENT_INTENT_LOOKBACK_DAYS = 10
_ACCOUNT_PATTERN = re.compile(r"^[A-Z0-9]+$")
_PORTFOLIO_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$")


def persist_reconciliation_report(
    session: Session,
    *,
    account_id: str,
    mode: str,
    result: ReconciliationResult,
) -> ReconciliationReport:
    report = ReconciliationReport(
        account_id=account_id,
        mode=mode,
        status=result.severity,
        entries_allowed=result.entries_allowed,
        result=result.to_dict(),
        created_at=datetime.now(timezone.utc),
    )
    session.add(report)
    session.flush()
    return report


#: Relative on purpose: the repair plan belongs beside the book it describes.
#: `durable_artifact_dir` relocates it out of a linked worktree so a
#: `git worktree remove` cannot delete it (2026-09-16).
DEFAULT_OUTPUT_DIR = Path("output/reconciliation")


def write_repair_plan(
    plan: RepairPlan, *, output_dir: Path = Path("output/reconciliation")
) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = plan.created_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"repair-plan-{stamp}.json"
    path.write_text(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
    return path


def _load_plan(path: Path) -> RepairPlan:
    try:
        payload = json.loads(Path(path).read_text())
        if not isinstance(payload, dict) or set(payload) != _PLAN_FIELDS:
            raise ValueError("top-level fields do not match the repair schema")
        account_id = payload["account_id"]
        if (
            not isinstance(account_id, str)
            or not account_id
            or _ACCOUNT_PATTERN.fullmatch(account_id) is None
        ):
            raise ValueError("account_id must be a non-empty broker account")
        if not isinstance(payload["created_at"], str):
            raise ValueError("created_at must be an ISO-8601 string")
        created_at = datetime.fromisoformat(payload["created_at"])
        if created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        if not isinstance(payload["actions"], list):
            raise ValueError("actions must be a list")
        if not isinstance(payload["unresolved"], list):
            raise ValueError("unresolved must be a list")
        actions = [
            _parse_repair_action(value, account_id)
            for value in payload["actions"]
        ]
        unresolved = [
            _parse_unresolved(value) for value in payload["unresolved"]
        ]
        return RepairPlan(
            account_id=account_id,
            created_at=created_at,
            actions=actions,
            unresolved=unresolved,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        OSError,
        UnicodeError,
    ) as exc:
        raise RepairRefusedError(f"invalid repair plan: {exc}") from exc


def _parse_repair_action(
    value: Any, plan_account_id: str
) -> RepairAction | MissedExitRepair:
    if isinstance(value, dict) and value.get("action") == "close_position_with_fill":
        if set(value) != _MISSED_EXIT_FIELDS:
            raise ValueError("missed-exit action fields do not match the schema")
    elif not isinstance(value, dict) or set(value) != _ACTION_FIELDS:
        raise ValueError("repair action fields do not match the schema")
    elif value["action"] != "set_position_quantity":
        raise ValueError("unsupported repair action")
    if value["account_id"] != plan_account_id:
        raise ValueError("repair action account does not match plan account")
    portfolio = value["portfolio"]
    if (
        not isinstance(portfolio, str)
        or _PORTFOLIO_PATTERN.fullmatch(portfolio) is None
    ):
        raise ValueError("portfolio is invalid")
    con_id = value["con_id"]
    if isinstance(con_id, bool) or not isinstance(con_id, int) or con_id <= 0:
        raise ValueError("con_id must be a positive integer")
    quantity = value["quantity"]
    if (
        isinstance(quantity, bool)
        or not isinstance(quantity, (int, float))
        or not math.isfinite(quantity)
        or quantity < 0
    ):
        raise ValueError("quantity must be a finite non-negative number")
    if value["action"] == "close_position_with_fill":
        return _parse_missed_exit(
            value, plan_account_id, portfolio, con_id, float(quantity)
        )
    return RepairAction(
        action="set_position_quantity",
        account_id=plan_account_id,
        portfolio=portfolio,
        con_id=con_id,
        quantity=float(quantity),
    )


def _is_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _parse_missed_exit(
    value: dict[str, Any],
    plan_account_id: str,
    portfolio: str,
    con_id: int,
    quantity: float,
) -> MissedExitRepair:
    """Validate shape only. A missing price or date is legal in a plan —
    it is how the plan asks the operator for it — and is refused at apply."""
    if quantity <= 0:
        raise ValueError("missed-exit quantity must be positive")
    price = value["price"]
    if price is not None and (not _is_number(price) or price <= 0):
        raise ValueError("price must be a finite positive number or null")
    executed_at = value["executed_at"]
    if executed_at is not None:
        if not isinstance(executed_at, str):
            raise ValueError("executed_at must be an ISO-8601 string or null")
        if datetime.fromisoformat(executed_at).tzinfo is None:
            raise ValueError("executed_at must include a timezone")
    execution_id = value["execution_id"]
    if execution_id is not None and (
        not isinstance(execution_id, str) or not execution_id.strip()
    ):
        raise ValueError("execution_id must be non-empty or null")
    commission = value["commission"]
    if commission is not None and (not _is_number(commission) or commission < 0):
        raise ValueError("commission must be a finite non-negative number or null")
    if value["classification"] != "missed_exit":
        raise ValueError("missed-exit classification must be missed_exit")
    if not isinstance(value["evidence"], dict):
        raise ValueError("missed-exit evidence must be an object")
    return MissedExitRepair(
        action="close_position_with_fill",
        account_id=plan_account_id,
        portfolio=portfolio,
        con_id=con_id,
        quantity=quantity,
        price=None if price is None else float(price),
        executed_at=executed_at,
        execution_id=execution_id,
        commission=None if commission is None else float(commission),
        classification="missed_exit",
        evidence=value["evidence"],
    )


def _require_exit_evidence(plan: RepairPlan) -> None:
    """Refuse a missed exit the operator has not fully supplied. Never default
    one: entry price books a plausible-looking zero, and a wrong price is a
    wrong P&L on the gate record for good."""
    for action in plan.actions:
        if not isinstance(action, MissedExitRepair):
            continue
        target = f"missed exit for con_id {action.con_id} in {action.portfolio!r}"
        if action.price is None:
            raise MissingExitPriceError(
                f"{target} has no exit price. Take it from the IB statement "
                "for the trade date and write it into the plan's price "
                "field; it is never defaulted."
            )
        if action.executed_at is None:
            raise RepairRefusedError(
                f"{target} has no executed_at. Take the execution time from "
                "the IB statement -- statement times are US Eastern with no "
                "offset, so write e.g. 2026-08-28T09:30:00-04:00, not +00:00."
            )
        if action.execution_id is None:
            raise RepairRefusedError(
                f"{target} has no execution_id. Take it from the IB "
                "statement; it is recorded in execution_fills so the same "
                "execution can never be booked twice."
            )
        if action.commission is None:
            raise RepairRefusedError(
                f"{target} has no commission. Take it from the IB statement "
                "(0 only if the statement says 0); it is never defaulted."
            )


def _parse_unresolved(value: Any) -> UnresolvedRepair:
    if (
        not isinstance(value, dict)
        or "reason" not in value
        or not set(value) <= _UNRESOLVED_FIELDS
    ):
        raise ValueError("unresolved item fields do not match the schema")
    reason = value["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("unresolved reason must be non-empty")
    con_id = value.get("con_id")
    if con_id is not None and (
        isinstance(con_id, bool) or not isinstance(con_id, int) or con_id <= 0
    ):
        raise ValueError("unresolved con_id must be a positive integer")
    ib_order_id = value.get("ib_order_id")
    if ib_order_id is not None and (
        not isinstance(ib_order_id, str) or not ib_order_id
    ):
        raise ValueError("unresolved ib_order_id must be non-empty")
    candidate = {}
    for key in ("candidate_recommendation_id", "candidate_portfolio"):
        text = value.get(key)
        if text is not None and (not isinstance(text, str) or not text):
            raise ValueError(f"unresolved {key} must be non-empty")
        candidate[key] = text
    return UnresolvedRepair(
        reason=reason, con_id=con_id, ib_order_id=ib_order_id, **candidate
    )


def recent_absent_intents(
    session: Session, *, account_id: str, now: datetime
) -> list[OrderIntent]:
    """Intents expired because IB had no record of their order (KAN-96).

    The only evidence of who owned a fill the book missed: the startup
    restore expires a filled-but-unseen order with ``ABSENT_AT_IB_REASON``,
    and the repair plan names it as the candidate for the matching
    ``missing_in_db`` position.
    """
    since = now - timedelta(days=ABSENT_INTENT_LOOKBACK_DAYS)
    return list(session.scalars(
        select(OrderIntent).where(
            OrderIntent.account_id == account_id,
            OrderIntent.status == OrderStatus.EXPIRED.value,
            OrderIntent.reason == ABSENT_AT_IB_REASON,
            # Only a BUY can explain shares IB holds that the book does not.
            func.upper(OrderIntent.action) == "BUY",
            OrderIntent.terminal_at >= since,
        ).order_by(OrderIntent.terminal_at)
    ))


def apply_repair_plan(session: Session, *, plan_path: Path) -> None:
    """Apply one previously reviewed paper repair plan behind hard guards.

    This function is deliberately separate from report generation. It must
    never be called by scheduled reconciliation.
    """
    if not sys.stdin.isatty():
        raise RepairRefusedError("repair requires interactive TTY stdin")
    plan = _load_plan(Path(plan_path))
    if plan.unresolved:
        raise RepairRefusedError("repair plan contains unresolved mappings")
    if not plan.account_id.startswith("DU"):
        raise RepairRefusedError("repair plan is not for an IB paper account")
    _require_exit_evidence(plan)

    backup_dir = Path(plan_path).resolve().parent
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dump_paper_state(
        session, backup_dir / f"paper_state_pre_repair_{stamp}.json"
    )
    confirmation = input(
        "Type APPLY PAPER REPAIR to apply the reviewed plan: "
    )
    if confirmation != "APPLY PAPER REPAIR":
        session.rollback()
        raise RepairRefusedError("exact repair confirmation was not provided")

    try:
        for action in plan.actions:
            _apply_action(session, plan.account_id, action)
        session.commit()
    except Exception:
        session.rollback()
        raise
    print(
        "Repair plan applied. Run scripts/reconcile_paper.py --report "
        "and verify entries_allowed before enabling entries."
    )


def _apply_action(
    session: Session,
    plan_account_id: str,
    action: RepairAction | MissedExitRepair,
) -> None:
    if action.action not in {"set_position_quantity", "close_position_with_fill"}:
        raise RepairRefusedError(
            f"unsupported serialized repair action {action.action!r}"
        )
    candidates = list(session.scalars(
        select(Position).where(
            Position.con_id == action.con_id,
            Position.status == "open",
        ).with_for_update()
    ))
    if any(position.account_id is None for position in candidates):
        raise RepairRefusedError(
            "repair target overlaps an unowned legacy position"
        )
    positions = [
        position for position in candidates
        if position.account_id == plan_account_id
        and position.portfolio == action.portfolio
        and position.con_id == action.con_id
    ]
    if len(positions) != 1:
        raise RepairRefusedError(
            "serialized repair target must identify exactly one open position"
        )
    if action.quantity < 0:
        raise RepairRefusedError("repair quantity cannot be negative")
    if isinstance(action, MissedExitRepair):
        _book_missed_exit(session, positions[0], action)
        return
    if action.quantity == 0 and _has_unoffset_buy_fills(
        session, plan_account_id, action.con_id
    ):
        raise RepairRefusedError(
            f"con_id {action.con_id} has fill history (a filled BUY with no "
            "offsetting SELL), so it was really held: a zero-quantity set "
            "would write off its exit with no trade and no cash. Regenerate "
            "the plan with --report, which proposes close_position_with_fill."
        )
    positions[0].quantity = float(action.quantity)
    if action.quantity == 0:
        positions[0].status = "closed"
        positions[0].closed_at = datetime.now(timezone.utc)
    session.flush()


def _has_unoffset_buy_fills(
    session: Session, account_id: str, con_id: int
) -> bool:
    net = 0.0
    for fill in session.scalars(
        select(ExecutionFill).where(
            ExecutionFill.account_id == account_id,
            ExecutionFill.con_id == con_id,
            ExecutionFill.projection_applied.is_(True),
        )
    ):
        side = fill.side.upper()
        if side == "BUY":
            net += float(fill.quantity)
        elif side == "SELL":
            net -= float(fill.quantity)
    return net > _QUANTITY_TOLERANCE


def _book_missed_exit(
    session: Session, position: Position, action: MissedExitRepair
) -> None:
    """Write what a real exit would have written: the fill, trade, P&L, cash.

    The SELL goes into ``execution_fills`` too. Without it the contract's fill
    history stays long forever, so the next --report would classify a true
    phantom on it as a missed exit and the zero-quantity guard would refuse
    it; and the duplicate-execution check below could never see this repair.
    """
    residue = float(position.quantity) - action.quantity
    if abs(residue) > _QUANTITY_TOLERANCE:
        raise RepairRefusedError(
            f"missed exit of {action.quantity} does not match the holding of "
            f"{position.quantity}: a residue of {residue:.6f} shares would be "
            "left open (or oversold), and reconciliation flags it again on "
            "the next run. Account for every execution on the IB statement."
        )
    if session.scalar(
        select(ExecutionFill.id).where(
            ExecutionFill.account_id == position.account_id,
            ExecutionFill.execution_id == action.execution_id,
        )
    ) is not None:
        raise RepairRefusedError(
            f"execution {action.execution_id!r} is already recorded; booking "
            "it again would double-count the P&L."
        )
    exit_reason = f"{MISSED_EXIT_REASON} (execution_id {action.execution_id})"
    executed_at = datetime.fromisoformat(action.executed_at)
    session.add(ExecutionFill(
        account_id=position.account_id,
        execution_id=action.execution_id,
        ib_order_id=REPAIR_SOURCE,
        recommendation_id=None,
        portfolio=position.portfolio,
        con_id=position.con_id,
        symbol=position.ticker,
        exchange=position.exchange,
        currency=position.currency,
        side="SELL",
        quantity=action.quantity,
        price=float(action.price),
        commission=float(action.commission),
        commission_trading=float(action.commission),
        cumulative_quantity=action.quantity,
        executed_at=executed_at,
        projection_applied=True,
        recovery_source=REPAIR_SOURCE,
        recovered_at=datetime.now(timezone.utc),
    ))
    try:
        PaperTradingState(session)._apply_fill_accounting(
            account_id=position.account_id,
            portfolio=position.portfolio,
            ticker=position.ticker,
            action="sell",
            quantity=action.quantity,
            price=float(action.price),
            fill_datetime=executed_at,
            commission=float(action.commission),
            con_id=position.con_id,
            exchange=position.exchange,
            currency=position.currency,
            strict_quantity=True,
            exit_reason=exit_reason,
        )
    except ValueError as exc:
        raise RepairRefusedError(f"missed exit refused: {exc}") from exc


def reconcile_snapshot(
    session: Session, snapshot: Any
) -> tuple[ReconciliationResult, RepairPlan]:
    """Compare one broker snapshot and persist only its audit report."""
    grouped = list(session.execute(
        select(
            Position.con_id,
            func.sum(Position.quantity),
            func.count(Position.id),
            func.min(Position.portfolio),
        ).where(
            Position.status == "open",
            Position.account_id == snapshot.account_id,
        ).group_by(Position.con_id)
    ))
    db_positions: dict[int, Any] = {}
    missing_contract_rows = 0
    for con_id, quantity, count, portfolio in grouped:
        if con_id is None:
            missing_contract_rows += int(count)
            continue
        db_positions[(snapshot.account_id, int(con_id))] = SimpleNamespace(
            account_id=snapshot.account_id,
            quantity=float(quantity),
            portfolio=portfolio if count == 1 else None,
        )

    unowned_position_rows = int(session.scalar(
        select(func.count(Position.id)).where(
            Position.status == "open",
            Position.account_id.is_(None),
        )
    ) or 0)

    broker_order_statuses = (
        OrderStatus.SUBMITTED.value,
        OrderStatus.PARTIALLY_FILLED.value,
    )
    active_statuses = (
        OrderStatus.APPROVED.value,
        *broker_order_statuses,
    )
    active_intents = list(session.scalars(
        select(OrderIntent).where(
            OrderIntent.account_id == snapshot.account_id,
            OrderIntent.status.in_(active_statuses),
        )
    ))
    db_orders = {
        str(intent.ib_order_id): intent
        for intent in active_intents
        if intent.ib_order_id is not None
        and intent.status in broker_order_statuses
    }
    fills = list(session.scalars(
        select(ExecutionFill).where(
            ExecutionFill.account_id == snapshot.account_id
        )
    ))

    result = PositionReconciler(account_id=snapshot.account_id).reconcile(
        broker_positions=snapshot.positions,
        db_positions=db_positions,
        broker_orders=snapshot.open_orders,
        db_orders=db_orders,
        execution_fills=fills,
        active_intents=active_intents,
    )
    if missing_contract_rows:
        result.discrepancies.append({
            "type": "db_position_missing_contract_id",
            "count": missing_contract_rows,
            "auto_correct": False,
        })
        result.severity = "major"
    if unowned_position_rows:
        result.discrepancies.append({
            "type": "db_position_missing_account_id",
            "count": unowned_position_rows,
            "auto_correct": False,
        })
        result.severity = "major"
    persist_reconciliation_report(
        session,
        account_id=snapshot.account_id,
        mode=snapshot.mode,
        result=result,
    )
    absent = recent_absent_intents(
        session,
        account_id=snapshot.account_id,
        now=datetime.now(timezone.utc),
    )
    return result, build_repair_plan(result, fills, absent_intents=absent)


async def _read_broker_snapshot(
    args: argparse.Namespace,
    mode: str,
    *,
    expected_base_currency: str,
    trading_currency: str,
) -> Any:
    from ib_insync import IB

    ib = IB()
    try:
        await ib.connectAsync(
            args.ib_host,
            args.ib_port,
            clientId=args.ib_client_id,
            readonly=True,
            timeout=15,
        )
        return await IBAccountReader(
            ib,
            expected_mode=mode,
            expected_base_currency=expected_base_currency,
            trading_currency=trading_currency,
        ).snapshot()
    finally:
        if ib.isConnected():
            ib.disconnect()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed broker/database paper reconciliation"
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--report", action="store_true")
    operation.add_argument("--apply-plan", type=Path)
    parser.add_argument("--db-url")
    parser.add_argument("--ib-host", default="127.0.0.1")
    parser.add_argument("--ib-port", type=int, default=7497)
    parser.add_argument("--ib-client-id", type=int, default=57)
    parser.add_argument(
        "--output-dir", type=Path, default=None
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    config = load_config("config/default.yaml")
    db_url = args.db_url or config.database.url
    session_factory = sessionmaker(bind=create_engine(db_url))
    with session_factory() as session:
        if args.apply_plan is not None:
            try:
                apply_repair_plan(session, plan_path=args.apply_plan)
            except RepairRefusedError as exc:
                print(f"Refusing repair: {exc}", file=sys.stderr)
                return 2
            return 0

        snapshot = asyncio.run(
            _read_broker_snapshot(
                args,
                "paper",
                expected_base_currency=config.currency.expected_base_currency,
                trading_currency=config.currency.trading_currency,
            )
        )
        result, plan = reconcile_snapshot(session, snapshot)
        plan_path = write_repair_plan(
            plan,
            output_dir=durable_artifact_dir(
                args.output_dir or DEFAULT_OUTPUT_DIR,
                explicit=args.output_dir is not None,
            ),
        )
        session.commit()
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        print(f"Repair plan: {plan_path}")
        for entry in plan.unresolved:
            candidate = getattr(entry, "candidate_recommendation_id", None)
            if candidate:
                print(
                    f"Probable missed fill: con_id {entry.con_id} matches "
                    f"{candidate} ({entry.candidate_portfolio}), expired "
                    "because IB no longer knew the order. --apply-plan cannot "
                    "repair it; rebuild the fill from an IB Flex Trades "
                    "statement with scripts/ops/restore_missed_entries.py."
                )
            elif entry.reason == "sleeve_mapping_required" and any(
                d.get("type") == "missing_in_db" and d.get("con_id") == entry.con_id
                for d in result.discrepancies
            ):
                print(
                    f"IB holds con_id {entry.con_id} and the book has no "
                    "record of it, and no recently expired order explains "
                    "it. --apply-plan cannot repair it; find the fill on an "
                    "IB Flex Trades statement and rebuild it with "
                    "scripts/ops/restore_missed_entries.py."
                )
        return 0 if result.entries_allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MissedExitRepair",
    "MissingExitPriceError",
    "RepairAction",
    "RepairPlan",
    "RepairRefusedError",
    "apply_repair_plan",
    "persist_reconciliation_report",
    "reconcile_snapshot",
    "write_repair_plan",
]
