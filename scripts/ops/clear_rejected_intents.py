from __future__ import annotations

"""Delete a day's RISK_REJECTED buy intents so a re-run can re-propose them.

When a bug (e.g. the phantom-drawdown circuit breaker) wrongly rejects a day's
buys, the intents land in terminal RISK_REJECTED. `run_paper.py` is idempotent
per `recommendation_id` (date-stamped), so a same-day re-run neither re-proposes
nor re-publishes them — the risk service skips terminal intents. This tool
clears those rows so the next run creates fresh PROPOSED intents at current
prices.

Safety (mirrors scripts/ops/retire_legacy_positions.py):
  * Dry-run by default; --apply required to delete.
  * PAPER ONLY: refuses any account id not starting with "DU".
  * Scoped to one account + one run-date + status RISK_REJECTED (buys only).
  * --apply requires an interactive TTY and an exact typed count. No bypass.
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from shared.config import load_config
from shared.models import OrderIntent, OrderStatus


class ClearRefusedError(RuntimeError):
    """Raised when a safety guard blocks the delete."""


@dataclass(frozen=True)
class ClearSummary:
    count: int
    rows: list[tuple[str, str, float]]  # recommendation_id, symbol, quantity


def _select_rejected(
    session: Session, *, account_id: str, run_date: date
) -> list[OrderIntent]:
    prefix = f"sleeve-{run_date}-{account_id}-"
    return list(
        session.scalars(
            select(OrderIntent).where(
                OrderIntent.account_id == account_id,
                OrderIntent.status == OrderStatus.RISK_REJECTED.value,
                OrderIntent.action == "BUY",
                OrderIntent.recommendation_id.like(f"{prefix}%"),
            )
        )
    )


def clear_rejected_intents(
    session: Session, *, account_id: str, run_date: date, apply: bool, confirm: str | None
) -> ClearSummary:
    if not account_id.startswith("DU"):
        raise ClearRefusedError(f"refusing: {account_id!r} is not a paper (DU*) account")
    rejected = _select_rejected(session, account_id=account_id, run_date=run_date)
    summary = ClearSummary(
        count=len(rejected),
        rows=[
            (i.recommendation_id, i.symbol, float(i.requested_quantity))
            for i in rejected
        ],
    )
    if not apply:
        return summary
    if confirm != str(summary.count):
        raise ClearRefusedError(
            f"exact confirmation required: expected '{summary.count}', got {confirm!r}"
        )
    for intent in rejected:
        session.delete(intent)
    session.commit()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete a day's RISK_REJECTED buy intents (paper re-validation)."
    )
    parser.add_argument("--account-id", default="DUN551088")
    parser.add_argument("--date", default=None,
                        help="Run date (YYYY-MM-DD); defaults to today.")
    parser.add_argument("--apply", action="store_true",
                        help="Delete the rows (default: dry-run report only).")
    args = parser.parse_args(argv)
    # Must match run_paper.py's local date.today() basis for recommendation_ids.
    run_date = date.fromisoformat(args.date) if args.date else date.today()  # noqa: DTZ011

    config = load_config("config/default.yaml")
    engine = create_engine(config.database.url)
    with Session(engine) as session:
        summary = clear_rejected_intents(
            session, account_id=args.account_id, run_date=run_date,
            apply=False, confirm=None,
        )
        print(f"RISK_REJECTED buy intents for {args.account_id} on {run_date}: "
              f"{summary.count}")
        for rec_id, symbol, qty in summary.rows:
            print(f"  {symbol:8} qty={qty:<12} {rec_id}")
        if not args.apply:
            print("\nDry-run only. Re-run with --apply to delete these rows.")
            return 0
        if summary.count == 0:
            print("Nothing to clear.")
            return 0
        if not sys.stdin.isatty():
            raise ClearRefusedError("--apply requires an interactive TTY")
        answer = input(f"\nType {summary.count} to permanently delete these rows: ")
        clear_rejected_intents(
            session, account_id=args.account_id, run_date=run_date,
            apply=True, confirm=answer.strip(),
        )
        print(f"Deleted {summary.count} RISK_REJECTED intents.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
