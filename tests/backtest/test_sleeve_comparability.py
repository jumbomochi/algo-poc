"""Whether a sleeve's shadow can fairly grade that sleeve's live equity.

``ExecutionModel.is_like_for_like`` answers this for a pinned backtest artifact
and is left untouched: ``run_sleeve_evaluation.py`` still gates on it, D18's
accepted coverage bias still rests on it, and the tripwire at
``tests/backtest/test_bias_acceptance.py:271`` still holds.

It does not transfer to a rolling shadow, because all four of its requirements
are satisfied by construction or are inapplicable to that feed:

* **next-open fills** — the runner decides on today's close and fills entries
  the next session (``backtest/runner.py`` phases 1c and 2c). Same-bar is not
  reachable.
* **per-order commission floor** — ``CostModel()`` defaults to
  ``commission_minimum = 1.0``.
* **point-in-time universe** and **coverage floor** — the shadow passes no
  ``MembershipCalendar``. A 30-session window over live's *current* universe
  has no historical-membership question, so there are no membership-days to
  price. The 11.28% that blinded the monitor is a property of the 10-year
  artifact, not of this feed.

So this gate checks what can actually go wrong with a shadow instead: it was
produced for a different session than the one being graded, it came from a
different model than live is running, the sleeve never made it into the
replay, or there is not enough overlap to compute a return.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backtest.sleeve_comparability import SleeveComparability


SESSIONS = [date(2026, 8, 3) + timedelta(days=i) for i in range(6)]
TODAY = SESSIONS[-1]
LIVE_ID = "shadow:1111111111111111"


def _comparable(**overrides):
    kwargs = dict(
        sleeve="momentum",
        graded_session=TODAY,
        shadow_session=TODAY,
        shadow_id=LIVE_ID,
        live_model_id=LIVE_ID,
        overlapping_sessions=30,
    )
    kwargs.update(overrides)
    return SleeveComparability(**kwargs)


def test_a_matching_fresh_shadow_is_comparable() -> None:
    assert _comparable().is_comparable is True
    assert _comparable().unmet_requirements() == []


# ---------------------------------------------------------------------------
# staleness — the failure the artifact's session_date exists to catch
# ---------------------------------------------------------------------------


def test_a_shadow_produced_for_an_earlier_session_is_refused() -> None:
    """If 04:15 fails, yesterday's artifact is still on disk. Grading today's
    live against yesterday's model curve would be silent and confident."""
    verdict = _comparable(shadow_session=SESSIONS[-2])

    assert verdict.is_comparable is False
    assert any("session" in r for r in verdict.unmet_requirements())


def test_the_staleness_reason_names_both_dates() -> None:
    """An operator at 04:45 has to be able to see how stale, not just that."""
    reasons = " ".join(_comparable(shadow_session=SESSIONS[0]).unmet_requirements())

    assert str(SESSIONS[0]) in reasons
    assert str(TODAY) in reasons


# ---------------------------------------------------------------------------
# model identity
# ---------------------------------------------------------------------------


def test_a_shadow_from_a_different_model_is_refused() -> None:
    """A sleeve parameter changed, or the checkout moved, between the shadow
    being produced and the monitor running. The curves are not comparable and
    the evidence rows would land under the wrong baseline."""
    verdict = _comparable(shadow_id="shadow:2222222222222222")

    assert verdict.is_comparable is False
    assert any("model" in r for r in verdict.unmet_requirements())


def test_the_model_reason_names_both_ids() -> None:
    reasons = " ".join(
        _comparable(shadow_id="shadow:2222222222222222").unmet_requirements()
    )

    assert "shadow:2222222222222222" in reasons
    assert LIVE_ID in reasons


# ---------------------------------------------------------------------------
# the sleeve made it into the replay
# ---------------------------------------------------------------------------


def test_a_sleeve_absent_from_the_shadow_is_refused() -> None:
    """No live history to seed from, or the replay produced nothing."""
    verdict = _comparable(shadow_session=None)

    assert verdict.is_comparable is False
    assert any("no shadow" in r.lower() for r in verdict.unmet_requirements())


# ---------------------------------------------------------------------------
# enough overlap to compute a return
# ---------------------------------------------------------------------------


def test_a_single_overlapping_session_cannot_produce_a_return() -> None:
    """window_return needs two points; one is not a short window, it is no
    window, and grading it would report 0.0% as though it were measured."""
    verdict = _comparable(overlapping_sessions=1)

    assert verdict.is_comparable is False
    assert any("overlap" in r.lower() for r in verdict.unmet_requirements())


def test_zero_overlap_is_refused() -> None:
    assert _comparable(overlapping_sessions=0).is_comparable is False


def test_two_overlapping_sessions_is_enough() -> None:
    """Short, but computable. The monitor already renders 'Only N overlapping
    days' for a young book; that is a note, not a refusal."""
    assert _comparable(overlapping_sessions=2).is_comparable is True


# ---------------------------------------------------------------------------
# several at once
# ---------------------------------------------------------------------------


def test_every_unmet_requirement_is_reported_not_just_the_first() -> None:
    """An operator who fixes the one reason shown and re-runs, only to hit the
    next, learns to distrust the message."""
    verdict = _comparable(
        shadow_session=SESSIONS[0],
        shadow_id="shadow:2222222222222222",
        overlapping_sessions=1,
    )

    assert len(verdict.unmet_requirements()) == 3


def test_the_pinned_artifact_gate_is_not_imported_here() -> None:
    """Pins the approved blast radius, not merely today's behaviour.

    ``ExecutionModel.is_like_for_like`` still gates ``run_sleeve_evaluation.py``
    and still carries D18's accepted coverage bias. This gate answers a
    different question about a different feed. An import appearing here would
    re-couple the two answers, so that it becomes a deliberate reviewable act
    rather than something that happens by accident — the same protection
    ``test_bias_acceptance.py:271`` gives the divergence module.
    """
    import inspect

    import backtest.sleeve_comparability as module

    source = inspect.getsource(module)

    assert "from backtest.divergence import" not in source, (
        "sleeve_comparability now imports the pinned-artifact gate. That "
        "re-couples shadow comparability to D18's coverage acceptance, which "
        "was explicitly out of scope."
    )
