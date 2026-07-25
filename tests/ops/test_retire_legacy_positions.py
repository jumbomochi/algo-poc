from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from scripts.ops.retire_legacy_positions import (
    RetireRefusedError,
    retire_legacy_positions,
)
from shared.models import Base, Position


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def _pos(session, *, ticker, portfolio, account_id, con_id, status="open"):
    p = Position(
        account_id=account_id, ticker=ticker, portfolio=portfolio, con_id=con_id,
        quantity=1.0, avg_entry_price=1.0, current_price=1.0, peak_price=1.0,
        highest_price_since_entry=1.0, opened_at=datetime.now(UTC),
        status=status,
    )
    session.add(p)
    session.flush()
    return p


def test_dry_run_reports_unowned_open_and_mutates_nothing(session):
    _pos(session, ticker="AMD", portfolio="momentum", account_id=None, con_id=None)
    _pos(session, ticker="NVDA", portfolio="quality_value", account_id=None, con_id=None)
    _pos(session, ticker="AAPL", portfolio="momentum", account_id="DUN551088", con_id=265598)

    summary = retire_legacy_positions(session, apply=False, confirm=None)

    assert summary.count == 2
    assert {r[0] for r in summary.rows} == {"AMD", "NVDA"}
    open_rows = session.scalars(select(Position).where(Position.status == "open")).all()
    assert len(open_rows) == 3  # nothing closed


def test_apply_closes_only_unowned_open_rows(session):
    _pos(session, ticker="AMD", portfolio="momentum", account_id=None, con_id=None)
    _pos(session, ticker="NVDA", portfolio="quality_value", account_id=None, con_id=None)
    owned = _pos(session, ticker="AAPL", portfolio="momentum", account_id="DUN551088", con_id=265598)

    summary = retire_legacy_positions(session, apply=True, confirm="2")

    assert summary.count == 2
    closed = session.scalars(select(Position).where(Position.status == "closed")).all()
    assert {c.ticker for c in closed} == {"AMD", "NVDA"}
    for c in closed:
        assert c.closed_at is not None
        assert c.account_id is None  # audit trail preserved
    session.refresh(owned)
    assert owned.status == "open"  # owned row untouched


def test_apply_refuses_on_wrong_confirmation_and_does_not_mutate(session):
    _pos(session, ticker="AMD", portfolio="momentum", account_id=None, con_id=None)

    with pytest.raises(RetireRefusedError):
        retire_legacy_positions(session, apply=True, confirm="99")

    assert session.scalars(select(Position).where(Position.status == "open")).all()


def test_apply_is_idempotent(session):
    _pos(session, ticker="AMD", portfolio="momentum", account_id=None, con_id=None)
    retire_legacy_positions(session, apply=True, confirm="1")

    summary = retire_legacy_positions(session, apply=True, confirm="0")

    assert summary.count == 0
