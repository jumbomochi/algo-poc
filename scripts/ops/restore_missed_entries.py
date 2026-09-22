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
from dataclasses import dataclass, replace
from datetime import datetime, time, timezone
from hashlib import sha256
from math import isfinite
from pathlib import Path

from sqlalchemy import create_engine, func, select
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
from services.portfolio_accounting.projector import (  # noqa: E402
    FillProjectionError,
    FillProjector,
)
from shared.config import load_config  # noqa: E402
from shared.models import (  # noqa: E402
    ExecutionFill,
    OrderIntent,
    PortfolioConfig,
    Position,
)
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

#: Canonical spelling of every required column, keyed by its casefolded
#: form. IB is not consistent with itself: the documented Flex field is
#: ``ConID``, the picker labels it "Conid", and the CSV the 2026-09-18
#: export actually produced writes ``Conid``. Matching a broker's
#: capitalisation is not a safety guard -- it is an accident, and it
#: refused a correct export. Nothing downstream is case-sensitive, so the
#: header is normalized once, here.
_CANONICAL_COLUMNS = {column.casefold(): column for column in REQUIRED_COLUMNS}

_SIDE = {"BUY": "buy", "SELL": "sell"}

#: Regular US trading hours in UTC, widened either side. Used only to FLAG a
#: statement that looks like it was exported in local time -- see
#: :func:`suspicious_times`. Deliberately not a refusal: a genuine
#: extended-hours fill is legal and must not be blocked by a heuristic.
_US_SESSION_UTC = (time(12, 0), time(21, 30))


@dataclass(frozen=True)
class Statement:
    """What one IB statement file yielded."""

    executions: tuple[SweptExecution, ...]
    #: TradeIDs of SELL rows, skipped rather than refused: a Trades export
    #: over a date range legitimately contains the day's exits, and making
    #: the operator hand-edit broker evidence to remove them would be worse
    #: than skipping them by name.
    skipped_sells: tuple[str, ...] = ()
    source: str | None = None
    source_sha256: str | None = None


def suspicious_times(
    executions: Sequence[SweptExecution],
) -> tuple[SweptExecution, ...]:
    """Executions whose UTC time-of-day is outside the US session.

    The one parser input where "wrong but plausible" is possible. Every
    ``DateTime`` is read as UTC, so a statement exported in the account's
    local time is off by four or five hours with nothing malformed about
    it -- and the fill lands against the wrong session. A US-equity fill
    at 09:31Z is four hours before any open, which is the signature.
    """
    low, high = _US_SESSION_UTC
    return tuple(
        execution for execution in executions
        if not (low <= execution.executed_at.timetz().replace(tzinfo=None) <= high)
    )

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


def _canonical_row(row: Mapping[str, str], index: int) -> dict[str, str]:
    """Map a statement row onto canonical column names, ignoring casing.

    Columns this tool does not need are dropped rather than refused: a Flex
    query with extra fields ticked is a fine export, and demanding exactly
    thirteen would send the operator back to the UI for nothing.

    Two spellings of ONE column in the same file IS refused. That is an
    ambiguous export, not a convenience -- silently picking one could pick
    the wrong one, and the value that lost would never be seen again.
    """
    canonical: dict[str, str] = {}
    source: dict[str, str] = {}
    for key, value in row.items():
        if key is None:
            continue
        column = _CANONICAL_COLUMNS.get(str(key).strip().casefold())
        if column is None:
            continue
        if column in canonical:
            raise StatementRefusedError(
                f"row {index} carries {column} twice, as {source[column]!r} "
                f"and {key!r}. Which one is authoritative cannot be guessed, "
                "so re-export without the duplicate."
            )
        canonical[column] = value
        source[column] = key
    return canonical


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


def _integer(row: Mapping[str, str], column: str, index: int) -> int:
    raw = _text(row, column, index)
    try:
        return int(raw)
    except ValueError as exc:
        raise StatementRefusedError(
            f"row {index} has a non-integer {column}: {raw!r}"
        ) from exc


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
) -> Statement:
    """Turn IB statement rows into the executions ``plan_sweep`` consumes.

    ``account_id``, when given, is the account the repair is for. When it is
    NOT given the first row's account is adopted and every later row must
    match it: a statement covering two accounts must never leak one book
    into the other, and leaving that guarantee to the operator remembering
    a flag is not a guarantee.
    """
    parsed: list[SweptExecution] = []
    skipped_sells: list[str] = []
    seen: set[str] = set()

    for index, raw_row in enumerate(rows, start=1):
        row = _canonical_row(raw_row, index)
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
        if account_id is None:
            account_id = account
        elif account != account_id:
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
        if _SIDE[side_raw] == "sell":
            # Skipped, not refused, and named in the dry run. A Trades
            # export over a date range legitimately contains the day's
            # exits; refusing the file would send the operator to hand-edit
            # broker evidence, which is worse. It must also happen BEFORE
            # the numeric parse: IB reports Quantity SIGNED, so a sell
            # reaching the positive-quantity guard is refused as "the export
            # is wrong" -- true of nothing, and it sends the operator to
            # re-export a perfectly good file.
            skipped_sells.append(execution_id)
            continue

        parsed.append(SweptExecution(
            execution_id=execution_id,
            account_id=account,
            ib_order_id=_text(row, "IBOrderID", index),
            con_id=_integer(row, "ConID", index),
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
        if skipped_sells:
            raise StatementRefusedError(
                f"the statement contains only SELL rows ({len(skipped_sells)} "
                "of them). This tool restores missed ENTRIES; a missed exit "
                "is scripts/ops/restore_missed_exit.py. Reporting 'nothing "
                "to recover' here would read as success."
            )
        raise StatementRefusedError(
            "the statement contains no rows. An empty export reports "
            "'nothing to recover', which looks exactly like a healthy book — "
            "so it is refused instead."
        )
    return Statement(
        executions=tuple(_with_cumulative(parsed)),
        skipped_sells=tuple(skipped_sells),
    )


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
        result.append(replace(execution, cumulative_quantity=total))
    return result


def load_statement(
    path: Path | str, *, account_id: str | None = None
) -> Statement:
    """Read an IB statement CSV off disk.

    ``utf-8-sig`` because IB's exports commonly carry a BOM, which turns
    the first header into ``\ufeffClientAccountID`` and produces a
    "missing required column: ClientAccountID" on a file that visibly
    contains it.
    """
    path = Path(path)
    if not path.is_file():
        raise StatementRefusedError(f"no statement file at {path}")
    raw = path.read_bytes()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        statement = parse_statement(csv.DictReader(handle), account_id=account_id)
    # Hashed at read time, so the digest describes the bytes that were
    # actually parsed rather than the file as it stood when the artifact
    # was written.
    return replace(
        statement, source=str(path), source_sha256=sha256(raw).hexdigest()
    )


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


@dataclass(frozen=True)
class RecoveryResult:
    """How far the apply got. Both halves matter; see :func:`apply_recovery`."""

    applied: tuple[str, ...] = ()
    #: ``(execution_id, reason)`` for the one that failed. At most one:
    #: the loop stops rather than burning the rest of the batch.
    failed: tuple[tuple[str, str], ...] = ()


def preflight(
    session: Session,
    outcome: SweepOutcome,
    executions: Sequence[SweptExecution],
) -> list[str]:
    """Rejections that are knowable from the plan, before anything is written.

    This exists because a rejection INSIDE the loop is terminal.
    ``FillProjector.apply`` commits its immutable ``execution_fills`` audit
    row before it validates, then raises, and
    ``OrderLedger.execution_fill_exists`` does not filter on
    ``projection_applied`` -- so a rejected fill reads as already-recorded
    forever. For the nightly sweep that is survivable, because IB serves the
    execution again tomorrow. Here there is no tomorrow: ``reqExecutions``
    is long past and the statement is the only surviving record. A burn is
    recoverable only by a database restore.

    Both checks mirror a ``ValueError`` in
    ``PaperTradingState._apply_fill_accounting`` that the projector converts
    to ``InvalidFillError`` *after* committing the audit row.
    """
    by_id = {execution.execution_id: execution for execution in executions}
    problems: list[str] = []

    # "fill would make sleeve cash negative" (paper_state.py:218)
    needed: dict[str, float] = {}
    for fill in outcome.recovered:
        execution = by_id[fill.execution_id]
        needed[fill.portfolio] = needed.get(fill.portfolio, 0.0) + (
            execution.quantity * execution.price + execution.commission
        )
    for portfolio, cost in sorted(needed.items()):
        config = session.scalar(
            select(PortfolioConfig).where(
                PortfolioConfig.portfolio == portfolio
            )
        )
        if config is None:
            problems.append(
                f"sleeve {portfolio!r} has no portfolio_config row, so the "
                "projector has no cash to move."
            )
        elif float(config.cash) - cost < -1e-9:
            problems.append(
                f"sleeve {portfolio!r} has cash {float(config.cash):,.2f} but "
                f"these fills cost {cost:,.2f}: the projector would raise "
                '"fill would make sleeve cash negative" AFTER committing the '
                "audit row, burning the execution permanently."
            )

    # "position account ownership is unresolved" (paper_state.py:192) --
    # ANY open position on the con_id with a NULL account_id, whatever sleeve.
    for con_id in sorted({by_id[f.execution_id].con_id for f in outcome.recovered}):
        orphan = session.scalar(
            select(func.count())
            .select_from(Position)
            .where(
                Position.con_id == con_id,
                Position.status == "open",
                Position.account_id.is_(None),
            )
        ) or 0
        if orphan:
            problems.append(
                f"con_id {con_id} has {orphan} open position(s) with no "
                'account_id: the projector would raise "position account '
                'ownership is unresolved" AFTER committing the audit row. '
                "Resolve the ownership first."
            )
    return problems


def unprojected_fills(
    session: Session, executions: Sequence[SweptExecution]
) -> list[str]:
    """Execution ids already recorded but never successfully projected.

    These are previous burns. ``plan_sweep`` counts them as
    ``already_recorded``, which on its own reads as "nothing to do" -- and
    that is indistinguishable from success while a real position is missing.
    """
    return sorted(
        row for (row,) in session.execute(
            select(ExecutionFill.execution_id).where(
                ExecutionFill.execution_id.in_(
                    [execution.execution_id for execution in executions]
                ),
                ExecutionFill.projection_applied.is_(False),
            )
        )
    )


def apply_recovery(
    session: Session, outcome: SweepOutcome, *, confirm: str
) -> RecoveryResult:
    """Project every recovered fill. Returns what landed and what did not.

    One transaction per fill, because ``FillProjector.apply`` owns its own
    and refuses a session carrying pending work. A failure partway therefore
    leaves the fills before it COMMITTED -- which is right: a purchase that
    really happened must never be rolled back to tidy up a later row's
    failure.

    It does NOT continue past a failure. The failing execution is already
    burned (see :func:`preflight`); carrying on would burn every remaining
    one against whatever condition rejected the first. Stopping keeps them
    recoverable.
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
        try:
            if projector.apply(fill):
                applied.append(fill.execution_id)
        except FillProjectionError as exc:
            return RecoveryResult(
                applied=tuple(applied),
                failed=((fill.execution_id, str(exc)),),
            )
    return RecoveryResult(applied=tuple(applied))


def verify(
    session: Session,
    outcome: SweepOutcome,
    result: RecoveryResult,
    executions: Sequence[SweptExecution],
) -> list[str]:
    """Refuse to report success unless the end state is exactly right.

    The sibling ``restore_missed_exit.py`` ends the same way. Without it a
    partial apply prints a success line.
    """
    session.expire_all()
    by_id = {execution.execution_id: execution for execution in executions}
    problems: list[str] = []
    if len(result.applied) != len(outcome.recovered):
        problems.append(
            f"{len(result.applied)} of {len(outcome.recovered)} fills were "
            "projected; the book is HALF REPAIRED"
        )
    for execution_id in result.applied:
        execution = by_id[execution_id]
        fill = session.scalar(
            select(ExecutionFill).where(
                ExecutionFill.account_id == execution.account_id,
                ExecutionFill.execution_id == execution_id,
            )
        )
        if fill is None or not fill.projection_applied:
            problems.append(
                f"{execution_id} ({execution.ticker}) has no projected "
                "execution_fills row"
            )
        position = session.scalar(
            select(Position).where(
                Position.con_id == execution.con_id,
                Position.status == "open",
            )
        )
        if position is None:
            problems.append(
                f"{execution.ticker} (con_id {execution.con_id}) has no open "
                "position"
            )
    return problems


def _render(
    outcome: SweepOutcome,
    executions: Sequence[SweptExecution],
    *,
    skipped_sells: Sequence[str] = (),
    burned: Sequence[str] = (),
    odd_hours: Sequence[SweptExecution] = (),
) -> str:
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
    already = f"{outcome.already_recorded} already recorded"
    if burned:
        already += f" ({len(burned)} of them NEVER PROJECTED)"
    lines.append(
        f"  {len(outcome.recovered)} to recover, {already}, "
        f"{len(outcome.untracked)} untracked, "
        f"{len(outcome.deferred)} deferred"
    )
    lines.append(f"  cost basis to be restored: {cost:,.2f}")
    if outcome.corrected:
        # A state change the operator is authorizing, and one the rollback
        # section says matters as much as the positions.
        lines.append(
            f"  {len(outcome.corrected)} intent(s) will be moved out of "
            "EXPIRED so the fill can terminalize them properly"
        )
    if skipped_sells:
        lines.append(
            f"  {len(skipped_sells)} SELL row(s) skipped -- this tool "
            "restores ENTRIES; a missed exit is restore_missed_exit.py"
        )
    if burned:
        lines.append("")
        lines.append(
            "  ⚠ PREVIOUSLY BURNED: these executions are recorded but were "
            "never projected, so they read as 'already recorded' and this "
            "tool can no longer recover them:"
        )
        for execution_id in burned:
            lines.append(f"      {execution_id}")
    if odd_hours:
        lines.append("")
        lines.append(
            "  ⚠ TIME ZONE: every DateTime is read as UTC, and these fall "
            "outside US trading hours. If the statement was exported in "
            "local time every fill is hours off and will be filed against "
            "the wrong session -- re-export in UTC:"
        )
        for execution in odd_hours:
            lines.append(
                f"      {execution.ticker:<6} "
                f"{execution.executed_at.strftime('%H:%M:%S')}Z "
                f"({execution.execution_id})"
            )
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
    statement: Statement,
    outcome: SweepOutcome,
    executions: Sequence[SweptExecution],
    applied: Sequence[str],
    stamp: str,
    failed: Sequence[tuple[str, str]] = (),
) -> dict:
    by_id = {execution.execution_id: execution for execution in executions}
    dates = sorted({by_id[e].executed_at.date().isoformat() for e in applied})
    return {
        "repair": "KAN-88 restore_missed_entries",
        "applied_at": stamp,
        "statement": statement.source,
        "statement_sha256": statement.source_sha256,
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
        #: Recorded but NOT projected: burned, and unrecoverable by this
        #: tool. The only durable record that a later run must not treat
        #: these as merely "already recorded".
        "failed": [
            {"execution_id": execution_id, "reason": reason}
            for execution_id, reason in failed
        ],
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
        help="the paper account this repair is for; any other is refused. "
             "Omitted, the first row's account is adopted and enforced.",
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

    try:
        return _run(args)
    except (StatementRefusedError, RecoveryRefusedError) as exc:
        # The refusal strings are the product. Arriving wrapped in a stack
        # trace at 05:00 wastes the effort that went into them.
        print(f"\nRefused: {exc}", file=sys.stderr)
        return 2


def _run(args) -> int:
    statement = load_statement(args.statement, account_id=args.account)
    executions = statement.executions
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
        burned = unprojected_fills(session, executions)
        odd_hours = suspicious_times(executions)
        print(f"\n{statement.source}\n")
        print(_render(
            outcome, executions,
            skipped_sells=statement.skipped_sells,
            burned=burned,
            odd_hours=odd_hours,
        ))

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
        if burned:
            print(
                "\nRefusing to apply: executions listed above are recorded "
                "but were never projected. They cannot be recovered by this "
                "tool -- see docs/operations/backups.md for the restore path."
            )
            session.rollback()
            return 1

        problems = preflight(session, outcome, executions)
        if problems:
            print("\nRefusing to apply -- these would be rejected by the "
                  "projector AFTER it commits the audit row, which burns the "
                  "execution permanently:")
            for problem in problems:
                print(f"  - {problem}")
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
        result = apply_recovery(session, outcome, confirm=answer.strip())

        # Written unconditionally: if the loop stopped partway, the record
        # of how far it got is the ONLY thing that says which of these a
        # later run must not attempt again.
        artifact_path = artifact_dir / f"entry_restore_{stamp}.json"
        artifact_path.write_text(json.dumps(
            _artifact(
                statement=statement,
                outcome=outcome,
                executions=executions,
                applied=result.applied,
                failed=result.failed,
                stamp=datetime.now(timezone.utc).isoformat(),
            ),
            indent=2,
        ))
        print(f"\nRecovered {len(result.applied)} fill(s). "
              f"Artifact: {artifact_path}")

        problems = verify(session, outcome, result, executions)
        if result.failed or problems:
            for execution_id, reason in result.failed:
                print(
                    f"\n🚨 {execution_id} was REJECTED by the projector: "
                    f"{reason}\n"
                    "   Its execution_fills row is committed but unprojected, "
                    "so this tool can never recover it again -- it will read "
                    "as 'already recorded'. Restore from the dump above, or "
                    "see docs/operations/backups.md."
                )
            for problem in problems:
                print(f"🚨 {problem}")
            print(
                "\nThe repair is INCOMPLETE. Do not re-run --apply blind; "
                "run the dry run and read what it now reports."
            )
            return 1

        print(
            "\nNow run:  python scripts/reconcile_paper.py --report\n"
            "and confirm severity: ok / entries_allowed: true before the "
            "next 04:15."
        )
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
