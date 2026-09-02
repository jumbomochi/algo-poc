"""A session where part of the book was graded must not read as full health.

``blindness()`` had two shapes of partial coverage and treated them very
differently:

* **rows missing** — a sleeve wrote nothing. Classified ``partial``.
* **rows present, mixed verdicts** — every sleeve wrote a row, some graded and
  some ``NO_DATA``. Classified as *nothing at all*: it fell through to the
  final ``else`` and landed in no bucket, so an epoch could run its whole
  length with one of six sleeves ever graded and report
  ``longest_consecutive=0, is_safety_incident=False``.

The second shape is the one the rolling shadow actually produces, because the
monitor now scores a sleeve it cannot grade as a recorded ``NO_DATA`` rather
than skipping it (a missing row means the monitor did not run; a recorded
NO_DATA means it ran and could not judge, and the store draws a hard line
between them).

The epoch clock is deliberately NOT paused by either shape — one lagging
sleeve must not extend an epoch indefinitely. Visibility is the fix, not a
stall.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.evidence_store import blindness
from shared.models.base import Base
from shared.models.evidence import DivergenceDaily


SLEEVES = ["momentum", "sector_rotation", "earnings_drift"]
# 2026-08-03..07 is Mon-Fri, five NYSE sessions.
DAYS = [date(2026, 8, 3 + i) for i in range(5)]
BASELINE = "shadow:aaaabbbbccccdddd"


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'evidence.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _add(session, sleeve: str, day: date, status: str) -> None:
    session.add(DivergenceDaily(
        sleeve=sleeve, session_date=day, baseline_id=BASELINE, status=status,
        window_sessions=30, threshold=0.2,
        created_at=datetime.now(timezone.utc),
    ))


def _blindness(session):
    return blindness(
        session, start=DAYS[0], end=DAYS[-1],
        sleeves=SLEEVES, baseline_id=BASELINE,
    )


def test_a_session_with_mixed_verdicts_is_partial_not_invisible(db) -> None:
    """The headline. Every sleeve reported; only one could be graded."""
    for sleeve, status in zip(SLEEVES, ["OK", "NO_DATA", "NO_DATA"]):
        _add(db, sleeve, DAYS[0], status)
    for day in DAYS[1:]:
        for sleeve in SLEEVES:
            _add(db, sleeve, day, "OK")
    db.commit()

    result = _blindness(db)

    assert result.partial_sessions == [DAYS[0]]


def test_a_fully_graded_session_is_not_partial(db) -> None:
    """No false positives: a healthy day must stay silent."""
    for day in DAYS:
        for sleeve in SLEEVES:
            _add(db, sleeve, day, "OK")
    db.commit()

    assert _blindness(db).partial_sessions == []


def test_a_warning_is_a_grade_not_a_gap(db) -> None:
    """WARNING and BREACH are verdicts. Only NO_DATA is an ungraded sleeve."""
    for day in DAYS:
        for sleeve, status in zip(SLEEVES, ["OK", "WARNING", "BREACH"]):
            _add(db, sleeve, day, status)
    db.commit()

    assert _blindness(db).partial_sessions == []


def test_every_sleeve_no_data_is_still_no_data_not_partial(db) -> None:
    """The whole book ungraded is a different state, and it already pauses the
    clock. A partial must not swallow it."""
    for day in DAYS:
        for sleeve in SLEEVES:
            _add(db, sleeve, day, "NO_DATA")
    db.commit()

    result = _blindness(db)

    assert result.no_data_sessions == DAYS
    assert result.partial_sessions == []


def test_missing_rows_are_still_partial(db) -> None:
    """The pre-existing shape keeps its classification."""
    for day in DAYS:
        _add(db, "momentum", day, "OK")
    db.commit()

    assert _blindness(db).partial_sessions == DAYS


def test_a_partial_session_does_not_pause_the_epoch_clock(db) -> None:
    """Deliberate, and load-bearing: counting a partial as blindness would let
    one lagging sleeve extend an epoch indefinitely (evidence_store.py's own
    reasoning). Visibility is the fix, not a stall."""
    for day in DAYS:
        for sleeve, status in zip(SLEEVES, ["OK", "NO_DATA", "NO_DATA"]):
            _add(db, sleeve, day, status)
    db.commit()

    result = _blindness(db)

    assert result.partial_sessions == DAYS
    assert result.longest_consecutive == 0
    assert result.is_safety_incident is False


def test_the_ungraded_sleeves_are_named_per_session(db) -> None:
    """A count tells an operator something is wrong; the names tell them what
    to go and look at."""
    for sleeve, status in zip(SLEEVES, ["OK", "NO_DATA", "OK"]):
        _add(db, sleeve, DAYS[0], status)
    for day in DAYS[1:]:
        for sleeve in SLEEVES:
            _add(db, sleeve, day, "OK")
    db.commit()

    assert _blindness(db).ungraded_by_sleeve == {"sector_rotation": 1}
