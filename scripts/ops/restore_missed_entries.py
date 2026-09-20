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

import csv
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.execution.execution_sweep import SweptExecution  # noqa: E402


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
