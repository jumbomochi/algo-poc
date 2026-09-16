"""Repairing the equity SERIES after a restored exit — KAN-85 slice 2.

``restore_missed_exit.py`` fixes the ledger: trade, realised P&L, cash, intent.
It cannot fix ``equity_snapshots``, which is a *recorded* series — one row
written per day from the cash and market value as they stood that morning
(``paper_state.py::record_equity_snapshot`` takes all three as arguments).
Correcting today's cash does not reach back into it.

That leaves the defect visible in the one place that matters. The divergence
monitor reads ``equity_snapshots`` (divergence_monitor.py:410), so after both
2026-09-16 restores ``sector_rotation`` still measured -34.0% against its
shadow, and the next morning's snapshot would have jumped +5,524.99 in a single
day with no trade dated to it — the same "step change with no trade" signature
the write-offs produced, only upward.

WHY A BACKFILL HERE IS RECOMPUTATION, NOT FABRICATION. The rows carry ``cash``
and ``market_value`` separately, and across the whole affected span
``sector_rotation``'s cash is frozen at 8,746.46: every drop came through
``market_value`` because ``_apply_action`` never touched cash. So the corrected
equity is just ``cash + market_value`` with the proceeds applied from the date
the book learned of each sale. No number is invented.

THE INVARIANT THAT MAKES IT SAFE. The adjustments must reconcile exactly to
``portfolio_config.cash`` as it now stands. If they do not, the operator has
mis-stated a date or an amount and the backfill is refused — which is what
stops this tool from being a way to paint any series you like.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from scripts.ops.backfill_restored_equity import (
    Adjustment,
    BackfillRefusedError,
    apply_backfill,
    plan_backfill,
)
from shared.models import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.portfolio_config import PortfolioConfig

PORTFOLIO = "sector_rotation"
BASE_CASH = 8746.46
PANW = 3187.54
LLY = 2337.45

#: (date, market_value) exactly as the real rows stand, across both write-offs.
SERIES = [
    ("2026-08-27", 6996.85),
    ("2026-08-28", 7368.37),
    ("2026-08-29", 3891.38),   # PANW left market_value here
    ("2026-09-12", 3870.60),
    ("2026-09-15", 1628.08),   # LLY left market_value here
    ("2026-09-16", 1586.48),
]

ADJUSTMENTS = [
    Adjustment(effective_from=date(2026, 8, 29), amount=PANW, note="PANW restored"),
    Adjustment(effective_from=date(2026, 9, 15), amount=LLY, note="LLY restored"),
]


@pytest.fixture()
def session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _seed(s)
        yield s


def _seed(session, *, cash_now=BASE_CASH + PANW + LLY):
    now = datetime.now(timezone.utc)
    session.add(PortfolioConfig(
        portfolio=PORTFOLIO, capital=15380.0, cash=cash_now, currency="USD",
        created_at=now, updated_at=now,
    ))
    for day, mkt in SERIES:
        session.add(EquitySnapshot(
            portfolio=PORTFOLIO, date=date.fromisoformat(day),
            equity=BASE_CASH + mkt, cash=BASE_CASH, market_value=mkt,
            created_at=now,
        ))
    session.commit()


_UNSET = object()


def _plan(session, adjustments=_UNSET, **over):
    # `or ADJUSTMENTS` would swallow an explicit empty list, which is exactly
    # the case test_no_adjustments_is_refused exercises.
    if adjustments is _UNSET:
        adjustments = ADJUSTMENTS
    kwargs = dict(portfolio=PORTFOLIO, adjustments=adjustments)
    kwargs.update(over)
    return plan_backfill(session, **kwargs)


# ---------------------------------------------------------------------------
# The reconciliation invariant — what stops this painting any series you like
# ---------------------------------------------------------------------------


def test_adjustments_must_reconcile_to_current_cash(session):
    """The last row's corrected cash must equal portfolio_config.cash. If it
    does not, a date or an amount is wrong and nothing should be written."""
    with pytest.raises(BackfillRefusedError, match="reconcile|does not match"):
        _plan(session, adjustments=[
            Adjustment(effective_from=date(2026, 8, 29), amount=PANW, note="PANW"),
        ])


def test_a_wrong_amount_is_refused(session):
    with pytest.raises(BackfillRefusedError, match="reconcile|does not match"):
        _plan(session, adjustments=[
            Adjustment(effective_from=date(2026, 8, 29), amount=PANW, note="PANW"),
            Adjustment(effective_from=date(2026, 9, 15), amount=LLY + 100, note="LLY"),
        ])


def test_the_refusal_names_both_figures(session):
    """A refusal the operator cannot act on just moves the problem."""
    with pytest.raises(BackfillRefusedError) as exc:
        _plan(session, adjustments=[
            Adjustment(effective_from=date(2026, 8, 29), amount=PANW, note="PANW"),
        ])
    message = str(exc.value)
    assert "14271.45" in message.replace(",", "")
    assert "11934.00" in message.replace(",", "")


def test_the_real_adjustments_reconcile(session):
    plan = _plan(session)
    assert plan.reconciles
    assert plan.final_cash == pytest.approx(BASE_CASH + PANW + LLY)


# ---------------------------------------------------------------------------
# The corrected series
# ---------------------------------------------------------------------------


def test_equity_is_recomputed_as_cash_plus_market_value(session):
    """No number is invented: market_value is untouched and cash moves by the
    stated adjustments only."""
    plan = _plan(session)
    for row in plan.rows:
        assert row.new_equity == pytest.approx(row.new_cash + row.market_value)


def test_market_value_is_never_touched(session):
    plan = _plan(session)
    for row, (_, mkt) in zip(plan.rows, SERIES):
        assert row.market_value == pytest.approx(mkt)


def test_rows_before_the_first_adjustment_are_unchanged(session):
    """The book had not learned of the sale yet; those rows were internally
    consistent and must stay exactly as recorded."""
    plan = _plan(session)
    early = [r for r in plan.rows if r.date < date(2026, 8, 29)]
    assert early, "fixture must cover dates before the first adjustment"
    for row in early:
        assert row.new_equity == pytest.approx(row.old_equity)
        assert row.new_cash == pytest.approx(BASE_CASH)


def test_the_artificial_step_changes_are_removed(session):
    """The whole point. -3,476.99 and -2,242.52 were write-offs, not market
    moves; what remains is the real mark-to-actual difference."""
    plan = _plan(session)
    by_date = {r.date: r for r in plan.rows}

    panw_step = (by_date[date(2026, 8, 29)].new_equity
                 - by_date[date(2026, 8, 28)].new_equity)
    lly_step = (by_date[date(2026, 9, 15)].new_equity
                - by_date[date(2026, 9, 12)].new_equity)

    assert panw_step == pytest.approx(-289.45, abs=0.01)
    assert lly_step == pytest.approx(94.93, abs=0.01)


def test_no_remaining_step_exceeds_a_plausible_market_move(session):
    """A residual step of thousands would mean an adjustment date is wrong."""
    plan = _plan(session)
    equities = [r.new_equity for r in plan.rows]
    steps = [abs(b - a) for a, b in zip(equities, equities[1:])]
    assert max(steps) < 500, steps


# ---------------------------------------------------------------------------
# Applying it, and the audit trail option C asks for
# ---------------------------------------------------------------------------


def test_apply_requires_the_exact_confirmation(session, tmp_path):
    plan = _plan(session)
    with pytest.raises(BackfillRefusedError, match="confirm"):
        apply_backfill(session, plan, confirm="yes", artifact_dir=tmp_path)


def test_apply_rewrites_the_series(session, tmp_path):
    plan = _plan(session)
    apply_backfill(session, plan, confirm="BACKFILL RESTORED EQUITY",
                   artifact_dir=tmp_path)

    rows = session.scalars(
        select(EquitySnapshot)
        .where(EquitySnapshot.portfolio == PORTFOLIO)
        .order_by(EquitySnapshot.date)
    ).all()
    latest = rows[-1]
    assert latest.cash == pytest.approx(BASE_CASH + PANW + LLY)
    assert latest.equity == pytest.approx(latest.cash + latest.market_value)


def test_apply_writes_an_audit_artifact_with_before_and_after(session, tmp_path):
    """Rewriting gate evidence silently has the same problem as reconstructing
    a trade silently. Every changed row is recorded, both sides."""
    plan = _plan(session)
    apply_backfill(session, plan, confirm="BACKFILL RESTORED EQUITY",
                   artifact_dir=tmp_path)

    artifacts = list(tmp_path.glob("equity-backfill-*.json"))
    assert len(artifacts) == 1, artifacts
    payload = json.loads(artifacts[0].read_text())

    assert payload["portfolio"] == PORTFOLIO
    assert len(payload["adjustments"]) == 2
    assert payload["final_cash"] == pytest.approx(BASE_CASH + PANW + LLY)
    changed = payload["rows"]
    assert len(changed) == len(SERIES)
    for row in changed:
        assert "old_equity" in row and "new_equity" in row
        assert "old_cash" in row and "new_cash" in row


def test_a_refused_plan_writes_nothing(session, tmp_path):
    plan = _plan(session)
    with pytest.raises(BackfillRefusedError):
        apply_backfill(session, plan, confirm="nope", artifact_dir=tmp_path)

    assert not list(tmp_path.glob("equity-backfill-*.json"))
    row = session.scalar(
        select(EquitySnapshot).where(EquitySnapshot.date == date(2026, 9, 16))
    )
    assert row.cash == pytest.approx(BASE_CASH), "refused plan must not write"


def test_apply_is_refused_twice(session, tmp_path):
    """Once applied the series already reconciles, so a second run would add
    the proceeds again. The invariant catches it."""
    plan = _plan(session)
    apply_backfill(session, plan, confirm="BACKFILL RESTORED EQUITY",
                   artifact_dir=tmp_path)

    with pytest.raises(BackfillRefusedError):
        _plan(session)


def test_an_unknown_portfolio_is_refused(session):
    with pytest.raises(BackfillRefusedError, match="unknown|no portfolio"):
        _plan(session, portfolio="not_a_sleeve")


def test_no_adjustments_is_refused(session):
    with pytest.raises(BackfillRefusedError, match="adjustment"):
        _plan(session, adjustments=[])
