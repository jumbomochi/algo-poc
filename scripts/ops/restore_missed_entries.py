#!/usr/bin/env python3
"""Rebuild entry fills the executor went blind to, from an IB statement.

KAN-88. On 2026-09-18 the 04:15 SGT run placed orders 189-205 and IB filled
15 of them — roughly USD 25.6k of purchases. The book has no record of any
of it: no positions, no trades, no cash movement, and a NAV the capital
model still derives a budget from.

WHY THE BOOK NEVER SAW THEM
---------------------------
``ib_executor`` treated IB **Error 1101** (connectivity restored, DATA LOST)
the same as 1102 (data maintained). A 1100/1101 leaves the API socket open,
so ``connect()`` never re-runs and ``_reregister_open_trades()`` never
fires: the per-``Trade`` ``statusEvent``/``commissionReportEvent`` handlers
stay bound to Python objects IB has stopped pushing to. "Connected to IB"
appears exactly once in that container's whole lifetime. Hours later the
order manager repriced against that stale view and cancelled 189-205 as
"unfilled" — against orders IB had already filled. Fixed forward in PR #184;
that stops the next one and cannot reach back to these.

WHY THE ORDINARY RECOVERY PATHS CANNOT DO IT
--------------------------------------------
* **KAN-87's execution sweep** re-reads executions from IB, but
  ``reqExecutions`` serves only about the current trading day. That window
  shut on 2026-09-19: it now returns 0 rows for these, and
  ``reqCompletedOrders`` no longer lists the orders either.
* **``reconcile_paper.py --apply-plan``** has exactly one repair action,
  ``set_position_quantity``. The generated plan for this divergence is 0
  actions and 15 unresolved, all ``sleeve_mapping_required``, and
  ``apply_repair_plan`` refuses any plan carrying unresolved entries.
* **``restore_missed_exit.py``** restores missed EXITS. These are ENTRIES.

So the IB Account Management statement is the only surviving record, and
this tool is how it gets back into the book.

WHAT IT DOES NOT DECIDE
-----------------------
Almost nothing. ``services.execution.execution_sweep.plan_sweep`` already
encodes every rule: skip an execution already in ``execution_fills``, refuse
one with no ``order_intents`` row, defer one whose commission cannot be
expressed in USD, un-expire an intent terminalized on absence, and build a
``FillMessage`` the projector will accept. This module supplies the
executions from a file instead of from IB, stamps them
``RECOVERY_SOURCE_STATEMENT`` so they are never mistaken for observed
evidence, and wraps the whole thing in the dry-run-and-confirm discipline
``restore_missed_exit.py`` established.

It never invents a value. Every economic figure comes from the statement,
and anything it cannot read is a named refusal rather than a default — a
wrong price is a wrong cost basis on the gate record forever.

WHAT IT DOES NOT REPAIR
-----------------------
``equity_snapshots``. Those rows are *recorded*, one per day from the cash
and market value as they stood that morning, and for the affected span they
overstate cash and understate market value by the recovered cost basis.
Correcting them would need per-position daily marks that were never stored
— the same limitation ``backfill_restored_equity.py`` documents. So the
applied artifact dates and explains the discontinuity instead of painting
over it.

Usage (dry run first, always):

    python scripts/ops/restore_missed_entries.py \\
        --statement ~/Downloads/DUN551088_trades_20260918.csv \\
        --account DUN551088
    python scripts/ops/restore_missed_entries.py ... --apply
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.run_paper import STATE_TABLES  # noqa: E402
from services.execution.execution_sweep import (  # noqa: E402
    RECOVERY_SOURCE_STATEMENT,
    SweepOutcome,
    SweptExecution,
    plan_sweep,
)
from services.portfolio_accounting.projector import FillProjector  # noqa: E402
from shared.config import load_config  # noqa: E402
from shared.models import ExecutionFill, OrderIntent  # noqa: E402
from shared.order_ledger import OrderLedger  # noqa: E402


class StatementRefusedError(RuntimeError):
    """The statement cannot be read safely. Every one of these is deliberate."""


#: IB Flex Query "Trades" field names, all required. Deliberately exact
#: rather than best-effort: a statement exported with a different field set
#: is a statement the operator must re-export, not one to guess at.
REQUIRED_COLUMNS = frozenset({
    "ClientAccountID",
    "TradeID",
    "IBOrderID",
    "ConID",
    "Symbol",
    "Exchange",
    "CurrencyPrimary",
    "Buy/Sell",
    "Quantity",
    "TradePrice",
    "IBCommission",
    "IBCommissionCurrency",
    "DateTime",
})

_SIDE = {"BUY": "buy", "SELL": "sell"}

#: ``DateTime`` is read as UTC. Flex can export in the account's local time,
#: and a silent 4-hour shift would file a fill against the wrong session, so
#: the runbook tells the operator to export in UTC and this never guesses a
#: zone.
_DATETIME_FORMATS = (
    "%Y%m%d;%H%M%S",
    "%Y%m%d;%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d, %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


def _text(row: Mapping[str, str], column: str, index: int) -> str:
    value = (row.get(column) or "").strip()
    if not value:
        raise StatementRefusedError(
            f"row {index} has no {column}. It comes from the IB statement "
            "and is never defaulted."
        )
    return value


def _number(row: Mapping[str, str], column: str, index: int) -> float:
    raw = _text(row, column, index)
    try:
        value = float(raw.replace(",", ""))
    except ValueError as exc:
        raise StatementRefusedError(
            f"row {index} has an unreadable {column}: {raw!r}"
        ) from exc
    if not isfinite(value):
        raise StatementRefusedError(
            f"row {index} has a non-finite {column}: {raw!r}"
        )
    return value


def _positive(row: Mapping[str, str], column: str, index: int) -> float:
    value = _number(row, column, index)
    if value <= 0:
        raise StatementRefusedError(
            f"row {index} has a {column} of {value}, which is not usable. "
            "A zero or negative one here means the export is wrong; it is "
            "never absorbed, because the resulting cost basis would be "
            "wrong on the gate record forever."
        )
    return value


def _moment(row: Mapping[str, str], index: int) -> datetime:
    raw = _text(row, "DateTime", index)
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise StatementRefusedError(
        f"row {index} has an unreadable DateTime: {raw!r}. Expected one of "
        + ", ".join(repr(fmt) for fmt in _DATETIME_FORMATS)
        + ", in UTC."
    )


def parse_statement(
    rows: Iterable[Mapping[str, str]], *, account_id: str | None = None
) -> list[SweptExecution]:
    """Turn IB statement rows into the executions ``plan_sweep`` consumes.

    ``account_id``, when given, is the account the repair is for: a row for
    any other one is refused rather than skipped, because a statement
    covering two accounts must never leak one into the other.
    """
    parsed: list[SweptExecution] = []
    seen: set[str] = set()

    for index, row in enumerate(rows, start=1):
        missing = sorted(REQUIRED_COLUMNS - set(row))
        if missing:
            raise StatementRefusedError(
                f"row {index} is missing required column(s): "
                f"{', '.join(missing)}. Re-export the statement with the "
                "full Flex 'Trades' field set rather than filling them in "
                "by hand."
            )

        account = _text(row, "ClientAccountID", index)
        if not account.startswith("DU"):
            raise StatementRefusedError(
                f"row {index} is for account {account!r}, which is not an IB "
                "paper account. A live ledger is never reconstructed by a "
                "script."
            )
        if account_id is not None and account != account_id:
            raise StatementRefusedError(
                f"row {index} is for account {account!r}, but this repair is "
                f"for {account_id!r}. Export one account at a time."
            )

        execution_id = _text(row, "TradeID", index)
        if execution_id in seen:
            raise StatementRefusedError(
                f"row {index} repeats TradeID {execution_id!r}. That is the "
                "idempotency key: a duplicate would be recognised as already "
                "recorded and dropped in silence, losing a real fill."
            )
        seen.add(execution_id)

        side_raw = _text(row, "Buy/Sell", index).upper()
        if side_raw not in _SIDE:
            raise StatementRefusedError(
                f"row {index} has an unrecognised Buy/Sell: {side_raw!r}. "
                f"Expected one of {', '.join(sorted(_SIDE))}."
            )

        parsed.append(SweptExecution(
            execution_id=execution_id,
            account_id=account,
            ib_order_id=_text(row, "IBOrderID", index),
            con_id=int(_positive(row, "ConID", index)),
            ticker=_text(row, "Symbol", index),
            exchange=_text(row, "Exchange", index),
            currency=_text(row, "CurrencyPrimary", index),
            side=_SIDE[side_raw],
            quantity=_positive(row, "Quantity", index),
            # Replaced below, once the whole file is in hand.
            cumulative_quantity=0.0,
            price=_positive(row, "TradePrice", index),
            # IB reports a commission CHARGE as negative, and
            # FillProjector._validate rejects a negative commission outright,
            # so a verbatim copy would refuse every real row. Stored as a
            # magnitude, matching what the live callback records.
            commission=abs(_number(row, "IBCommission", index)),
            commission_currency=_text(row, "IBCommissionCurrency", index),
            executed_at=_moment(row, index),
        ))

    if not parsed:
        raise StatementRefusedError(
            "the statement contains no rows. An empty export reports "
            "'nothing to recover', which looks exactly like a healthy book — "
            "so it is refused instead."
        )
    return _with_cumulative(parsed)


def _with_cumulative(
    executions: Sequence[SweptExecution],
) -> list[SweptExecution]:
    """Assign each execution its order's running total, in time order.

    ``cumulative_quantity`` is what the projector advances the intent on, so
    a per-row copy of ``quantity`` would terminalize a 2-of-5 fill as
    complete and release the rest of the reservation. Rows arrive in
    whatever order the export produced, hence the sort: accumulating in file
    order would hand the later fill the smaller running total.

    The returned order is the sorted one, so a caller projecting in sequence
    never advances an intent past a fill it has not applied yet.
    """
    ordered = sorted(executions, key=lambda e: (e.executed_at, e.execution_id))
    running: dict[str, float] = {}
    result = []
    for execution in ordered:
        total = running.get(execution.ib_order_id, 0.0) + execution.quantity
        running[execution.ib_order_id] = total
        result.append(
            SweptExecution(**{**execution.__dict__, "cumulative_quantity": total})
        )
    return result


def load_statement(
    path: Path | str, *, account_id: str | None = None
) -> list[SweptExecution]:
    """Read an IB statement CSV off disk."""
    path = Path(path)
    if not path.is_file():
        raise StatementRefusedError(f"no statement file at {path}")
    with path.open(newline="") as handle:
        return parse_statement(csv.DictReader(handle), account_id=account_id)


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------

#: Typed by the operator, in full, to apply. Same shape as the repair tool's
#: "APPLY PAPER REPAIR" -- a confirmation you can produce by accident is not
#: one.
CONFIRMATION = "RESTORE MISSED ENTRIES"


class RecoveryRefusedError(RuntimeError):
    """The recovery cannot be applied safely.

    Kept distinct from :class:`StatementRefusedError` so an unreadable file
    and a bad confirmation are never confused in a traceback.
    """


def apply_recovery(
    session: Session, outcome: SweepOutcome, *, confirm: str
) -> list[str]:
    """Project every recovered fill. Returns the execution ids written.

    One transaction per fill, because ``FillProjector.apply`` owns its own
    and refuses a session carrying pending work. A failure partway therefore
    leaves the fills before it COMMITTED -- which is right: a purchase that
    really happened must never be rolled back to tidy up a later row's
    failure. The caller reports how far it got, and a re-run resumes
    (``already_recorded`` skips what landed).
    """
    if confirm != CONFIRMATION:
        raise RecoveryRefusedError(
            f"exact confirmation required: expected {CONFIRMATION!r}"
        )
    # `plan_sweep` un-expired the intents whose EXPIRED was written on
    # absence (AC4), and `restore_absent_terminalization` only FLUSHES.
    # `FillProjector.apply` opens with `_end_read_only_autobegin`, which
    # ROLLS BACK a clean in-transaction session -- so without this commit the
    # correction is silently undone and the fill lands against an intent that
    # is still EXPIRED. The projector accepts that (EXPIRED is in its
    # fillable set) and `_advance_intent` returns early on a terminal status,
    # leaving the intent terminal-and-wrong forever. Committing here rather
    # than asking callers to remember it: this failure is invisible at the
    # call site and permanent in the book.
    session.commit()
    projector = FillProjector(session)
    applied: list[str] = []
    for fill in outcome.recovered:
        if projector.apply(fill):
            applied.append(fill.execution_id)
    return applied


def _render(outcome: SweepOutcome, executions: Sequence[SweptExecution]) -> str:
    by_id = {execution.execution_id: execution for execution in executions}
    lines = []
    cost = 0.0
    for fill in outcome.recovered:
        execution = by_id[fill.execution_id]
        notional = execution.quantity * execution.price
        cost += notional + execution.commission
        lines.append(
            f"  RECOVER  {execution.ticker:<6} order {execution.ib_order_id:<5} "
            f"{execution.quantity:>10,.4f} @ {execution.price:>10,.4f}  "
            f"commission {execution.commission:>7,.2f}  "
            f"= {notional:>12,.2f}  ({execution.execution_id})"
        )
    for order_id in outcome.untracked:
        lines.append(
            f"  UNTRACKED order {order_id} -- no order_intents row the book "
            "can attribute this to, or an intent terminal for a reason the "
            "broker record does not refute. NOT projected."
        )
    for execution_id in outcome.deferred:
        lines.append(
            f"  DEFERRED {execution_id} -- the commission cannot be expressed "
            "in USD, and the projector would reject the message permanently. "
            "Left entirely alone so it can be decided again."
        )
    lines.append("")
    lines.append(
        f"  {len(outcome.recovered)} to recover, "
        f"{outcome.already_recorded} already recorded, "
        f"{len(outcome.untracked)} untracked, "
        f"{len(outcome.deferred)} deferred"
    )
    lines.append(f"  cost basis to be restored: {cost:,.2f}")
    return "\n".join(lines)


#: What a rollback of THIS repair needs. ``dump_paper_state`` covers
#: equity_snapshots, trades, positions and portfolio_config — the four
#: tables ``--reset`` wipes — but this repair also writes ``order_intents``
#: (the AC4 correction) and ``execution_fills`` (the immutable audit row),
#: and a dump without them cannot put either back. They are added here
#: rather than to ``STATE_TABLES`` because that tuple also drives
#: ``reset_paper_state``, which would then start DELETING the ledger.
_DUMP_TABLES = (*STATE_TABLES, OrderIntent, ExecutionFill)


def _dump_pre_repair(engine, out_path: Path) -> Path:
    """Serialize the book as it stands, through a session of its own.

    The session that planned the repair is carrying the un-expiring
    ``plan_sweep`` performed (AC4), unflushed. Dumping through it would
    autoflush and record those intents SUBMITTED — so restoring from the
    "pre-repair" dump would leave them non-terminal, holding reservations
    nothing will ever release. A fresh session sees committed state only,
    which is what "before the repair" means.
    """
    payload = {}
    with Session(engine) as reader:
        for model in _DUMP_TABLES:
            columns = [column.name for column in model.__table__.columns]
            payload[model.__table__.name] = [
                {column: getattr(row, column) for column in columns}
                for row in reader.query(model).all()
            ]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    return out_path


def _artifact(
    *,
    statement: Path,
    outcome: SweepOutcome,
    executions: Sequence[SweptExecution],
    applied: Sequence[str],
    stamp: str,
) -> dict:
    by_id = {execution.execution_id: execution for execution in executions}
    dates = sorted({by_id[e].executed_at.date().isoformat() for e in applied})
    return {
        "repair": "KAN-88 restore_missed_entries",
        "applied_at": stamp,
        "statement": str(statement),
        "statement_sha256": sha256(statement.read_bytes()).hexdigest(),
        "recovery_source": RECOVERY_SOURCE_STATEMENT,
        "execution_dates": dates,
        "applied": [
            {
                "execution_id": execution_id,
                "ib_order_id": by_id[execution_id].ib_order_id,
                "ticker": by_id[execution_id].ticker,
                "con_id": by_id[execution_id].con_id,
                "side": by_id[execution_id].side,
                "quantity": by_id[execution_id].quantity,
                "price": by_id[execution_id].price,
                "commission": by_id[execution_id].commission,
                "commission_currency": by_id[execution_id].commission_currency,
                "executed_at": by_id[execution_id].executed_at.isoformat(),
            }
            for execution_id in applied
        ],
        "untracked": list(outcome.untracked),
        "deferred": list(outcome.deferred),
        # AC3's "explicitly dated and explained" branch. The ledger is
        # corrected; the recorded series is not, and saying so here is the
        # correction.
        "equity_series_note": (
            "equity_snapshots rows dated from "
            f"{dates[0] if dates else 'the execution date'} to {stamp[:10]} "
            "overstate cash and understate market value by the cost basis "
            "restored above, because the book did not know it held these "
            "positions. Those rows are NOT rewritten: correcting them needs "
            "per-position daily marks that were never stored (the same "
            "limitation scripts/ops/backfill_restored_equity.py documents). "
            "The series is correct from the next snapshot after applied_at; "
            "the step between is this repair, not a trade."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild entry fills from an IB Account Management statement, "
            "for executions reqExecutions can no longer serve."
        )
    )
    parser.add_argument(
        "--statement", required=True,
        help="IB Flex Query 'Trades' CSV, exported with DateTime in UTC",
    )
    parser.add_argument(
        "--account", default=None,
        help="the paper account this repair is for; any other is refused",
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument(
        "--artifact-dir", default="output/repairs",
        help="where the pre-repair state dump and the applied artifact land",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Write the recovery (default: dry-run report only).",
    )
    args = parser.parse_args(argv)

    statement = Path(args.statement)
    executions = load_statement(statement, account_id=args.account)
    url = args.database_url or load_config("config/default.yaml").database.url
    engine = create_engine(url)

    with Session(engine) as session:
        outcome = plan_sweep(
            executions,
            OrderLedger(session),
            recovery_source=RECOVERY_SOURCE_STATEMENT,
        )
        # NOT committed here. Planning un-expires intents as a side effect
        # (AC4), and on any path that does not go on to apply, that
        # correction must be discarded -- a report-only command that quietly
        # moves 15 intents out of a terminal state is a write, whatever it
        # prints. `apply_recovery` commits it when the operator confirms.
        print(f"\n{statement}\n")
        print(_render(outcome, executions))

        # A partial repair leaves the book half-right and reconciliation
        # still fail-closed, which is the state this whole exercise exists
        # to leave. The operator sees why first.
        if outcome.untracked or outcome.deferred:
            print(
                "\nRefusing to apply: the statement contains executions this "
                "cannot recover (listed above). Resolve them first -- a "
                "partial repair leaves entries fail-closed anyway."
            )
            session.rollback()
            return 1
        if not outcome.recovered:
            print("\nNothing to recover. The book already has these fills.")
            session.rollback()
            return 0
        if not args.apply:
            print("\nDry-run only. Re-run with --apply to write it.")
            session.rollback()
            return 0
        if not sys.stdin.isatty():
            session.rollback()
            raise RecoveryRefusedError("--apply requires an interactive TTY")

        artifact_dir = Path(args.artifact_dir)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # The rollback path, written BEFORE the prompt so it exists even if
        # the operator walks away at the confirmation.
        dump = _dump_pre_repair(
            engine, artifact_dir / f"paper_state_pre_entry_restore_{stamp}.json"
        )
        print(f"\nPre-repair state dumped to {dump}")

        answer = input(f"\nType {CONFIRMATION} to write these entries: ")
        applied = apply_recovery(session, outcome, confirm=answer.strip())

        artifact_path = artifact_dir / f"entry_restore_{stamp}.json"
        artifact_path.write_text(json.dumps(
            _artifact(
                statement=statement,
                outcome=outcome,
                executions=executions,
                applied=applied,
                stamp=datetime.now(timezone.utc).isoformat(),
            ),
            indent=2,
        ))
        print(f"Recovered {len(applied)} fill(s). Artifact: {artifact_path}")
        print(
            "\nNow run:  python scripts/reconcile_paper.py --report\n"
            "and confirm severity: ok / entries_allowed: true before the "
            "next 04:15."
        )
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
