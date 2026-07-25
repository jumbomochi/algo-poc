from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from shared.config import load_config
from shared.models import Position


class RetireRefusedError(RuntimeError):
    """Raised when a safety guard blocks the retirement apply."""


@dataclass(frozen=True)
class RetireSummary:
    count: int
    rows: list[tuple[str, str, float]]


def _select_legacy(session: Session) -> list[Position]:
    return list(session.scalars(
        select(Position).where(
            Position.account_id.is_(None),
            Position.status == "open",
        )
    ))


def retire_legacy_positions(
    session: Session, *, apply: bool, confirm: str | None
) -> RetireSummary:
    """Close unowned (``account_id IS NULL``) open legacy positions.

    Dry-run by default. On apply, requires ``confirm == str(count)`` and closes
    the rows in a single committed transaction, leaving account_id/con_id intact
    as an audit trail. Owned positions are never selected or mutated.
    """
    legacy = _select_legacy(session)
    summary = RetireSummary(
        count=len(legacy),
        rows=[(p.ticker, p.portfolio, float(p.quantity)) for p in legacy],
    )
    if not apply:
        return summary
    if any(p.account_id is not None for p in legacy):  # defensive
        raise RetireRefusedError("selection unexpectedly includes an owned position")
    if confirm != str(summary.count):
        raise RetireRefusedError(
            f"exact confirmation required: expected '{summary.count}', got {confirm!r}"
        )
    now = datetime.now(UTC)
    for p in legacy:
        p.status = "closed"
        p.closed_at = now
    session.commit()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Retire unowned legacy paper positions (Path A re-baseline)."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Close the rows (default: dry-run report only).",
    )
    args = parser.parse_args(argv)

    config = load_config("config/default.yaml")  # applies ALGO_DATABASE_URL override
    engine = create_engine(config.database.url)
    with Session(engine) as session:
        summary = retire_legacy_positions(session, apply=False, confirm=None)
        print(f"Legacy unowned open positions: {summary.count}")
        for ticker, portfolio, qty in summary.rows:
            print(f"  {ticker:8} {portfolio:18} qty={qty}")
        if not args.apply:
            print("\nDry-run only. Re-run with --apply to close these rows.")
            return 0
        if summary.count == 0:
            print("Nothing to retire.")
            return 0
        if not sys.stdin.isatty():
            raise RetireRefusedError("--apply requires an interactive TTY")
        answer = input(f"\nType {summary.count} to permanently close these rows: ")
        retire_legacy_positions(session, apply=True, confirm=answer.strip())
        print(f"Retired {summary.count} legacy positions.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
