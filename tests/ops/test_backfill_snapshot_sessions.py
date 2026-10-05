"""KAN-103: filling equity_snapshots.session_date on history.

The rows below are shaped like the real paper book's: Tue–Sat 04:15 SGT runs,
a pre-KAN-44 row with no valuation_at, the Tuesday after Labor Day re-valuing
Friday, and the Sunday catch-up re-valuing Friday.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from scripts.ops import backfill_snapshot_sessions as backfill
from scripts.ops.backfill_snapshot_sessions import (
    CONFIRMATION,
    DISAGREES,
    FILL,
    STAMPED,
    UNRESOLVED,
    BackfillRefusedError,
    apply_backfill,
    plan_backfill,
    render,
)
from shared.models import Base
from shared.models.equity_snapshot import EquitySnapshot

ROOT = Path(__file__).resolve().parents[2]
SGT = timezone(timedelta(hours=8))


def sgt(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=SGT).astimezone(timezone.utc)


#: (portfolio, run date, equity, valuation_at, created_at, session_date)
ROWS = [
    # Pre-KAN-44: no valuation_at; Tue run values Monday 08-03.
    ("momentum", date(2026, 8, 4), 100.0, None, sgt(2026, 8, 4, 4, 16), None),
    # Sat 08-08 and the Sunday 22:30 catch-up both value Friday 08-07.
    ("momentum", date(2026, 8, 8), 101.0, None, sgt(2026, 8, 8, 4, 16), None),
    ("momentum", date(2026, 8, 9), 101.0, None, sgt(2026, 8, 9, 22, 30), None),
    # Sat 09-05 and Tue 09-08 (after Labor Day) both value Friday 09-04.
    ("momentum", date(2026, 9, 5), 102.0, sgt(2026, 9, 5, 4, 15),
     sgt(2026, 9, 5, 4, 16), None),
    ("momentum", date(2026, 9, 8), 102.0, sgt(2026, 9, 8, 6, 20),
     sgt(2026, 9, 8, 6, 27), None),
    ("_aggregate", date(2026, 9, 5), 500.0, sgt(2026, 9, 5, 4, 15),
     sgt(2026, 9, 5, 4, 16), None),
    ("_aggregate", date(2026, 9, 8), 500.0, sgt(2026, 9, 8, 6, 20),
     sgt(2026, 9, 8, 6, 27), None),
    # The 08:43 SGT catch-up on 10-02 values Thursday 10-01.
    ("momentum", date(2026, 10, 2), 103.0, sgt(2026, 10, 2, 8, 40),
     sgt(2026, 10, 2, 8, 43), None),
    # Valued at 22:00 SGT, inside the US 09-29 session: maybe partial.
    ("momentum", date(2026, 9, 29), 104.0, sgt(2026, 9, 29, 22, 0),
     sgt(2026, 9, 29, 22, 1), None),
    # valuation_at says Monday 09-28, the run date (Wed 09-30) says Tuesday.
    ("momentum", date(2026, 9, 30), 105.0, sgt(2026, 9, 29, 4, 15),
     sgt(2026, 9, 30, 4, 16), None),
    # Already stamped by run_paper: kept.
    ("momentum", date(2026, 10, 3), 106.0, sgt(2026, 10, 3, 4, 15),
     sgt(2026, 10, 3, 4, 16), date(2026, 10, 2)),
    # Stamped from a stale bar: disagrees with the rule, never changed.
    ("momentum", date(2026, 10, 1), 107.0, sgt(2026, 10, 1, 4, 15),
     sgt(2026, 10, 1, 4, 16), date(2026, 9, 29)),
]


def _seed(session: Session) -> None:
    for portfolio, run_date, equity, valuation_at, created_at, session_date in ROWS:
        session.add(EquitySnapshot(
            portfolio=portfolio, date=run_date, equity=equity, cash=equity,
            market_value=0.0, valuation_at=valuation_at, created_at=created_at,
            session_date=session_date,
        ))
    session.commit()


@pytest.fixture()
def db_url(tmp_path) -> str:
    url = f"sqlite:///{tmp_path / 'sessions.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        _seed(s)
    return url


@pytest.fixture()
def session(db_url):
    with Session(create_engine(db_url)) as s:
        yield s


def _dump(url: str) -> list[tuple]:
    with create_engine(url).connect() as connection:
        return [tuple(r) for r in connection.execute(
            text("SELECT * FROM equity_snapshots ORDER BY id")
        )]


def _by_key(plan):
    return {(r.portfolio, r.run_date): r for r in plan.rows}


def test_the_plan_maps_each_run_date_to_the_session_it_valued(session):
    rows = _by_key(plan_backfill(session))

    assert rows[("momentum", date(2026, 8, 4))].proposed == date(2026, 8, 3)
    assert rows[("momentum", date(2026, 8, 4))].rule == "run_date"
    assert rows[("momentum", date(2026, 8, 8))].proposed == date(2026, 8, 7)
    assert rows[("momentum", date(2026, 8, 9))].proposed == date(2026, 8, 7)
    assert rows[("momentum", date(2026, 9, 5))].proposed == date(2026, 9, 4)
    assert rows[("momentum", date(2026, 9, 8))].proposed == date(2026, 9, 4)
    assert rows[("momentum", date(2026, 9, 8))].rule == "valuation_at"
    assert rows[("momentum", date(2026, 10, 2))].proposed == date(2026, 10, 1)
    for key in [
        ("momentum", date(2026, 8, 4)), ("momentum", date(2026, 9, 8)),
        ("_aggregate", date(2026, 9, 8)), ("momentum", date(2026, 10, 2)),
    ]:
        assert rows[key].status == FILL


def test_a_row_valued_inside_the_settle_margin_is_unresolved(tmp_path):
    """KAN-104's "in progress" includes the 5 minutes after the bell."""
    url = f"sqlite:///{tmp_path / 'settle.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        # 16:02 ET on Mon 09-28: closed, but its daily bar not yet trusted.
        at = datetime(2026, 9, 28, 20, 2, tzinfo=timezone.utc)
        s.add(EquitySnapshot(
            portfolio="momentum", date=date(2026, 9, 29), equity=1.0, cash=1.0,
            market_value=0.0, valuation_at=at, created_at=at,
        ))
        s.commit()
        (row,) = plan_backfill(s).rows
    assert row.status == UNRESOLVED
    assert "inside the 2026-09-28 session" in row.reason


def test_a_row_valued_while_a_session_traded_is_unresolved(session):
    row = _by_key(plan_backfill(session))[("momentum", date(2026, 9, 29))]
    assert row.status == UNRESOLVED
    assert row.proposed is None
    assert "inside the 2026-09-29 session" in row.reason


def test_a_row_whose_two_rules_disagree_is_unresolved(session):
    row = _by_key(plan_backfill(session))[("momentum", date(2026, 9, 30))]
    assert row.status == UNRESOLVED
    assert "rules disagree" in row.reason


def test_stamped_rows_are_reported_and_never_planned(session):
    rows = _by_key(plan_backfill(session))
    assert rows[("momentum", date(2026, 10, 3))].status == STAMPED
    stale = rows[("momentum", date(2026, 10, 1))]
    assert stale.status == DISAGREES
    assert stale.current == date(2026, 9, 29)
    assert stale.proposed == date(2026, 9, 30)


def test_duplicate_sessions_are_reported_with_whether_values_agree(session):
    plan = plan_backfill(session)
    duplicates = plan.duplicates()
    assert set(duplicates) == {
        (date(2026, 8, 7), (date(2026, 8, 8), date(2026, 8, 9))),
        (date(2026, 9, 4), (date(2026, 9, 5), date(2026, 9, 8))),
    }
    text_ = render(plan)
    assert "2026-09-04 Fri <- 2026-09-05 Sat, 2026-09-08 Tue" in text_
    assert "values identical" in text_
    assert "Unresolved rows (left NULL) (2)" in text_


def test_the_dry_run_writes_nothing(db_url, capsys):
    before = _dump(db_url)
    assert backfill.main(["--database-url", db_url]) == 0
    assert _dump(db_url) == before
    out = capsys.readouterr().out
    assert "Dry-run only" in out
    assert "12 rows" in out


def test_apply_fills_only_the_nulls_it_resolved(session, tmp_path):
    plan = plan_backfill(session)
    count, artifact = apply_backfill(
        session, plan, confirm=CONFIRMATION, artifact_dir=tmp_path / "audit"
    )
    assert count == len(plan.to_fill) == 8

    stored = {
        (r.portfolio, r.date): r.session_date
        for r in session.scalars(select(EquitySnapshot))
    }
    assert stored[("momentum", date(2026, 9, 8))] == date(2026, 9, 4)
    assert stored[("momentum", date(2026, 8, 9))] == date(2026, 8, 7)
    # Unresolved rows stay NULL; stamped rows keep their stamp, even the
    # one that disagrees with the rule.
    assert stored[("momentum", date(2026, 9, 29))] is None
    assert stored[("momentum", date(2026, 9, 30))] is None
    assert stored[("momentum", date(2026, 10, 3))] == date(2026, 10, 2)
    assert stored[("momentum", date(2026, 10, 1))] == date(2026, 9, 29)

    record = json.loads(artifact.read_text())
    assert len(record["rows"]) == 8
    assert len(record["unresolved"]) == 2


def test_apply_touches_no_other_column(db_url, session, tmp_path):
    before = {row[0]: row for row in _dump(db_url)}
    apply_backfill(
        session, plan_backfill(session), confirm=CONFIRMATION,
        artifact_dir=tmp_path,
    )
    with create_engine(db_url).connect() as connection:
        names = list(connection.execute(
            text("SELECT * FROM equity_snapshots")
        ).keys())
    position = names.index("session_date")
    for row in _dump(db_url):
        old = before[row[0]]
        assert row[:position] + row[position + 1:] == (
            old[:position] + old[position + 1:]
        )


def test_apply_is_idempotent(session, tmp_path):
    apply_backfill(
        session, plan_backfill(session), confirm=CONFIRMATION,
        artifact_dir=tmp_path,
    )
    again = plan_backfill(session)
    assert again.to_fill == ()
    assert apply_backfill(
        session, again, confirm=CONFIRMATION, artifact_dir=tmp_path
    ) == (0, None)


def test_apply_never_overwrites_a_row_stamped_since_the_plan(session, tmp_path):
    plan = plan_backfill(session)
    target = next(r for r in plan.to_fill if r.run_date == date(2026, 8, 4))
    session.execute(
        text("UPDATE equity_snapshots SET session_date = '2026-08-01' WHERE id = :id"),
        {"id": target.id},
    )
    session.commit()

    count, _ = apply_backfill(
        session, plan, confirm=CONFIRMATION, artifact_dir=tmp_path
    )
    assert count == len(plan.to_fill) - 1
    assert session.get(EquitySnapshot, target.id).session_date == date(2026, 8, 1)


def test_apply_requires_the_exact_confirmation(db_url, session, tmp_path):
    before = _dump(db_url)
    with pytest.raises(BackfillRefusedError, match="exact confirmation"):
        apply_backfill(
            session, plan_backfill(session), confirm="yes",
            artifact_dir=tmp_path,
        )
    assert _dump(db_url) == before


def test_apply_refuses_without_a_tty(db_url, monkeypatch):
    before = _dump(db_url)
    monkeypatch.setattr(backfill.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(
        "builtins.input",
        lambda *_: pytest.fail("must refuse before prompting"),
    )
    with pytest.raises(BackfillRefusedError, match="interactive TTY"):
        backfill.main(["--database-url", db_url, "--apply"])
    assert _dump(db_url) == before


def test_apply_with_a_tty_and_the_phrase_fills(db_url, monkeypatch, tmp_path):
    monkeypatch.setattr(backfill.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_: CONFIRMATION)
    assert backfill.main([
        "--database-url", db_url, "--apply",
        "--artifact-dir", str(tmp_path / "audit"),
    ]) == 0
    with Session(create_engine(db_url)) as s:
        assert plan_backfill(s).to_fill == ()
    assert list((tmp_path / "audit").glob("snapshot-session-backfill-*.json"))


# ------------------------------------------------- before the migration lands


@pytest.fixture()
def premigration_url(tmp_path, monkeypatch) -> str:
    url = f"sqlite:///{tmp_path / 'pre.db'}"
    monkeypatch.setenv("ALGO_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "007388445941")
    with create_engine(url).begin() as connection:
        connection.execute(text(
            "INSERT INTO equity_snapshots "
            "(portfolio, date, equity, cash, market_value, created_at) "
            "VALUES ('momentum', '2026-08-04', 100.0, 100.0, 0.0, "
            "'2026-08-03 20:16:00')"
        ))
    return url


def test_the_dry_run_copes_with_a_database_without_the_column(
    premigration_url, capsys
):
    assert backfill.main(["--database-url", premigration_url]) == 0
    out = capsys.readouterr().out
    assert "session_date column NOT present" in out
    assert "2026-08-04 Tue    2026-08-03 Mon" in out


def test_apply_refuses_a_database_without_the_column(premigration_url, monkeypatch):
    monkeypatch.setattr(backfill.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_: CONFIRMATION)
    with pytest.raises(BackfillRefusedError, match="alembic upgrade head"):
        backfill.main(["--database-url", premigration_url, "--apply"])
