"""Mark a recorded critical alert as resolved.

Gate 5 of the go-live checklist counts *unresolved* criticals, so an alert has
to be closable — otherwise the first genuine critical of an epoch blocks
promotion forever and the honest response becomes deleting rows by hand.

Resolution is deliberately a named human act: nothing here decides an incident
is over, it only records that an operator did. Already-resolved alerts are
refused rather than overwritten, so the trail says who called it first.

Usage::

    python -m scripts.ops.resolve_alert --list
    python -m scripts.ops.resolve_alert --id 41 --resolved-by huiliang
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from shared.models.alerts import AlertRecord


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.ops.resolve_alert",
        description="List or resolve critical alerts recorded from stream:alerts.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Defaults to config/default.yaml (ALGO_DATABASE_URL overrides it).",
    )
    parser.add_argument(
        "--list", action="store_true", help="Show unresolved critical alerts."
    )
    parser.add_argument("--id", type=int, help="alert_records.id to resolve.")
    parser.add_argument(
        "--resolved-by", help="Who is closing it — recorded on the row."
    )
    args = parser.parse_args(argv)

    if not args.list and (args.id is None or not args.resolved_by):
        parser.error("either --list, or both --id and --resolved-by")

    database_url = args.database_url
    if database_url is None:
        from shared.config import load_config

        database_url = load_config("config/default.yaml").database.url

    with sessionmaker(bind=create_engine(database_url))() as session:
        if args.list:
            rows = session.scalars(
                select(AlertRecord)
                .where(
                    AlertRecord.priority == "critical",
                    AlertRecord.resolved_at.is_(None),
                )
                .order_by(AlertRecord.raised_at)
            ).all()
            if not rows:
                print("No unresolved critical alerts.")
                return 0
            for row in rows:
                print(
                    f"{row.id}\t{row.raised_at.isoformat()}\t{row.event_type}\t"
                    f"{row.message}"
                )
            return 0

        record = session.get(AlertRecord, args.id)
        if record is None:
            print(f"No alert_records row with id {args.id}.", file=sys.stderr)
            return 1
        if record.resolved_at is not None:
            print(
                f"Alert {args.id} was already resolved by "
                f"{record.resolved_by} at {record.resolved_at.isoformat()}.",
                file=sys.stderr,
            )
            return 1
        record.resolved_at = datetime.now(timezone.utc)
        record.resolved_by = args.resolved_by
        session.commit()
        print(f"Alert {args.id} resolved by {args.resolved_by}.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
