from __future__ import annotations

# ruff: noqa: E402 -- direct-script execution needs the repo root first.

import argparse
import asyncio
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from scripts.run_paper import dump_paper_state
from services.execution.ib_account import IBAccountReader
from services.execution.reconciliation import (
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


class RepairRefusedError(RuntimeError):
    """Raised before a repair whenever an operator safety guard fails."""


_PLAN_FIELDS = {"account_id", "created_at", "actions", "unresolved"}
_ACTION_FIELDS = {
    "action", "account_id", "portfolio", "con_id", "quantity"
}
_UNRESOLVED_FIELDS = {"reason", "con_id", "ib_order_id"}
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


def _parse_repair_action(value: Any, plan_account_id: str) -> RepairAction:
    if not isinstance(value, dict) or set(value) != _ACTION_FIELDS:
        raise ValueError("repair action fields do not match the schema")
    if value["action"] != "set_position_quantity":
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
    return RepairAction(
        action="set_position_quantity",
        account_id=plan_account_id,
        portfolio=portfolio,
        con_id=con_id,
        quantity=float(quantity),
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
    return UnresolvedRepair(
        reason=reason, con_id=con_id, ib_order_id=ib_order_id
    )


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
    session: Session, plan_account_id: str, action: RepairAction
) -> None:
    if action.action != "set_position_quantity":
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
    positions[0].quantity = float(action.quantity)
    if action.quantity == 0:
        positions[0].status = "closed"
        positions[0].closed_at = datetime.now(timezone.utc)
    session.flush()


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
    return result, build_repair_plan(result)


async def _read_broker_snapshot(args: argparse.Namespace, mode: str) -> Any:
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
        return await IBAccountReader(ib, expected_mode=mode).snapshot()
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
        "--output-dir", type=Path, default=Path("output/reconciliation")
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

        snapshot = asyncio.run(_read_broker_snapshot(args, "paper"))
        result, plan = reconcile_snapshot(session, snapshot)
        plan_path = write_repair_plan(plan, output_dir=args.output_dir)
        session.commit()
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        print(f"Repair plan: {plan_path}")
        return 0 if result.entries_allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "RepairAction",
    "RepairPlan",
    "RepairRefusedError",
    "apply_repair_plan",
    "persist_reconciliation_report",
    "reconcile_snapshot",
    "write_repair_plan",
]
