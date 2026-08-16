#!/usr/bin/env python3
"""Render the authoritative part of the 04:52 daily digest.

KAN-30. The digest used to report order counts scraped out of the paper log,
which means it could report success while the pipeline never received an order
(KAN-31), and it did not report halt state at all — so the operator could be
halted and not told.

This module answers the three questions from authoritative tables:

* **halt** — ``system_halt`` via :class:`~shared.halt_state.HaltStateRepository`
* **fills** — rows in ``execution_fills``, i.e. real broker executions
* **rejections** — ``order_intents`` in ``RISK_REJECTED`` / ``SUBMISSION_FAILED``,
  kept apart so "risk said no" and "the broker said no" stay distinguishable

Pure rendering is split from collection so the message content is testable by
inserting rows, not by grepping the shell script. Delivery belongs to the
launchd wrapper (``deploy/launchd/run_pipeline_report.sh``) via the shared
``telegram()`` helper; this script only ever prints.

A failure exits nonzero and prints nothing, so the wrapper can substitute its
"unknown" marker. Printing a reassuring "halt: clear" we could not
substantiate would be worse than printing nothing.

Usage:
    python scripts/ops/pipeline_report_summary.py \\
        --since 2026-08-16T00:00:00+0800 --mode paper
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

# Executed by path from the launchd wrapper, which puts `scripts/ops/` — not
# the repo root — on sys.path. Pin the repo root explicitly so the imports
# resolve to THIS checkout rather than to whatever tree an editable install
# happens to point at.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.halt_state import HaltStateRepository  # noqa: E402
from shared.models.order_ledger import (  # noqa: E402
    ExecutionFill,
    OrderIntent,
    OrderStatus,
)

# Telegram rejects a body over 4096 chars with HTTP 400, and the wrapper's
# fire-and-forget send discards curl's status — so an over-long message is
# silently dropped. `reason` is a String(500) all by itself.
MAX_REASON_CHARS = 120

# A bad DSN surfaces verbatim in SQLAlchemy's ArgumentError, and the DSN
# carries the live Postgres password. Runs to the LAST '@' on purpose: the
# password may itself contain '@' or whitespace, and over-redacting is the
# safe direction. Deliberately a second copy of the pattern in
# scripts/ops/divergence_alert.py rather than an import — that module pulls in
# the backtest package, which this one has no business loading — so each copy
# carries its own test.
_DSN_CREDENTIAL = re.compile(r"(?P<prefix>[a-zA-Z][\w+.-]*://[^\s/@]*:).*@")


def _redact(text: str) -> str:
    return _DSN_CREDENTIAL.sub(r"\g<prefix>***@", text)


@dataclass(frozen=True)
class RunFacts:
    """What the overnight run actually did, per the database."""

    halt_active: bool
    halt_source: str | None
    halt_reason: str | None
    fills: int
    risk_rejected: int
    submission_failed: int


def collect_facts(session: Session, *, since: datetime, mode: str) -> RunFacts:
    """Read the run's facts from the ledger tables.

    ``since`` bounds fills by ``executed_at`` and rejections by ``created_at``
    — an intent is created and rejected within the same run, so its creation
    time is what attributes it to this morning rather than yesterday's.

    ``mode`` scopes the halt and the intents. ``execution_fills`` has no mode
    column; in this deployment exactly one book writes fills, and a wrong
    filter would under-report, so fills are counted unscoped.
    """
    halt = HaltStateRepository(session).load_active_halt(mode=mode)

    fills = session.scalar(
        select(func.count())
        .select_from(ExecutionFill)
        .where(ExecutionFill.executed_at >= since)
    ) or 0

    rejected = dict(
        session.execute(
            select(OrderIntent.status, func.count())
            .where(
                OrderIntent.mode == mode,
                OrderIntent.created_at >= since,
                OrderIntent.status.in_(
                    [OrderStatus.RISK_REJECTED, OrderStatus.SUBMISSION_FAILED]
                ),
            )
            .group_by(OrderIntent.status)
        ).all()
    )

    return RunFacts(
        halt_active=halt is not None,
        halt_source=halt.source if halt else None,
        halt_reason=halt.reason if halt else None,
        fills=int(fills),
        risk_rejected=int(rejected.get(OrderStatus.RISK_REJECTED, 0)),
        submission_failed=int(rejected.get(OrderStatus.SUBMISSION_FAILED, 0)),
    )


def render_summary(facts: RunFacts) -> str:
    """One line: halt state first, then fills, then rejections by status.

    Halt leads because it is the fact an operator must not miss, and anything
    at the end of a long line is what gets missed.
    """
    if facts.halt_active:
        reason = (facts.halt_reason or "no reason recorded").strip()
        if len(reason) > MAX_REASON_CHARS:
            reason = reason[: MAX_REASON_CHARS - 1] + "…"
        halt = f"🛑 HALT ({facts.halt_source or 'unknown'}): {reason}"
    else:
        halt = "halt: clear"

    return (
        f"{halt} · fills:{facts.fills}"
        f" · rejected: risk {facts.risk_rejected}"
        f" / broker {facts.submission_failed}"
    )


def _local_midnight() -> datetime:
    now = datetime.now().astimezone()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the daily digest summary.")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("ALGO_DATABASE_URL"),
        help="defaults to $ALGO_DATABASE_URL",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="ISO-8601 lower bound for fills/rejections; defaults to local midnight",
    )
    parser.add_argument("--mode", default=os.environ.get("ALGO_MODE", "paper"))
    args = parser.parse_args(argv)

    if not args.database_url:
        parser.error("no --database-url and no $ALGO_DATABASE_URL")
    since = datetime.fromisoformat(args.since) if args.since else _local_midnight()

    try:
        engine = create_engine(args.database_url)
        try:
            with Session(engine) as session:
                facts = collect_facts(session, since=since, mode=args.mode)
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 — the wrapper needs a clean exit code
        # One redacted line, not a traceback: the DSN carries the live Postgres
        # password and this goes to the report log verbatim.
        print(f"{_redact(type(exc).__name__)}: {_redact(str(exc))}", file=sys.stderr)
        return 1

    print(render_summary(facts))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
