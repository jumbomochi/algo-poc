"""Capital flows into and between sleeves, and how equity readers remove them.

WHY THIS EXISTS (KAN-113)
-------------------------
Since KAN-111 a sleeve can only spend its ledger cash
(``portfolio_config.cash``), so deploying new capital means crediting that
cash, and ``scripts/ops/topup_sleeve_cash.py`` is the tool that does it. A
credit raises the sleeve's recorded equity by the amount credited on the next
snapshot. Read naively, that step is a return: a $5,000 credit to a $20,000
sleeve looks like a +25% session, it lifts the drawdown peak, and it hides a
loss the sleeve was carrying. None of that is performance.

WHERE A FLOW IS RECORDED
------------------------
In ``capital_adjustments`` — the table the durable-ledger design created for
exactly this ("explicit funding and withdrawal events by sleeve",
docs/superpowers/specs/2026-07-18-durable-paper-ledger-design.md §6.3), which
nothing wrote until now. One row per sleeve leg, signed (a transfer is two
rows that sum to zero), ``created_at`` = the instant the cash change
committed. The tool writes the rows in the SAME transaction as the cash, so a
flow is recorded if and only if it happened. No migration is needed: the
table has existed since the durable ledger shipped.

WHICH SNAPSHOT A FLOW IS IN
---------------------------
An ``equity_snapshots`` row valued its sleeve at the cash it read when it was
written, so a flow is *included* in a row exactly when the flow committed
before the row's ``created_at``. That is decided per row, not per date, so a
catch-up run that re-values an old session after a flow is attributed
correctly (the step into and out of it carries the flow both ways).

HOW READERS REMOVE IT: TIME-WEIGHTED RETURNS
--------------------------------------------
:func:`flow_adjust` rewrites a series so that every step's return is the
flow-free return ``(E_i - F_i) / E_{i-1}``, where ``F_i`` is the flow that
entered between the two valuations. The adjusted series is anchored at its
LAST point — the newest value is the real equity, and every earlier value is
rescaled into today's capital — so:

* ratios (window returns, daily returns, correlations, drawdowns) are exactly
  the time-weighted ones, and are independent of the anchor;
* values after the newest flow are unchanged, bit for bit; a series with no
  flows is returned unchanged.

THE SHADOW (the KAN-105 seeding question)
-----------------------------------------
The rolling shadow is seeded at live's NAV on its window's first session and
then replays with no flows. A window that spans a flow would seed the shadow
with the pre-flow NAV and grade it against a sleeve that later had more cash
to spend (KAN-111 caps buys at that cash), which is a capacity difference,
not drift. The rule is the simplest correct one: **the shadow's window never
spans a flow.** :func:`flow_free_since` gives the first session after the
newest flow step, and ``scripts/run_paper.py::live_equity_by_sleeve`` trims
the live curve it seeds from to that session onward. The cost is a shorter
window — "Only N overlapping days" — for up to one window length after a
flow, which is honest: a sleeve whose capital just changed has that much
flow-free history and no more.

WHAT THIS DOES NOT DO
---------------------
It rewrites no row. ``equity_snapshots`` keeps the raw equity; adjustment
happens in the reader, every time, from the recorded flows. Displayed balances
("Equity 51,234.00 USD") stay raw; only returns and drawdowns are adjusted.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from shared.models.order_ledger import CapitalAdjustment

__all__ = [
    "CapitalFlow",
    "flow_adjust",
    "flow_adjusted_by_session",
    "flow_free_since",
    "flows_by_portfolio",
    "included_flow",
    "recorded_flows",
    "rows_of_record",
]

#: Below this a flow difference is float noise, not money.
_EPS = 1e-6

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _utc(moment: datetime | None) -> datetime:
    if moment is None:
        # A row with no write time cannot be placed after any flow.
        return _EPOCH
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@dataclass(frozen=True)
class CapitalFlow:
    """One sleeve leg of a recorded capital flow. ``amount`` is signed USD."""

    portfolio: str
    amount: float
    at: datetime
    reason: str = ""
    account_id: str | None = None


def recorded_flows(
    session: Session, *, portfolios: Iterable[str] | None = None
) -> list[CapitalFlow]:
    """Every recorded flow, oldest first. ``[]`` if the table does not exist.

    A database without ``capital_adjustments`` (one that predates the durable
    ledger) cannot hold a flow — the tool that writes them needs the table —
    so absence reads as "no flows", not as an error. The table is checked
    before it is queried: on Postgres a failed SELECT would abort the caller's
    transaction.
    """
    if not inspect(session.connection()).has_table(
        CapitalAdjustment.__tablename__
    ):
        return []
    statement = select(CapitalAdjustment).order_by(
        CapitalAdjustment.created_at, CapitalAdjustment.id
    )
    if portfolios is not None:
        statement = statement.where(
            CapitalAdjustment.portfolio.in_(list(portfolios))
        )
    return [
        CapitalFlow(
            portfolio=row.portfolio,
            amount=float(row.amount),
            at=_utc(row.created_at),
            reason=row.reason or "",
            account_id=row.account_id,
        )
        for row in session.scalars(statement)
    ]


def flows_by_portfolio(
    flows: Iterable[CapitalFlow],
) -> dict[str, list[CapitalFlow]]:
    out: dict[str, list[CapitalFlow]] = {}
    for flow in flows:
        out.setdefault(flow.portfolio, []).append(flow)
    return out


def included_flow(
    flows: Iterable[CapitalFlow], written_at: datetime | None
) -> float:
    """Net flow already in the cash of a row written at ``written_at``."""
    moment = _utc(written_at)
    return sum(flow.amount for flow in flows if flow.at < moment)


def flow_adjust(
    values: Mapping[date, float], included: Mapping[date, float]
) -> dict[date, float]:
    """Remove flows from an equity series: time-weighted, anchored at the end.

    ``included[d]`` is the cumulative flow already inside ``values[d]``; the
    step from ``d_{i-1}`` to ``d_i`` therefore carries ``F_i = included[d_i] -
    included[d_{i-1}]``. Walking back from the newest point with a scale
    ``k`` (1 at the end), ``k_{i-1} = k_i * E_i / (E_i - F_i)``, and the
    adjusted value is ``E_i * k_i``. Each adjusted step's return is then
    ``(E_i - F_i) / E_{i-1} - 1``: the flow-free return.

    The convention is that a flow earns nothing in the step it enters: a
    credit lands as idle cash between two snapshots and is first deployed by
    the next paper run, so the step's return is measured on the pre-flow
    base.

    A step whose flow-free value ``E_i - F_i`` is not positive cannot be
    expressed as a return (the readers already drop non-positive equity as a
    data fault); it is left unscaled rather than divided by.
    """
    days = sorted(values)
    if not days:
        return {}
    flows = [
        included.get(day, 0.0) - included.get(prev, 0.0)
        for prev, day in zip(days, days[1:])
    ]
    if all(abs(flow) < _EPS for flow in flows):
        return {day: values[day] for day in days}
    scale = 1.0
    out: dict[date, float] = {days[-1]: values[days[-1]]}
    for index in range(len(days) - 1, 0, -1):
        day = days[index]
        flow = flows[index - 1]
        equity = float(values[day])
        if abs(flow) >= _EPS and equity > 0 and equity - flow > 0:
            scale *= equity / (equity - flow)
        previous = days[index - 1]
        out[previous] = float(values[previous]) * scale
    return {day: out[day] for day in days}


def rows_of_record(
    rows: Iterable[Mapping[str, Any]],
) -> dict[date, tuple[float, datetime | None]]:
    """``{session: (equity, created_at)}`` from ``get_equity_history`` dicts.

    The same row-of-record rule as ``shared.session_dating.equity_by_session``:
    unstamped rows are skipped, and where two run dates valued one session the
    later run wins.
    """
    latest: dict[date, tuple[date, float, datetime | None]] = {}
    for row in rows:
        raw = row.get("session_date")
        if raw is None:
            continue
        session = raw if isinstance(raw, date) else date.fromisoformat(str(raw))
        run = row["date"]
        run = run if isinstance(run, date) else date.fromisoformat(str(run))
        held = latest.get(session)
        if held is None or run >= held[0]:
            latest[session] = (run, float(row["equity"]), row.get("created_at"))
    return {
        session: (value, written)
        for session, (_, value, written) in sorted(latest.items())
    }


def flow_adjusted_by_session(
    rows: Iterable[Mapping[str, Any]], flows: Iterable[CapitalFlow]
) -> dict[date, float]:
    """One sleeve's equity by session, with its recorded flows removed."""
    record = rows_of_record(rows)
    flows = list(flows)
    values = {session: value for session, (value, _) in record.items()}
    if not flows:
        return values
    included = {
        session: included_flow(flows, written)
        for session, (_, written) in record.items()
    }
    return flow_adjust(values, included)


def flow_free_since(
    rows: Iterable[Mapping[str, Any]], flows: Iterable[CapitalFlow]
) -> date | None:
    """First session from which the series holds no flow step, or None.

    Every session on or after the returned one includes the same flows, so a
    window starting there never spans a flow. ``None`` for an empty series.
    """
    record = rows_of_record(rows)
    if not record:
        return None
    flows = list(flows)
    sessions = sorted(record)
    if not flows:
        return sessions[0]
    included = [included_flow(flows, record[s][1]) for s in sessions]
    start = sessions[0]
    for index in range(1, len(sessions)):
        if abs(included[index] - included[index - 1]) >= _EPS:
            start = sessions[index]
    return start
