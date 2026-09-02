"""The weekly digest reports sessions where only part of the book was graded.

``evidence_store.Blindness.partial_sessions`` justified not pausing the epoch
clock on the grounds that a partial is "a data-quality problem the digest
surfaces separately". It was not: the field was read by nothing but tests, so
the degradation it recorded reached no human. A degradation nobody can see is
indistinguishable from health.
"""
from __future__ import annotations

from datetime import date

from scripts.ops.evidence_digest import PartialReport, _partial_line


DAYS = [date(2026, 8, 3 + i) for i in range(3)]


def test_nothing_is_reported_when_every_session_was_fully_graded() -> None:
    """A clean week must stay quiet, or the line becomes noise to scroll past."""
    assert _partial_line(None) == []
    assert _partial_line(PartialReport([], 21, {})) == []


def test_a_partial_week_is_reported() -> None:
    line = _partial_line(PartialReport(DAYS, 21, {"earnings_drift": 3}))

    assert line, "a week with partial sessions produced no digest line"


def test_the_line_counts_the_partial_sessions_against_the_week() -> None:
    text = " ".join(_partial_line(PartialReport(DAYS, 21, {"earnings_drift": 3})))

    assert "3" in text and "21" in text, text


def test_the_line_names_the_sleeves_and_how_often() -> None:
    """The count says something is wrong; the names say what to look at."""
    text = " ".join(_partial_line(
        PartialReport(DAYS, 21, {"earnings_drift": 3, "tail_risk_hedge": 1})
    ))

    assert "earnings_drift" in text
    assert "tail_risk_hedge" in text


def test_the_worst_offender_is_named_first() -> None:
    """Scanning a digest at 08:00, the first name should be the one that
    matters most."""
    text = " ".join(_partial_line(
        PartialReport(DAYS, 21, {"tail_risk_hedge": 1, "earnings_drift": 3})
    ))

    assert text.index("earnings_drift") < text.index("tail_risk_hedge"), text


def test_the_line_does_not_claim_the_monitor_was_blind() -> None:
    """Blind and partial are different states with different remedies, and the
    digest already has a 🚨 BLIND line for the former."""
    text = " ".join(_partial_line(PartialReport(DAYS, 21, {"earnings_drift": 3})))

    assert "BLIND" not in text


# ---------------------------------------------------------------------------
# reaching a rendered digest
# ---------------------------------------------------------------------------


def test_the_partial_line_reaches_the_rendered_digest() -> None:
    """The whole point: the field existed and was read by nothing, so the
    degradation it recorded never reached a human."""
    from scripts.ops.evidence_digest import DigestSnapshot, render_digest

    snapshot = DigestSnapshot(
        as_of=DAYS[-1], window_start=DAYS[0], epoch=None, blind=None,
        partial=PartialReport(DAYS, 21, {"earnings_drift": 3}),
        sleeves=[], equity=None, dlq={}, alerts=None, drills_due=[],
    )

    text = render_digest(snapshot)

    assert "PARTIAL" in text
    assert "earnings_drift" in text


def test_a_clean_week_renders_no_partial_line() -> None:
    from scripts.ops.evidence_digest import DigestSnapshot, render_digest

    snapshot = DigestSnapshot(
        as_of=DAYS[-1], window_start=DAYS[0], epoch=None, blind=None,
        partial=None, sleeves=[], equity=None, dlq={}, alerts=None,
        drills_due=[],
    )

    assert "PARTIAL" not in render_digest(snapshot)
