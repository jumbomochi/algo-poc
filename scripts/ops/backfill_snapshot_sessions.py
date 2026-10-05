#!/usr/bin/env python3
"""Fill ``equity_snapshots.session_date`` on history (KAN-103).

``equity_snapshots.date`` is the SGT run date: the row dated Tuesday holds
Monday's US close. From KAN-103 ``run_paper.py`` stamps ``session_date``, the
US session whose closes marked the book, from the bars it actually priced.
Rows written before that carry NULL, and their bars were never recorded, so
this tool fills them from what each row DID record, with the rule in
``shared/session_dating.py::infer_session``:

* the last NYSE session closed by ``valuation_at`` (KAN-44 rows onward);
* otherwise the last NYSE session strictly before the SGT run date.

docs/decisions/divergence-session-dating-2026-10.md shows the two rules agree
on every recorded row that carries both. Where they do not, or where the row
was written while a US session was trading (its bars may be partial), the row
is left NULL and reported, because a guessed session is worse than an absent
one: a NULL reads as NO_DATA, a wrong date grades the wrong day.

WHAT IT WILL AND WILL NOT DO
---------------------------
* Dry-run by default: prints the run-date -> session mapping, the sessions
  valued more than once (a Tuesday after a US Monday holiday, a weekend
  catch-up — legitimate, readers take the latest row), every row it cannot
  resolve and why, and every already-stamped row that disagrees with the
  rule. On Postgres the dry-run runs in a READ ONLY transaction, so it cannot
  write even by mistake. It works before the migration too: it then reads
  only the existing columns and says that ``--apply`` needs
  ``alembic upgrade head`` first.
* ``--apply`` sets ``session_date`` only ``WHERE session_date IS NULL``, in one
  transaction, after an exact typed confirmation on an interactive TTY. It
  never changes a stamped row, never touches ``date`` or any value column, and
  never deletes. Re-running it is a no-op. It writes a JSON audit record of
  every row it filled.

The operator runs ``--apply``. Agents run the dry-run at most.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, inspect, select, text, update
from sqlalchemy.orm import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.artifact_dir import durable_artifact_dir  # noqa: E402
from shared.config import load_config  # noqa: E402
from shared.models.equity_snapshot import EquitySnapshot  # noqa: E402
from shared.session_close import session_in_progress  # noqa: E402
from shared.session_dating import (  # noqa: E402
    as_utc,
    infer_session,
    last_closed_session,
    last_session_before,
)

CONFIRMATION = "BACKFILL SNAPSHOT SESSIONS"

DEFAULT_ARTIFACT_DIR = _REPO_ROOT / "output" / "reconciliation"

FILL = "fill"
STAMPED = "stamped"
DISAGREES = "stamped_disagrees"
UNRESOLVED = "unresolved"


class BackfillRefusedError(RuntimeError):
    """The backfill cannot be made safely."""


@dataclass(frozen=True)
class RowPlan:
    id: int
    portfolio: str
    run_date: date
    equity: float
    valuation_at: datetime | None
    created_at: datetime
    current: date | None
    proposed: date | None
    rule: str | None
    status: str
    reason: str = ""


@dataclass(frozen=True)
class BackfillPlan:
    rows: tuple[RowPlan, ...]
    column_present: bool

    def with_status(self, status: str) -> tuple[RowPlan, ...]:
        return tuple(row for row in self.rows if row.status == status)

    @property
    def to_fill(self) -> tuple[RowPlan, ...]:
        return self.with_status(FILL)

    @property
    def unresolved(self) -> tuple[RowPlan, ...]:
        return self.with_status(UNRESOLVED)

    @property
    def disagreements(self) -> tuple[RowPlan, ...]:
        return self.with_status(DISAGREES)

    def duplicates(self) -> dict[tuple[date, tuple[date, ...]], list[RowPlan]]:
        """Sessions valued by more than one run date, grouped across sleeves.

        Keyed by (session, the run dates that valued it); the value is every
        row in those groups, so a caller can tell whether the values agree.
        """
        by_key: dict[tuple[str, date], list[RowPlan]] = defaultdict(list)
        for row in self.rows:
            session = row.current or row.proposed
            if session is not None:
                by_key[(row.portfolio, session)].append(row)
        grouped: dict[tuple[date, tuple[date, ...]], list[RowPlan]] = (
            defaultdict(list)
        )
        for (_portfolio, session), rows in by_key.items():
            if len(rows) > 1:
                run_dates = tuple(sorted(r.run_date for r in rows))
                grouped[(session, run_dates)].extend(rows)
        return dict(sorted(grouped.items()))


def has_session_column(session: Session) -> bool:
    columns = inspect(session.connection()).get_columns("equity_snapshots")
    return any(column["name"] == "session_date" for column in columns)


def _resolve(
    run_date: date, valuation_at: datetime | None, created_at: datetime
) -> tuple[date | None, str | None, str]:
    """(session, rule, reason-if-unresolved) for one recorded row."""
    instant = as_utc(valuation_at if valuation_at is not None else created_at)
    # KAN-104's notion of "in progress": opened, and not yet past its close
    # plus the settle margin. One definition for the guard and the backfill.
    trading = session_in_progress(instant)
    if trading is not None:
        which = "valuation_at" if valuation_at is not None else "created_at"
        return None, None, (
            f"{which} {instant.isoformat()} falls inside the "
            f"{trading.session} session; its bars may be partial"
        )
    by_run_date = last_session_before(run_date)
    if valuation_at is not None:
        by_valuation = last_closed_session(valuation_at)
        if by_valuation != by_run_date:
            return None, None, (
                f"rules disagree: valuation_at says {by_valuation}, the run "
                f"date says {by_run_date}"
            )
        rule = "valuation_at"
    else:
        by_written = last_closed_session(created_at)
        if by_written != by_run_date:
            return None, None, (
                f"rules disagree: created_at says {by_written}, the run date "
                f"says {by_run_date}"
            )
        rule = "run_date"
    session = infer_session(run_date, valuation_at)
    if session is None:
        return None, None, "no NYSE session found before the run"
    return session, rule, ""


def plan_backfill(session: Session) -> BackfillPlan:
    """Work out every row's session. Issues SELECTs only."""
    column_present = has_session_column(session)
    columns = [
        EquitySnapshot.id,
        EquitySnapshot.portfolio,
        EquitySnapshot.date,
        EquitySnapshot.equity,
        EquitySnapshot.valuation_at,
        EquitySnapshot.created_at,
    ]
    if column_present:
        columns.append(EquitySnapshot.session_date)
    records = session.execute(
        select(*columns).order_by(EquitySnapshot.date, EquitySnapshot.portfolio)
    ).all()

    rows: list[RowPlan] = []
    for record in records:
        current = record.session_date if column_present else None
        proposed, rule, reason = _resolve(
            record.date, record.valuation_at, record.created_at
        )
        if current is not None:
            if proposed is not None and proposed != current:
                status = DISAGREES
                reason = f"stamped {current}, the rule says {proposed}"
            else:
                status = STAMPED
        elif proposed is None:
            status = UNRESOLVED
        else:
            status = FILL
        rows.append(RowPlan(
            id=record.id,
            portfolio=record.portfolio,
            run_date=record.date,
            equity=float(record.equity),
            valuation_at=record.valuation_at,
            created_at=record.created_at,
            current=current,
            proposed=proposed,
            rule=rule,
            status=status,
            reason=reason,
        ))
    return BackfillPlan(rows=tuple(rows), column_present=column_present)


def apply_backfill(
    session: Session,
    plan: BackfillPlan,
    *,
    confirm: str,
    artifact_dir: Path | str = DEFAULT_ARTIFACT_DIR,
) -> tuple[int, Path | None]:
    """Fill the NULLs the plan resolved. Returns (rows filled, audit path)."""
    if confirm != CONFIRMATION:
        raise BackfillRefusedError(
            f"exact confirmation required: expected {CONFIRMATION!r}"
        )
    if not plan.column_present or not has_session_column(session):
        raise BackfillRefusedError(
            "equity_snapshots.session_date does not exist: run "
            "`alembic upgrade head` first"
        )

    table = EquitySnapshot.__table__
    filled: list[RowPlan] = []
    for row in plan.to_fill:
        result = session.execute(
            update(table)
            .where(table.c.id == row.id, table.c.session_date.is_(None))
            .values(session_date=row.proposed)
        )
        if result.rowcount:
            filled.append(row)
    session.commit()

    if not filled:
        return 0, None
    # Written after the commit so it can never claim a change that did not land.
    directory = Path(artifact_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"snapshot-session-backfill-{stamp}.json"
    path.write_text(json.dumps({
        "written_at": datetime.now(timezone.utc).isoformat(),
        "reason": (
            "KAN-103: equity_snapshots.date is the SGT run date; session_date "
            "records the US session each row valued. Filled only where NULL, "
            "by shared/session_dating.py::infer_session."
        ),
        "rows": [
            {
                "id": row.id,
                "portfolio": row.portfolio,
                "date": row.run_date.isoformat(),
                "session_date": row.proposed.isoformat(),
                "rule": row.rule,
            }
            for row in filled
        ],
        "unresolved": [
            {
                "id": row.id,
                "portfolio": row.portfolio,
                "date": row.run_date.isoformat(),
                "reason": row.reason,
            }
            for row in plan.unresolved
        ],
    }, indent=2) + "\n")
    return len(filled), path


def _day(d: date | None) -> str:
    return "-" if d is None else f"{d.isoformat()} {d:%a}"


def render(plan: BackfillPlan) -> str:
    lines: list[str] = []
    counts = {
        status: len(plan.with_status(status))
        for status in (FILL, STAMPED, DISAGREES, UNRESOLVED)
    }
    lines.append(
        f"equity_snapshots: {len(plan.rows)} rows — {counts[FILL]} to fill, "
        f"{counts[STAMPED]} already stamped, {counts[DISAGREES]} stamped but "
        f"disagreeing, {counts[UNRESOLVED]} unresolved"
    )
    if not plan.column_present:
        lines.append(
            "  session_date column NOT present: this is the plan only. "
            "--apply needs `alembic upgrade head` first."
        )

    lines.append("")
    lines.append("Run date -> session valued")
    lines.append(f"  {'run date (SGT)':18}{'session (US)':18}{'rule':14}rows")
    mapping: dict[tuple[date, date | None, str | None, str], int] = defaultdict(int)
    for row in plan.rows:
        session = row.current or row.proposed
        mapping[(row.run_date, session, row.rule, row.status)] += 1
    for (run_date, session, rule, status), n in sorted(
        mapping.items(), key=lambda item: (item[0][0], str(item[0][1]))
    ):
        note = "" if status == FILL else f"  [{status}]"
        lines.append(
            f"  {_day(run_date):18}{_day(session):18}{rule or '-':14}{n}{note}"
        )

    duplicates = plan.duplicates()
    lines.append("")
    lines.append(f"Sessions valued by more than one run ({len(duplicates)})")
    for (session, run_dates), rows in duplicates.items():
        by_portfolio: dict[str, set[float]] = defaultdict(set)
        for row in rows:
            by_portfolio[row.portfolio].add(round(row.equity, 2))
        identical = all(len(v) == 1 for v in by_portfolio.values())
        lines.append(
            f"  {_day(session)} <- "
            + ", ".join(_day(d) for d in run_dates)
            + f"  ({len(by_portfolio)} portfolios, values "
            + ("identical" if identical else "DIFFER")
            + "; readers take the latest row)"
        )

    for title, rows in (
        ("Unresolved rows (left NULL)", plan.unresolved),
        ("Stamped rows that disagree with the rule (never changed)",
         plan.disagreements),
    ):
        lines.append("")
        lines.append(f"{title} ({len(rows)})")
        for row in rows:
            lines.append(
                f"  id={row.id} {row.portfolio} {row.run_date}: {row.reason}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fill equity_snapshots.session_date on history (KAN-103)."
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument(
        "--artifact-dir", default=None,
        help="where the audit record goes; defaults beside the repair plans "
             "(relocated out of a linked git worktree).",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Fill the NULLs (default: dry-run plan only).",
    )
    args = parser.parse_args(argv)

    url = args.database_url or load_config("config/default.yaml").database.url
    engine = create_engine(url)
    with Session(engine) as session:
        if not args.apply and engine.dialect.name == "postgresql":
            # Must be the transaction's first statement. Any write after it,
            # by this tool or by a bug in it, is refused by the server.
            session.execute(text("SET TRANSACTION READ ONLY"))
        plan = plan_backfill(session)
        print(render(plan))
        if not args.apply:
            session.rollback()
            print("\nDry-run only. Re-run with --apply to fill the NULLs.")
            return 0
        if not plan.column_present:
            raise BackfillRefusedError(
                "equity_snapshots.session_date does not exist: run "
                "`alembic upgrade head` first"
            )
        if not plan.to_fill:
            print("\nNothing to fill.")
            return 0
        if not sys.stdin.isatty():
            raise BackfillRefusedError("--apply requires an interactive TTY")
        answer = input(
            f"\nType {CONFIRMATION} to fill {len(plan.to_fill)} rows: "
        )
        artifact_dir = durable_artifact_dir(
            args.artifact_dir or DEFAULT_ARTIFACT_DIR,
            explicit=args.artifact_dir is not None,
        )
        count, path = apply_backfill(
            session, plan, confirm=answer.strip(), artifact_dir=artifact_dir
        )
        print(f"Filled {count} rows. Audit artifact: {path}")
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
