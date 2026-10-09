#!/usr/bin/env python3
"""Credit, allocate or transfer sleeve ledger cash, audited and flow-aware (KAN-113).

WHY THIS EXISTS
---------------
Since KAN-111 every buy is capped at its sleeve's ledger cash
(``portfolio_config.cash``, the row the fill projector locks and refuses to
take below zero). A deposit at IB, an FX conversion or a higher
``capital.paper.max_deployable_usd`` therefore adds NO buying power until that
cash is raised. Before this tool the only way was a hand-written UPDATE: no
pre-checks, no audit trail, and a step in sleeve equity that every reader
would have read as a return. See docs/operations/sleeve-cash.md.

MODES
-----
* credit one sleeve:   ``--portfolio momentum --amount-usd 5000``
* allocate a total by ``CAPITAL_ALLOCATIONS`` weights (scripts/run_paper.py),
  rounded to cents, the rounding remainder to the largest-weight sleeve:
  ``--allocate 20000``
* transfer between sleeves (no new money):
  ``--from thematic_momentum --to momentum --amount-usd 1500``

A credit or allocation raises each sleeve's ``cash`` AND ``capital`` (its
contributed capital, so the report's cash/capital stays meaningful); a
transfer moves both from source to destination.

WHAT IT CHECKS (every one is printed, ok or FAIL)
-------------------------------------------------
* the account is an IB paper account (``DU*``); a live ledger is never
  changed by a script;
* every sleeve exists, and is not a synthetic portfolio (``_*``);
* the amount is positive, finite and in whole cents;
* (credit/allocate) graded ledger capital after the credit — every graded
  sleeve's cash plus its open positions at their last marks — is within
  ``capital.paper.max_deployable_usd``, and within today's deployable capital
  as ``shared.capital.calculate_capital_budget`` computes it from the broker
  snapshot (the same function and definition the paper run sizes by);
* (credit/allocate) IB backs it: the account's USD cash (``TotalCashBalance``,
  what the capital model calls settled cash) less
  ``currency.minimum_settled_usd_reserve`` covers EVERY sleeve's ledger cash
  after the credit. Read-only snapshot, its own client id (61),
  ``readonly=True``. ``--skip-broker-check`` passes it as SKIPPED and the
  audit records that it was not verified;
* (transfer) the source keeps its open buy reservations: its cash after the
  transfer is at least ``OrderLedger.active_buy_reservations_for_account(
  portfolio=source)`` — the same number KAN-111 sizes against — so a
  transfer cannot strand an order that is already working;
* not a repeat: an identical flow (same mode, sleeves and amounts) recorded
  in the last 24 hours is refused unless ``--allow-repeat``;
* outside the paper-run window (04:00-07:00 SGT), and no equity snapshot
  written in the last 15 minutes (a late or manual paper run), so a snapshot
  cannot read half of a flow. In a plain dry run these two are WARNINGs, so
  the operator can preview at any hour; with ``--apply`` they refuse, and
  they are judged again under the lock at the moment of the write.

A credit whose sleeve would be worth more than its share of deployable
capital (``deployable x CAPITAL_ALLOCATIONS weight``) is a WARNING, not a
refusal: re-weighting a sleeve on purpose is what ``--portfolio`` is for. The
dry run also prints contributed capital (the ``capital`` column) beside the
marked figure, so profit is visible as the gap between them.

DRY RUN (default)
-----------------
Prints every sleeve's cash before and after, the totals against
``max_deployable_usd`` and the IB account, and every check. On Postgres the
planning transaction is ``READ ONLY``: it cannot write even by mistake.

--apply
-------
Needs ``--account``, an interactive TTY, a writable artifact directory
(proved BEFORE anything commits) and the exact phrase ``TOP UP SLEEVE
CASH``. Then ONE transaction: ``SELECT ... FOR UPDATE`` on the affected
``portfolio_config`` rows (the projector's lock, taken in name order), a
refusal if any sleeve's cash moved since the plan, the transfer reservation
check again under the lock, the timing guards and the broker snapshot's
age (at most 5 minutes) judged at the moment of the write, the cash/capital
update, and one ``capital_adjustments`` row per leg — the flow record (see
``shared/capital_flows.py``), stamped with the database clock
(``clock_timestamp()``) after the locks and checks, immediately before the
commit. Any exception rolls all of it back and is
recorded. After commit the book is re-read and compared with the plan, and an
audit artifact is written under ``output/reconciliation`` (relocated out of a
linked worktree by ``durable_artifact_dir``; an existing file is never
overwritten).

EVIDENCE
--------
The ``capital_adjustments`` rows are what keep the credit from reading as a
return: the divergence monitor, the epoch report / go-live gate drawdown, the
weekly digest and the risk service's ``peak_nav`` all use flow-adjusted
(time-weighted) equity, and the rolling shadow is seeded at live's raw NAV
and receives the same cash on the same session, so its capacity matches
live's and a window that spans a flow is graded in full: no window restarts,
no AGGREGATE collapse, and a BREACH streak neither resets nor pauses. ``equity_snapshots`` is never rewritten.

Usage (dry run first, always; ``python`` alone imports from another checkout,
so set ``PYTHONPATH``; the database comes from ``ALGO_DATABASE_URL`` or
``--database-url``)::

    PYTHONPATH=$PWD .venv/bin/python scripts/ops/topup_sleeve_cash.py \\
        --account DUN551088 --allocate 20000
    PYTHONPATH=$PWD .venv/bin/python scripts/ops/topup_sleeve_cash.py \\
        --account DUN551088 --allocate 20000 --apply

The operator runs ``--apply``. Agents run the dry-run at most.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import math
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta, timezone
from math import isclose
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.artifact_dir import durable_artifact_dir  # noqa: E402
from shared.broker_state import BrokerAccountSnapshot  # noqa: E402
from shared.capital import calculate_capital_budget  # noqa: E402
from shared.capital_flows import CapitalFlow, recorded_flows  # noqa: E402
from shared.config import load_config  # noqa: E402
from shared.models import (  # noqa: E402
    CapitalAdjustment,
    EquitySnapshot,
    PortfolioConfig,
    Position,
)
from shared.order_ledger import OrderLedger  # noqa: E402
from shared.universe import is_excluded_portfolio  # noqa: E402

#: Typed by the operator, in full, to apply.
CONFIRMATION = "TOP UP SLEEVE CASH"

DEFAULT_ARTIFACT_DIR = _REPO_ROOT / "output" / "reconciliation"

#: Distinct from every other IB client in the repo (1, 2, 10, 54, 57, 58, 95,
#: 105, 118, 119): IB disconnects the older session on a duplicate id.
DEFAULT_IB_CLIENT_ID = 61

CREDIT = "credit"
ALLOCATE = "allocate"
TRANSFER = "transfer"

#: An identical flow inside this window is presumed to be an accidental re-run.
REPEAT_WINDOW = timedelta(hours=24)

#: The 04:15 SGT paper run reads cash and writes snapshots in here. A flow that
#: commits between a run's cash read and its snapshot write would be filed as
#: included when it was not.
SGT = ZoneInfo("Asia/Singapore")
PAPER_RUN_WINDOW_SGT = (time(4, 0), time(7, 0))

#: A snapshot written this recently means a paper run (scheduled, late or
#: manual) may still be writing: a flow now could land between its cash read
#: and its snapshot write.
RECENT_SNAPSHOT_GUARD = timedelta(minutes=15)

#: The broker evidence a credit is approved on must be this fresh at apply.
MAX_BROKER_SNAPSHOT_AGE = timedelta(minutes=5)

MAX_DEPLOYABLE_REMEDY = (
    "raise capital.paper.max_deployable_usd first (config/default.yaml), or "
    "move existing cash with --from/--to instead"
)

#: ``reason`` prefix of every row this tool writes; the repeat check parses it.
REASON_PREFIX = "KAN-113"
_REASON_RE = re.compile(rf"^{REASON_PREFIX} (\w+) flow ([0-9a-f]+):")

_EPS = 1e-6


class TopupRefusedError(RuntimeError):
    """The top-up cannot be applied safely. Every one of these is deliberate."""


# --------------------------------------------------------------------------
# Planning (read-only)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    #: Passed, but the operator must read it. A dry-run timing check that
    #: ``--apply`` would refuse is one of these, so the preview still runs.
    warning: bool = False


@dataclass(frozen=True)
class Leg:
    """One sleeve's side of the flow, as planned."""

    portfolio: str
    amount: float
    cash_before: float
    capital_before: float
    market_value: float
    reservations: float

    @property
    def cash_after(self) -> float:
        return self.cash_before + self.amount

    @property
    def capital_after(self) -> float:
        return self.capital_before + self.amount


@dataclass(frozen=True)
class SleeveRow:
    portfolio: str
    cash: float
    capital: float
    market_value: float


@dataclass(frozen=True)
class BrokerEvidence:
    account_id: str
    mode: str
    settled_cash_usd: float
    net_liquidation_base: float
    fx_base_per_trading: float
    captured_at: str
    deployable_capital: float | None
    deployable_error: str | None = None


@dataclass(frozen=True)
class TopupPlan:
    kind: str
    account: str
    legs: tuple[Leg, ...]
    sleeves: tuple[SleeveRow, ...]
    max_deployable_usd: float | None
    minimum_usd_reserve: float
    broker: BrokerEvidence | None
    broker_skipped: bool
    broker_error: str | None
    checks: tuple[Check, ...]
    recent_flows: tuple[dict, ...] = ()
    #: The commission model the reservations were computed with, so the
    #: re-check under the lock uses the same one.
    commission_per_share: float = 0.0
    minimum_commission: float = 0.0
    #: Every capital_adjustments row already on record (any writer): the
    #: readers treat all of them as flows, so the operator should know.
    flows_on_record: int = 0
    flows_on_record_net: float = 0.0

    @property
    def net_amount(self) -> float:
        return sum(leg.amount for leg in self.legs)

    @property
    def graded_capital_before(self) -> float:
        return sum(
            row.cash + row.market_value for row in self.sleeves
            if not is_excluded_portfolio(row.portfolio)
        )

    @property
    def graded_capital_after(self) -> float:
        return self.graded_capital_before + self.net_amount

    @property
    def contributed_capital_before(self) -> float:
        """Sum of ``portfolio_config.capital`` over graded sleeves: what was
        put in, as opposed to what it is worth at the marks."""
        return sum(
            row.capital for row in self.sleeves
            if not is_excluded_portfolio(row.portfolio)
        )

    @property
    def contributed_capital_after(self) -> float:
        return self.contributed_capital_before + self.net_amount

    @property
    def ledger_cash_before(self) -> float:
        """Every sleeve's cash, drill sleeves included: all of it is IB's USD."""
        return sum(row.cash for row in self.sleeves)

    @property
    def ledger_cash_after(self) -> float:
        return self.ledger_cash_before + self.net_amount

    @property
    def problems(self) -> tuple[str, ...]:
        return tuple(
            f"{check.name}: {check.detail}" for check in self.checks
            if not check.ok
        )


def _cents(value: float) -> float:
    return round(value + 0.0, 2)


def amount_problem(amount: Any) -> str | None:
    """Why ``amount`` is not a usable credit, or None."""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return f"{amount!r} is not a number"
    if not math.isfinite(value):
        return f"{value} is not finite"
    if value <= 0:
        return f"{value} is not positive"
    if abs(value - _cents(value)) > 1e-9:
        return f"{value} has sub-cent precision; give dollars and cents"
    return None


def capital_allocations() -> dict[str, float]:
    """The live sleeve weights, from the one place that owns them.

    Imported lazily, like ``record_epoch.py``: ``scripts/run_paper.py`` pulls
    in the broker stack.
    """
    from scripts.run_paper import CAPITAL_ALLOCATIONS

    return dict(CAPITAL_ALLOCATIONS)


def allocate(total: float, weights: Mapping[str, float]) -> dict[str, float]:
    """Split ``total`` by ``weights`` in cents, summing exactly to ``total``.

    The rounding remainder (a cent or two) goes to the largest-weight sleeve.
    """
    weight_sum = sum(weights.values())
    if not weights or not isclose(weight_sum, 1.0, abs_tol=1e-6):
        raise TopupRefusedError(
            f"CAPITAL_ALLOCATIONS weights sum to {weight_sum}, not 1.0"
        )
    shares = {name: _cents(total * weight) for name, weight in weights.items()}
    remainder = _cents(total - sum(shares.values()))
    if remainder:
        largest = max(weights, key=lambda name: (weights[name], name))
        shares[largest] = _cents(shares[largest] + remainder)
    return shares


def requested_amounts(
    kind: str,
    *,
    amount: float | None = None,
    portfolio: str | None = None,
    source: str | None = None,
    destination: str | None = None,
    weights: Mapping[str, float] | None = None,
) -> list[tuple[str, float]]:
    """The signed leg amounts a mode asks for, before any check."""
    if kind == CREDIT:
        return [(str(portfolio), float(amount))]
    if kind == ALLOCATE:
        shares = allocate(float(amount), weights or capital_allocations())
        return sorted(shares.items())
    if kind == TRANSFER:
        return [(str(source), -float(amount)), (str(destination), float(amount))]
    raise TopupRefusedError(f"unknown mode {kind!r}")


def _sleeve_rows(session: Session) -> dict[str, SleeveRow]:
    market: dict[str, float] = {
        portfolio: float(value or 0.0)
        for portfolio, value in session.execute(
            select(
                Position.portfolio,
                func.sum(Position.quantity * Position.current_price),
            )
            .where(Position.status == "open")
            .group_by(Position.portfolio)
        ).all()
    }
    return {
        row.portfolio: SleeveRow(
            portfolio=row.portfolio,
            cash=float(row.cash),
            capital=float(row.capital),
            market_value=market.get(row.portfolio, 0.0),
        )
        for row in session.scalars(
            select(PortfolioConfig).order_by(PortfolioConfig.portfolio)
        )
    }


def _reservations(
    session: Session,
    *,
    account: str,
    portfolio: str,
    commission_per_share: float,
    minimum_commission: float,
) -> float:
    """What the sleeve's cash is already committed to (KAN-111's number)."""
    return OrderLedger(session).active_buy_reservations_for_account(
        account,
        commission_per_share=commission_per_share,
        minimum_commission=minimum_commission,
        portfolio=portfolio,
    )


def _flow_groups(
    flows: Sequence[CapitalFlow], *, since: datetime
) -> dict[tuple[str, str], list[CapitalFlow]]:
    """This tool's recorded flows since ``since``, by (mode, flow id)."""
    groups: dict[tuple[str, str], list[CapitalFlow]] = {}
    for flow in flows:
        match = _REASON_RE.match(flow.reason)
        if match is None or flow.at < since:
            continue
        groups.setdefault((match.group(1), match.group(2)), []).append(flow)
    return groups


def _signature(legs: Sequence[tuple[str, float]]) -> tuple:
    return tuple(sorted((name, _cents(amount)) for name, amount in legs))


def in_paper_run_window(now: datetime) -> bool:
    local = now.astimezone(SGT).time()
    start, end = PAPER_RUN_WINDOW_SGT
    return start <= local < end


def plan_topup(
    session: Session,
    *,
    kind: str,
    amounts: Sequence[tuple[str, float]],
    account: str,
    config: Any,
    broker_snapshot: BrokerAccountSnapshot | None = None,
    broker_error: str | None = None,
    skip_broker_check: bool = False,
    allow_repeat: bool = False,
    now: datetime | None = None,
    for_apply: bool = False,
) -> TopupPlan:
    """Work out the flow and every check. Issues SELECTs only, takes no locks.

    ``for_apply``: the timing checks (paper-run window, a snapshot written in
    the last 15 minutes) FAIL when the plan is about to be applied and are
    WARNINGs in a plain dry run, so the operator can preview at any hour.
    ``apply_topup`` re-checks both under the lock regardless.
    """
    now = now or datetime.now(timezone.utc)
    capital_mode = config.capital.paper
    currency = config.currency
    checks: list[Check] = []

    paper = str(account).startswith("DU")
    checks.append(Check(
        "paper account", paper,
        "" if paper else (
            f"account {account!r} is not an IB paper account (DU*); a live "
            "ledger is never changed by a script"
        ),
    ))

    problems = [
        f"{name}: {problem}" for name, amount in amounts
        if (problem := amount_problem(abs(amount))) is not None
    ]
    checks.append(Check("amount", not problems, "; ".join(problems)))

    sleeves = _sleeve_rows(session)
    missing = [name for name, _ in amounts if name not in sleeves]
    synthetic = [name for name, _ in amounts if is_excluded_portfolio(name)]
    sleeve_problem = "; ".join(
        [f"no portfolio_config row for {name!r}" for name in missing]
        + [f"{name!r} is a synthetic portfolio, not a graded sleeve"
           for name in synthetic]
    )
    checks.append(Check("sleeve exists", not sleeve_problem, sleeve_problem))

    if kind == TRANSFER:
        names = [name for name, _ in amounts]
        distinct = len(set(names)) == len(names)
        checks.append(Check(
            "distinct sleeves", distinct,
            "" if distinct else "--from and --to name the same sleeve",
        ))

    legs: list[Leg] = []
    for name, amount in amounts:
        row = sleeves.get(name)
        if row is None:
            continue
        reserved = (
            _reservations(
                session, account=account, portfolio=name,
                commission_per_share=currency.commission_per_share_usd,
                minimum_commission=currency.minimum_commission_usd,
            )
            if amount < 0 else 0.0
        )
        legs.append(Leg(
            portfolio=name,
            amount=float(amount),
            cash_before=row.cash,
            capital_before=row.capital,
            market_value=row.market_value,
            reservations=reserved,
        ))

    plan_kwargs = dict(
        kind=kind,
        account=account,
        legs=tuple(legs),
        sleeves=tuple(sleeves.values()),
        max_deployable_usd=capital_mode.max_deployable_usd,
        minimum_usd_reserve=float(currency.minimum_settled_usd_reserve),
        broker=None,
        broker_skipped=skip_broker_check,
        broker_error=broker_error,
        commission_per_share=float(currency.commission_per_share_usd),
        minimum_commission=float(currency.minimum_commission_usd),
    )
    draft = TopupPlan(**plan_kwargs, checks=())
    net = draft.net_amount

    if kind == TRANSFER:
        for leg in legs:
            if leg.amount >= 0:
                continue
            ok = leg.cash_after >= leg.reservations - _EPS
            checks.append(Check(
                "source keeps its reservations", ok,
                "" if ok else (
                    f"{leg.portfolio} would hold ${leg.cash_after:,.2f} "
                    f"against ${leg.reservations:,.2f} of open buy "
                    "reservations; a working order would no longer be funded"
                ),
            ))
        checks.append(Check(
            "total ledger capital unchanged", abs(net) < _EPS,
            "" if abs(net) < _EPS else f"legs sum to {net}",
        ))
    else:
        cap = capital_mode.max_deployable_usd
        after = draft.graded_capital_after
        if cap is None:
            checks.append(Check(
                "within max_deployable_usd", True,
                "uncapped: capital.paper.max_deployable_usd is null",
            ))
        else:
            ok = after <= cap + _EPS
            checks.append(Check(
                "within max_deployable_usd", ok,
                "" if ok else (
                    f"graded ledger capital after the credit would be "
                    f"${after:,.2f} at the marks (contributed "
                    f"${draft.contributed_capital_after:,.2f}), above "
                    f"max_deployable_usd ${cap:,.2f}; "
                    f"{MAX_DEPLOYABLE_REMEDY}"
                ),
            ))

    broker: BrokerEvidence | None = None
    if kind != TRANSFER:
        if skip_broker_check:
            checks.append(Check(
                "IB backs the credit", True,
                "SKIPPED (--skip-broker-check): NOT verified against IB; the "
                "audit artifact records that",
            ))
        elif broker_snapshot is None:
            checks.append(Check(
                "IB backs the credit", False,
                f"no broker snapshot ({broker_error or 'not read'}); fix the "
                "Gateway connection or pass --skip-broker-check deliberately",
            ))
        else:
            deployable: float | None = None
            deployable_error: str | None = None
            try:
                deployable = calculate_capital_budget(
                    broker_snapshot, "paper", config.capital, currency,
                    capital_allocations(),
                    now=now,
                ).deployable_capital
            except Exception as exc:  # noqa: BLE001 -- reported as a check
                deployable_error = f"{type(exc).__name__}: {exc}"
            broker = BrokerEvidence(
                account_id=broker_snapshot.account_id,
                mode=broker_snapshot.mode,
                settled_cash_usd=float(broker_snapshot.settled_cash_trading),
                net_liquidation_base=float(broker_snapshot.net_liquidation_base),
                fx_base_per_trading=float(broker_snapshot.fx_base_per_trading),
                captured_at=_iso(broker_snapshot.captured_at),
                deployable_capital=deployable,
                deployable_error=deployable_error,
            )
            same = (
                broker_snapshot.account_id == account
                and broker_snapshot.mode == "paper"
            )
            checks.append(Check(
                "broker account matches", same,
                "" if same else (
                    f"the Gateway serves {broker_snapshot.account_id!r} "
                    f"({broker_snapshot.mode}), not {account!r} (paper)"
                ),
            ))
            available = broker.settled_cash_usd - draft.minimum_usd_reserve
            ok = draft.ledger_cash_after <= available + _EPS
            checks.append(Check(
                "IB backs the credit", ok,
                "" if ok else (
                    f"every sleeve's ledger cash after the credit would be "
                    f"${draft.ledger_cash_after:,.2f}, but IB holds "
                    f"${broker.settled_cash_usd:,.2f} USD less the "
                    f"${draft.minimum_usd_reserve:,.2f} reserve = "
                    f"${available:,.2f}"
                ),
            ))
            if deployable is None:
                checks.append(Check(
                    "within today's deployable capital", False,
                    f"the capital model could not compute it: "
                    f"{deployable_error}",
                ))
            else:
                ok = draft.graded_capital_after <= deployable + _EPS
                checks.append(Check(
                    "within today's deployable capital", ok,
                    "" if ok else (
                        f"graded ledger capital after the credit would be "
                        f"${draft.graded_capital_after:,.2f}, above today's "
                        f"deployable capital ${deployable:,.2f} "
                        "(min(NAV x deployment_fraction, max_deployable_usd)); "
                        f"{MAX_DEPLOYABLE_REMEDY}"
                    ),
                ))

    if kind != TRANSFER:
        checks.extend(_budget_warnings(
            legs,
            base=(
                broker.deployable_capital
                if broker is not None and broker.deployable_capital is not None
                else capital_mode.max_deployable_usd
            ),
        ))

    on_record = recorded_flows(session)
    recent = _flow_groups(on_record, since=now - REPEAT_WINDOW)
    wanted = _signature(amounts)
    repeats = [
        flow_id for (mode, flow_id), flows in sorted(recent.items())
        if mode == kind
        and _signature([(f.portfolio, f.amount) for f in flows]) == wanted
    ]
    if repeats and allow_repeat:
        checks.append(Check(
            "not a repeat", True,
            f"identical flow(s) {', '.join(repeats)} in the last 24h; "
            "allowed by --allow-repeat",
        ))
    else:
        checks.append(Check(
            "not a repeat", not repeats,
            "" if not repeats else (
                f"an identical {kind} was recorded in the last 24h (flow "
                f"{', '.join(repeats)}); if a second one is really meant, "
                "pass --allow-repeat"
            ),
        ))

    timing = dict(timing_problems(session, now))
    for name in TIMING_CHECKS:
        problem = timing.get(name)
        if problem is None:
            checks.append(Check(name, True))
        elif for_apply:
            checks.append(Check(name, False, problem))
        else:
            checks.append(Check(
                name, True,
                f"WARNING: {problem}; --apply would refuse now",
                warning=True,
            ))

    recent_rows = tuple(
        {
            "flow_id": flow_id,
            "mode": mode,
            "legs": [
                {"portfolio": f.portfolio, "amount": f.amount,
                 "at": _iso(f.at)}
                for f in flows
            ],
        }
        for (mode, flow_id), flows in sorted(recent.items())
    )
    return TopupPlan(
        **{**plan_kwargs, "broker": broker},
        checks=tuple(checks),
        recent_flows=recent_rows,
        flows_on_record=len(on_record),
        flows_on_record_net=sum(flow.amount for flow in on_record),
    )


def _budget_warnings(legs: Sequence[Leg], *, base: float | None) -> list[Check]:
    """WARN when a credit takes a sleeve past its share of deployable capital.

    Not a refusal: a deliberate re-weighting is a legitimate use of
    ``--portfolio``. But a sleeve worth more than ``base x weight`` at the
    marks is larger than the paper run's own sizing basis for it.
    """
    if base is None or not legs:
        return []
    weights = capital_allocations()
    over = []
    for leg in legs:
        weight = weights.get(leg.portfolio)
        if weight is None or leg.amount <= 0:
            continue
        worth = leg.cash_after + leg.market_value
        budget = base * weight
        if worth > budget + _EPS:
            over.append(
                f"{leg.portfolio} would be worth ${worth:,.2f} at the marks, "
                f"above its {weight:.2%} share ${budget:,.2f}"
            )
    if not over:
        return []
    return [Check(
        "within each sleeve's share", True,
        "WARNING: " + "; ".join(over), warning=True,
    )]


TIMING_CHECKS = (
    "outside the paper-run window",
    "no snapshot in the last 15 minutes",
)


def timing_problems(session: Session, now: datetime) -> list[tuple[str, str]]:
    """``(check name, problem)`` for each reason this is a bad moment to write.

    The paper run reads sleeve cash and then writes its snapshot; a flow that
    commits between the two is filed as inside a row whose cash never saw it.
    """
    problems: list[tuple[str, str]] = []
    if in_paper_run_window(now):
        problems.append((
            "outside the paper-run window",
            "it is 04:00-07:00 SGT, when the paper run reads sleeve cash and "
            "writes snapshots; apply after it finishes",
        ))
    newest = session.scalar(select(func.max(EquitySnapshot.created_at)))
    if newest is not None:
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        if newest >= now - RECENT_SNAPSHOT_GUARD:
            problems.append((
                "no snapshot in the last 15 minutes",
                f"an equity snapshot was written at {_iso(newest)}: a paper "
                "run (scheduled, late or manual) may still be running; wait "
                "until it has finished",
            ))
    return problems


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AppliedLeg:
    portfolio: str
    amount: float
    cash_before: float
    cash_after: float
    capital_before: float
    capital_after: float


@dataclass(frozen=True)
class AppliedTopup:
    flow_id: str
    kind: str
    account: str
    applied_at: str
    operator: str
    reason: str
    legs: tuple[AppliedLeg, ...] = field(default_factory=tuple)


def reason_for(kind: str, flow_id: str, note: str) -> str:
    return f"{REASON_PREFIX} {kind} flow {flow_id}: {note}"


def apply_topup(
    session: Session,
    plan: TopupPlan,
    *,
    confirm: str,
    operator: str,
    note: str,
    now: datetime | None = None,
) -> AppliedTopup:
    """Apply the plan in ONE transaction under the projector's row locks.

    Everything is re-read under ``FOR UPDATE`` rather than trusted from the
    plan. If any sleeve's cash moved since the plan (a fill landed), it is a
    refusal, not a recalculation: the operator approved the numbers printed,
    and the post-apply check compares against them. Any exception leaves the
    ``with`` and rolls back the cash, the capital and the flow rows together.

    The flow's timestamp is taken AFTER the locks are held and every check
    has passed, immediately before the write and the commit, from the
    database clock (``clock_timestamp()``) on Postgres. The timing guards
    (paper-run window, a snapshot in the last 15 minutes) and the broker
    snapshot's age are judged at that same instant, because the operator may
    have typed the phrase minutes after the dry run. ``now`` overrides the
    clock (tests).
    """
    if confirm != CONFIRMATION:
        raise TopupRefusedError(
            f"exact confirmation required: expected {CONFIRMATION!r}"
        )
    if plan.problems:
        raise TopupRefusedError(
            "the plan carries refusals; resolve them and re-run the dry run:"
            "\n  " + "\n  ".join(plan.problems)
        )
    if not plan.legs:
        raise TopupRefusedError("the plan has no legs")
    # The planning reads opened a transaction; the write needs its own.
    if session.in_transaction():
        session.rollback()

    flow_id = uuid4().hex[:12]
    reason = reason_for(plan.kind, flow_id, note)
    names = sorted(leg.portfolio for leg in plan.legs)
    applied: list[AppliedLeg] = []
    with session.begin():
        rows = {
            row.portfolio: row
            for row in session.scalars(
                select(PortfolioConfig)
                .where(PortfolioConfig.portfolio.in_(names))
                .order_by(PortfolioConfig.portfolio)
                .with_for_update()
            )
        }
        for leg in plan.legs:
            row = rows.get(leg.portfolio)
            if row is None:
                raise TopupRefusedError(
                    f"{leg.portfolio}: the portfolio_config row has gone"
                )
            if not isclose(float(row.cash), leg.cash_before,
                           rel_tol=0, abs_tol=_EPS):
                raise TopupRefusedError(
                    f"{leg.portfolio} cash is {float(row.cash):,.6f}, the dry "
                    f"run planned from {leg.cash_before:,.6f}: the book moved "
                    "(a fill?). Nothing was written; re-run the dry run."
                )
            if leg.amount < 0:
                reserved = _reservations(
                    session, account=plan.account, portfolio=leg.portfolio,
                    commission_per_share=plan.commission_per_share,
                    minimum_commission=plan.minimum_commission,
                )
                if float(row.cash) + leg.amount < reserved - _EPS:
                    raise TopupRefusedError(
                        f"{leg.portfolio} would hold "
                        f"${float(row.cash) + leg.amount:,.2f} against "
                        f"${reserved:,.2f} of open buy reservations (they "
                        "grew since the dry run). Nothing was written."
                    )
        applied_at = now or _db_now(session)
        for _, problem in timing_problems(session, applied_at):
            raise TopupRefusedError(f"{problem}. Nothing was written.")
        if plan.broker is not None:
            age = applied_at - datetime.fromisoformat(plan.broker.captured_at)
            if age > MAX_BROKER_SNAPSHOT_AGE:
                raise TopupRefusedError(
                    f"the broker snapshot the credit was approved on is "
                    f"{age.total_seconds() / 60:.1f} minutes old (limit "
                    f"{MAX_BROKER_SNAPSHOT_AGE.total_seconds() / 60:.0f}); "
                    "re-run the dry run and apply straight after it. "
                    "Nothing was written."
                )
        for leg in plan.legs:
            row = rows[leg.portfolio]
            cash_before, capital_before = float(row.cash), float(row.capital)
            row.cash = cash_before + leg.amount
            row.capital = capital_before + leg.amount
            row.updated_at = applied_at
            session.add(CapitalAdjustment(
                account_id=plan.account,
                portfolio=leg.portfolio,
                amount=leg.amount,
                reason=reason,
                operator=operator,
                created_at=applied_at,
            ))
            applied.append(AppliedLeg(
                portfolio=leg.portfolio,
                amount=leg.amount,
                cash_before=cash_before,
                cash_after=cash_before + leg.amount,
                capital_before=capital_before,
                capital_after=capital_before + leg.amount,
            ))
    return AppliedTopup(
        flow_id=flow_id,
        kind=plan.kind,
        account=plan.account,
        applied_at=_iso(applied_at),
        operator=operator,
        reason=reason,
        legs=tuple(applied),
    )


def _db_now(session: Session) -> datetime:
    """The database's wall clock, as UTC; the process clock off Postgres.

    ``clock_timestamp()`` (not ``now()``, which is the transaction start) so
    the stamp is the moment of the write, on the same clock that will judge
    the next snapshot's ``created_at`` against it.
    """
    if session.get_bind().dialect.name == "postgresql":
        value = session.scalar(text("SELECT clock_timestamp()"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return _now()


def _discard(session: Session) -> None:
    """Best-effort rollback on a session whose connection may be gone."""
    try:
        session.rollback()
    except Exception:  # noqa: BLE001 -- nothing useful to do with it
        pass


@dataclass(frozen=True)
class Verification:
    lines: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


def verify(
    session: Session, applied: AppliedTopup, plan: TopupPlan
) -> Verification:
    """Re-read the book and compare it with what the plan predicted.

    ``capital`` and the flow rows are this tool's alone, so a mismatch there
    is a problem. ``cash`` is also moved by the projector: a fill on the
    sleeve between the commit and this re-read changes it legitimately, so a
    cash mismatch is reported, labelled as possibly that, and not counted as
    a failure.
    """
    lines: list[str] = []
    problems: list[str] = []
    planned = {leg.portfolio: leg for leg in plan.legs}
    try:
        session.expire_all()
        for leg in applied.legs:
            row = session.scalar(select(PortfolioConfig).where(
                PortfolioConfig.portfolio == leg.portfolio
            ))
            expected = planned[leg.portfolio]
            for label, actual, want in (
                ("cash", None if row is None else float(row.cash),
                 expected.cash_after),
                ("capital", None if row is None else float(row.capital),
                 expected.capital_after),
            ):
                ok = actual is not None and isclose(
                    actual, want, rel_tol=0, abs_tol=_EPS
                )
                shown = "missing" if actual is None else f"{actual:,.6f}"
                fill_may_explain = label == "cash" and actual is not None
                flag = (
                    "" if ok
                    else "  MISMATCH (may be a fill that landed after commit)"
                    if fill_may_explain else "  MISMATCH"
                )
                lines.append(
                    f"  {leg.portfolio} {label} {shown} (planned {want:,.6f})"
                    f"{flag}"
                )
                if not ok and not fill_may_explain:
                    problems.append(
                        f"{leg.portfolio} {label} is {shown}, the plan "
                        f"predicted {want:,.6f}"
                    )
        recorded = session.scalar(
            select(func.count()).select_from(CapitalAdjustment).where(
                CapitalAdjustment.reason == applied.reason
            )
        )
        ok = recorded == len(applied.legs)
        lines.append(
            f"  capital_adjustments rows for flow {applied.flow_id}: "
            f"{recorded} (planned {len(applied.legs)})"
            f"{'' if ok else '  MISMATCH'}"
        )
        if not ok:
            problems.append(
                f"flow {applied.flow_id} has {recorded} capital_adjustments "
                f"row(s), expected {len(applied.legs)}"
            )
    except Exception as exc:  # noqa: BLE001 -- verification must not mask
        problems.append(
            f"could not re-read the book to verify: {type(exc).__name__}: {exc}"
        )
    finally:
        _discard(session)
    return Verification(lines=tuple(lines), problems=tuple(problems))


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _money(value: float | None) -> str:
    return "-" if value is None else f"${value:,.2f}"


def render(plan: TopupPlan) -> str:
    lines: list[str] = []
    lines.append(
        f"Sleeve cash {plan.kind} for account {plan.account}: net "
        f"{_money(plan.net_amount)} new capital"
    )
    lines.append("")
    lines.append(
        f"  {'sleeve':20}{'cash before':>16}{'change':>14}{'cash after':>16}"
        f"{'capital after':>16}{'positions':>14}"
    )
    by_name = {leg.portfolio: leg for leg in plan.legs}
    for row in plan.sleeves:
        leg = by_name.get(row.portfolio)
        change = 0.0 if leg is None else leg.amount
        capital_after = row.capital + change
        marker = "" if leg is None else "  <-"
        lines.append(
            f"  {row.portfolio:20}{_money(row.cash):>16}"
            f"{(f'{change:+,.2f}' if change else '-'):>14}"
            f"{_money(row.cash + change):>16}{_money(capital_after):>16}"
            f"{_money(row.market_value):>14}{marker}"
        )
    for leg in plan.legs:
        if leg.reservations:
            lines.append(
                f"  {leg.portfolio}: open buy reservations "
                f"{_money(leg.reservations)}"
            )
    lines.append("")
    cap = (
        "uncapped" if plan.max_deployable_usd is None
        else _money(plan.max_deployable_usd)
    )
    lines.append(
        f"  graded ledger capital (cash + positions at last marks): "
        f"{_money(plan.graded_capital_before)} -> "
        f"{_money(plan.graded_capital_after)}   max_deployable_usd {cap}"
    )
    gap = plan.graded_capital_before - plan.contributed_capital_before
    lines.append(
        f"  contributed capital (sum of portfolio_config.capital): "
        f"{_money(plan.contributed_capital_before)} -> "
        f"{_money(plan.contributed_capital_after)}   marks vs contributed: "
        f"{gap:+,.2f} (profit and loss so far)"
    )
    lines.append(
        f"  ledger cash, all sleeves: {_money(plan.ledger_cash_before)} -> "
        f"{_money(plan.ledger_cash_after)}"
    )
    if plan.broker is not None:
        broker = plan.broker
        lines.append(
            f"  IB {broker.account_id} ({broker.mode}): USD cash "
            f"{_money(broker.settled_cash_usd)} less reserve "
            f"{_money(plan.minimum_usd_reserve)} = "
            f"{_money(broker.settled_cash_usd - plan.minimum_usd_reserve)}; "
            f"today's deployable capital "
            f"{_money(broker.deployable_capital)}  (captured "
            f"{broker.captured_at})"
        )
    elif plan.broker_skipped:
        lines.append("  IB: NOT READ (--skip-broker-check)")
    elif plan.kind == TRANSFER:
        lines.append("  IB: not needed for a transfer (no new money)")
    else:
        lines.append(f"  IB: unavailable ({plan.broker_error or 'not read'})")
    lines.append(
        f"  capital flows already on record (capital_adjustments): "
        f"{plan.flows_on_record} row(s), net "
        f"{_money(plan.flows_on_record_net)}"
    )
    if plan.recent_flows:
        lines.append("")
        lines.append("  flows recorded by this tool in the last 24h:")
        for flow in plan.recent_flows:
            legs = ", ".join(
                f"{leg['portfolio']} {leg['amount']:+,.2f}"
                for leg in flow["legs"]
            )
            lines.append(f"    {flow['mode']} {flow['flow_id']}: {legs}")
    lines.append("")
    lines.append("  pre-checks:")
    for check in plan.checks:
        mark = "FAIL" if not check.ok else "WARN" if check.warning else "ok  "
        suffix = f" -- {check.detail}" if check.detail else ""
        lines.append(f"    [{mark}] {check.name}{suffix}")
    return "\n".join(lines)


def write_exclusive(directory: Path, stem: str, payload: Mapping) -> Path:
    """Write ``payload`` as JSON to a NEW file; never replace an existing one."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, default=str) + "\n"
    for n in range(1000):
        path = directory / (f"{stem}.json" if n == 0 else f"{stem}-{n}.json")
        try:
            with path.open("x") as handle:
                handle.write(body)
        except FileExistsError:
            continue
        return path
    raise TopupRefusedError(f"could not find a free artifact name for {stem}")


def ensure_writable(directory: Path) -> Path:
    """Prove the artifact can be written BEFORE anything commits."""
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=directory, prefix=".topup-sleeve-cash-probe-"
        ) as probe:
            probe.write(b"ok")
            probe.flush()
    except OSError as exc:
        raise TopupRefusedError(
            f"the audit artifact directory {directory} is not writable "
            f"({exc}). Nothing has been written to the database. Fix it or "
            "pass --artifact-dir."
        ) from exc
    return directory


FLOW_NOTE = (
    "This is a capital flow, not a return. It is recorded in "
    "capital_adjustments (one signed row per sleeve leg, created_at = the "
    "database clock at the write, just before commit), and "
    "shared/capital_flows.py removes it from equity readers: the divergence "
    "monitor, the epoch report and go-live gate drawdown, the weekly digest's "
    "change and the risk service's peak_nav use time-weighted, flow-adjusted "
    "equity. The rolling shadow receives the same cash on the same session, "
    "so its capacity matches live's; divergence windows spanning the flow "
    "are graded in full on flow-adjusted returns (a note names it) and BREACH "
    "streaks carry across it. equity_snapshots is not rewritten."
)


def build_artifact(
    plan: TopupPlan,
    *,
    attempted_at: datetime,
    note: str,
    operator: str,
    applied: AppliedTopup | None,
    failure: str | None,
    verification: Verification | None,
) -> dict:
    return {
        "tool": "KAN-113 topup_sleeve_cash",
        "attempted_at": _iso(attempted_at),
        "mode": plan.kind,
        "account": plan.account,
        "operator": operator,
        "note": note,
        "outcome": (
            "applied" if applied is not None and failure is None
            else "rolled_back"
        ),
        "plan": {
            "legs": [
                {**asdict(leg), "cash_after": leg.cash_after,
                 "capital_after": leg.capital_after}
                for leg in plan.legs
            ],
            "net_amount": plan.net_amount,
            "graded_capital_before": plan.graded_capital_before,
            "graded_capital_after": plan.graded_capital_after,
            "contributed_capital_before": plan.contributed_capital_before,
            "contributed_capital_after": plan.contributed_capital_after,
            "ledger_cash_before": plan.ledger_cash_before,
            "ledger_cash_after": plan.ledger_cash_after,
            "max_deployable_usd": plan.max_deployable_usd,
            "minimum_usd_reserve": plan.minimum_usd_reserve,
            "checks": [asdict(check) for check in plan.checks],
            "sleeves_before": [asdict(row) for row in plan.sleeves],
        },
        "broker": (
            asdict(plan.broker) if plan.broker is not None else None
        ),
        "broker_check": (
            "not needed (transfer)" if plan.kind == TRANSFER
            else "SKIPPED by --skip-broker-check: the credit was NOT "
            "verified against IB" if plan.broker_skipped
            else "verified"
        ),
        "applied": None if applied is None else {
            **{k: v for k, v in asdict(applied).items() if k != "legs"},
            "legs": [asdict(leg) for leg in applied.legs],
        },
        "failure": failure,
        "verification": None if verification is None else {
            "lines": list(verification.lines),
            "problems": list(verification.problems),
        },
        "evidence_note": FLOW_NOTE,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


async def _read_broker_snapshot_async(
    *, host: str, port: int, client_id: int, config: Any
) -> BrokerAccountSnapshot:
    from ib_insync import IB

    from services.execution.ib_account import IBAccountReader

    ib = IB()
    try:
        await ib.connectAsync(
            host, port, clientId=client_id, readonly=True, timeout=15
        )
        return await IBAccountReader(
            ib,
            expected_mode="paper",
            expected_base_currency=config.currency.expected_base_currency,
            trading_currency=config.currency.trading_currency,
        ).snapshot()
    finally:
        if ib.isConnected():
            ib.disconnect()


def read_broker_snapshot(
    *, host: str, port: int, client_id: int, config: Any
) -> BrokerAccountSnapshot:
    """Read-only IB account snapshot. Module-level so tests can replace it."""
    return asyncio.run(_read_broker_snapshot_async(
        host=host, port=port, client_id=client_id, config=config
    ))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Credit, allocate or transfer sleeve ledger cash "
            "(portfolio_config.cash). Dry-run by default."
        )
    )
    parser.add_argument("--portfolio", help="credit this sleeve")
    parser.add_argument(
        "--amount-usd", type=float,
        help="the amount for --portfolio or --from/--to",
    )
    parser.add_argument(
        "--allocate", type=float, metavar="USD",
        help="split this total across sleeves by CAPITAL_ALLOCATIONS",
    )
    parser.add_argument("--from", dest="source", help="transfer source sleeve")
    parser.add_argument("--to", dest="destination", help="transfer destination")
    parser.add_argument(
        "--account", required=True,
        help="the IB paper account (DU*) this ledger belongs to",
    )
    parser.add_argument(
        "--skip-broker-check", action="store_true",
        help="do not read IB; the credit is NOT verified and the audit says so",
    )
    parser.add_argument(
        "--allow-repeat", action="store_true",
        help="allow an identical flow recorded in the last 24h",
    )
    parser.add_argument(
        "--note", default="operator top-up",
        help="why (recorded on every capital_adjustments row and the audit)",
    )
    parser.add_argument("--operator", default=None, help="defaults to $USER")
    parser.add_argument(
        "--database-url", default=None,
        help="defaults to ALGO_DATABASE_URL / config/default.yaml",
    )
    parser.add_argument(
        "--artifact-dir", default=None,
        help="where the audit record goes; defaults to output/reconciliation "
             "(relocated out of a linked git worktree)",
    )
    parser.add_argument("--ib-host", default=None)
    parser.add_argument("--ib-port", type=int, default=None)
    parser.add_argument(
        "--ib-client-id", type=int, default=DEFAULT_IB_CLIENT_ID
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="write the flow (default: dry-run report only)",
    )
    return parser


def _mode(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    credit = args.portfolio is not None
    allocating = args.allocate is not None
    transfer = args.source is not None or args.destination is not None
    if credit + allocating + transfer != 1:
        parser.error(
            "choose exactly one mode: --portfolio X --amount-usd N, "
            "--allocate N, or --from A --to B --amount-usd N"
        )
    if credit:
        if args.amount_usd is None:
            parser.error("--portfolio needs --amount-usd")
        return CREDIT
    if allocating:
        if args.amount_usd is not None:
            parser.error("--allocate takes its amount itself; drop --amount-usd")
        return ALLOCATE
    if args.source is None or args.destination is None:
        parser.error("a transfer needs both --from and --to")
    if args.amount_usd is None:
        parser.error("a transfer needs --amount-usd")
    return TRANSFER


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    kind = _mode(args, parser)
    try:
        return _run(args, kind)
    except TopupRefusedError as exc:
        print(f"\nRefused: {exc}", file=sys.stderr)
        return 2


def _now() -> datetime:
    """The wall clock; module-level so tests can pin it."""
    return datetime.now(timezone.utc)


def _run(args: argparse.Namespace, kind: str) -> int:
    config = load_config(str(_REPO_ROOT / "config" / "default.yaml"))
    amount = args.allocate if kind == ALLOCATE else args.amount_usd
    problem = amount_problem(amount)
    if problem is not None:
        raise TopupRefusedError(f"amount: {problem}")
    amounts = requested_amounts(
        kind,
        amount=amount,
        portfolio=args.portfolio,
        source=args.source,
        destination=args.destination,
    )

    snapshot: BrokerAccountSnapshot | None = None
    broker_error: str | None = None
    if kind != TRANSFER and not args.skip_broker_check:
        try:
            snapshot = read_broker_snapshot(
                host=args.ib_host or config.ib.host,
                port=args.ib_port or config.ib.paper_port,
                client_id=args.ib_client_id,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 -- shown as a failed check
            broker_error = f"{type(exc).__name__}: {exc}"

    url = args.database_url or config.database.url
    engine = create_engine(url)
    operator = args.operator or getpass.getuser()
    with Session(engine) as session:
        if engine.dialect.name == "postgresql":
            # Must be the transaction's first statement. Planning never
            # writes; any write, by this tool or a bug in it, is refused.
            session.execute(text("SET TRANSACTION READ ONLY"))
        now = _now()
        plan = plan_topup(
            session,
            kind=kind,
            amounts=amounts,
            account=args.account,
            config=config,
            broker_snapshot=snapshot,
            broker_error=broker_error,
            skip_broker_check=args.skip_broker_check,
            allow_repeat=args.allow_repeat,
            now=now,
            for_apply=args.apply,
        )
        session.rollback()
        print(render(plan))

        if plan.problems:
            print("\nRefusing to apply:")
            for line in plan.problems:
                print(f"  - {line}")
            print("\nNothing was written.")
            return 1
        if not args.apply:
            print(
                "\nDry-run only; nothing was written. To apply, re-run "
                "IMMEDIATELY with the same arguments and --apply."
            )
            return 0

        if not sys.stdin.isatty():
            raise TopupRefusedError("--apply requires an interactive TTY")
        directory = ensure_writable(durable_artifact_dir(
            args.artifact_dir or DEFAULT_ARTIFACT_DIR,
            explicit=args.artifact_dir is not None,
        ))
        moved = sum(leg.amount for leg in plan.legs if leg.amount > 0)
        answer = input(
            f"\nType {CONFIRMATION} to {plan.kind} {_money(moved)}: "
        )
        if answer.strip() != CONFIRMATION:
            raise TopupRefusedError(
                f"exact confirmation required: expected {CONFIRMATION!r}"
            )

        attempted_at = _now()
        applied: AppliedTopup | None = None
        failure: str | None = None
        verification: Verification | None = None
        try:
            try:
                applied = apply_topup(
                    session, plan, confirm=answer.strip(),
                    operator=operator, note=args.note,
                )
            except BaseException as exc:
                failure = f"{type(exc).__name__}: {exc}"
                _discard(session)
                if not isinstance(exc, Exception):
                    raise
            if applied is not None:
                verification = verify(session, applied, plan)
        finally:
            path = write_exclusive(
                directory,
                f"topup-sleeve-cash-{attempted_at:%Y%m%dT%H%M%SZ}",
                build_artifact(
                    plan,
                    attempted_at=attempted_at,
                    note=args.note,
                    operator=operator,
                    applied=applied,
                    failure=failure,
                    verification=verification,
                ),
            )
            print(f"\nAudit artifact: {path}")

        if failure is not None:
            print(
                f"\n🚨 FAILED: {failure}\n"
                "   The transaction rolled back completely: no sleeve's cash "
                "or capital changed and no flow was recorded."
            )
            return 1
        assert applied is not None
        print(f"\nApplied flow {applied.flow_id} ({applied.kind}):")
        for leg in applied.legs:
            print(
                f"  {leg.portfolio}: cash {leg.cash_before:,.2f} -> "
                f"{leg.cash_after:,.2f}, capital {leg.capital_before:,.2f} -> "
                f"{leg.capital_after:,.2f}"
            )
        if verification is not None:
            print("\nPost-apply check (book vs plan):")
            for line in verification.lines:
                print(line)
            if verification.problems:
                for line in verification.problems:
                    print(f"🚨 {line}")
                return 1
        print(f"\n{FLOW_NOTE}")
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
