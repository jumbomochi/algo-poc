"""Freshness must test when the shadow was WRITTEN, not what it covers.

The first version compared the shadow's ``session_date`` against the live
book's most recent ``equity_snapshots`` date. Those can never be equal in
production:

* ``equity_snapshots.date`` is stamped with the run's SGT wall-clock date;
* the shadow's last bar is the last COMPLETE US session, which at 04:15 SGT is
  always the previous calendar day.

Measured 2026-09-04: live said ``2026-09-04``, the shadow said ``2026-09-03``;
on 09-03, ``2026-09-03`` and ``2026-09-02``. Always exactly one day apart, so
every sleeve was refused as "stale" every single day, permanently — and because
the verdicts are dated by the compared window, the 09-03 run wrote its NO_DATA
rows over session 09-02 and left 09-03 with no evidence at all.

So the artifact records two different facts separately:

* ``session_date`` — the last market session the curve covers. Unchanged.
* ``produced_on`` — the wall-clock date the run wrote it. This is what
  freshness tests, because the question is "did today's 04:15 run make this",
  not "which session does it describe".
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backtest.shadow_artifact import dump_shadow, load_shadow
from backtest.sleeve_comparability import SleeveComparability


SESSIONS = [date(2026, 9, 1) + timedelta(days=i) for i in range(3)]
SERIES = {"momentum": {d: 20_000.0 for d in SESSIONS}}


def _write(tmp_path, *, produced_on, session_date=SESSIONS[-1]):
    path = tmp_path / "shadow.json"
    dump_shadow(path, series=SERIES, shadow_id="shadow:x", window_sessions=30,
                session_date=session_date, produced_on=produced_on)
    return path


# --- the artifact carries both facts ----------------------------------------


def test_the_artifact_records_when_it_was_written(tmp_path) -> None:
    path = _write(tmp_path, produced_on=date(2026, 9, 4))

    assert load_shadow(path).produced_on == date(2026, 9, 4)


def test_the_two_dates_are_independent(tmp_path) -> None:
    """The normal production case: written on the 4th, covering the 3rd."""
    path = _write(tmp_path, produced_on=date(2026, 9, 4),
                  session_date=date(2026, 9, 3))

    artifact = load_shadow(path)

    assert artifact.produced_on == date(2026, 9, 4)
    assert artifact.session_date == date(2026, 9, 3)


# --- freshness tests the run date, not the session --------------------------


def _verdict(**overrides):
    kwargs = dict(
        sleeve="momentum",
        run_date=date(2026, 9, 4),
        shadow_produced_on=date(2026, 9, 4),
        graded_session=date(2026, 9, 3),
        overlapping_sessions=30,
    )
    kwargs.update(overrides)
    return SleeveComparability(**kwargs)


def test_a_shadow_written_today_is_fresh_even_though_it_covers_yesterday() -> None:
    """The production case that was ALWAYS refused before."""
    verdict = _verdict()

    assert verdict.is_comparable is True, verdict.unmet_requirements()


def test_yesterdays_artifact_is_still_caught() -> None:
    """The case the check exists for: 04:15 failed, the old file remains."""
    verdict = _verdict(shadow_produced_on=date(2026, 9, 3))

    assert verdict.is_comparable is False
    assert any("stale" in r for r in verdict.unmet_requirements())


def test_the_staleness_reason_names_both_run_dates() -> None:
    reasons = " ".join(
        _verdict(shadow_produced_on=date(2026, 8, 30)).unmet_requirements()
    )

    assert "2026-08-30" in reasons
    assert "2026-09-04" in reasons


def test_a_sleeve_absent_from_the_shadow_is_still_refused() -> None:
    assert _verdict(shadow_produced_on=None).is_comparable is False


def test_insufficient_overlap_is_still_refused() -> None:
    assert _verdict(overlapping_sessions=1).is_comparable is False


def test_a_saturday_catch_up_for_fridays_session_is_fresh() -> None:
    """A catch-up run scores the last session that closed. Written today, so
    fresh — the session it covers being older is normal, not stale."""
    verdict = _verdict(
        run_date=date(2026, 9, 5), shadow_produced_on=date(2026, 9, 5),
        graded_session=date(2026, 9, 3),
    )

    assert verdict.is_comparable is True
