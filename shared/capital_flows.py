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
rows that sum to zero), ``created_at`` = the database clock
(``clock_timestamp()``) taken under the row locks after every check, just
before the commit. The tool writes the rows in the SAME transaction as the
cash, so a flow is recorded if and only if it happened. No migration is
needed: the table has existed since the durable ledger shipped.

WHICH SNAPSHOT A FLOW IS IN
---------------------------
An ``equity_snapshots`` row valued its sleeve at the cash it read when it was
written, so a flow is *included* in a row exactly when the flow's
``created_at`` precedes the row's. The microseconds between the stamp and the
commit cannot be misfiled because the tool refuses to write during the paper
run's window or within 15 minutes of any snapshot write. That is decided per row, not per date, so a
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
The rolling shadow is seeded at live's RAW NAV on its window's first session
— what live actually held — and every flow inside the window is replayed
into it as a cash injection on the session it enters live's series
(:func:`flow_steps`; ``BacktestRunner.run(cash_flows=...)``). The shadow's
capacity therefore matches live's: positions taken before the flow were sized
on the pre-flow capital on both sides, and the credit becomes available to
size from the same session on (KAN-111's cash cap sees it). Both curves are
then compared on the flow-adjusted (time-weighted) basis. So:

* a window that spans a flow is graded in full — it never shrinks, the
  AGGREGATE (which intersects sessions across sleeves) never collapses after
  an ``--allocate``, and a credit can neither turn sessions into NO_DATA nor
  produce a short-window OK that clears a running BREACH streak;
* the verdict carries a note naming the flow (:func:`describe_flow_steps`);
* a window with no flow inside it is replayed exactly as before, and the
  shadow fingerprint does not change: a flow is a fact about the book, not a
  model change.

Two rejected alternatives: starting the window after the flow (exact, but it
collapses the window and the AGGREGATE to one session for up to a window
length), and seeding at the flow-adjusted NAV (it runs the whole window on
the post-flow capital, so idle credited cash on the live side reads as
drift: a +20% credit into a +5% rally is roughly a 20% relative divergence,
the BREACH threshold).

A withdrawal the shadow's own cash cannot cover (a transfer out of a sleeve
whose counterfactual book is more invested than live's) is clamped to the
shadow's cash: the replay cannot sell to fund it. The clamped amount is what
the shadow's curve is adjusted by, so its returns stay correct; its capacity
then exceeds live's by the shortfall, until the replay's own exits free cash.

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
    "describe_flow_steps",
    "flow_steps",
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


def flow_steps(
    rows: Iterable[Mapping[str, Any]], flows: Iterable[CapitalFlow]
) -> list[tuple[date, float]]:
    """``(session, net flow)`` for each session whose value took in a flow.

    The session is the first one valued after the flow, i.e. where the step
    happens in the series. Empty with no flows or no rows.
    """
    record = rows_of_record(rows)
    flows = list(flows)
    if not record or not flows:
        return []
    sessions = sorted(record)
    included = [included_flow(flows, record[s][1]) for s in sessions]
    return [
        (sessions[index], included[index] - included[index - 1])
        for index in range(1, len(sessions))
        if abs(included[index] - included[index - 1]) >= _EPS
    ]


def describe_flow_steps(
    steps: Iterable[tuple[str, date, float]],
    *,
    start: date | None,
    end: date | None,
) -> str | None:
    """A verdict note for the flows inside ``(start, end]``, or None.

    ``steps`` are ``(sleeve, session, amount)``. A step ON ``start`` is not
    inside: the window's first value already includes it.
    """
    if start is None or end is None:
        return None
    inside = sorted(
        (session, sleeve, amount) for sleeve, session, amount in steps
        if start < session <= end
    )
    if not inside:
        return None
    named = ", ".join(
        f"{sleeve} {amount:+,.2f} USD valued from {session.isoformat()}"
        for session, sleeve, amount in inside
    )
    return (
        f"capital flow(s) inside this window (KAN-113): {named}. The shadow "
        "received the same cash on the same session, and both sides are "
        "compared on flow-adjusted (time-weighted) returns, so the window is "
        "graded in full; the flow is not performance."
    )
