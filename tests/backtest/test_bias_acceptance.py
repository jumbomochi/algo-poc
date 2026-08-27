"""Admissibility of a baseline whose bias has been formally accepted (KAN-68).

D18 (KAN-59) accepted the PIT coverage bias *in the record* and deliberately did
not move the coverage floor: affected artifacts still report ``BLOCKED`` and
``is_like_for_like`` False. That left KAN-55 unrunnable — its old AC1 wanted
``coverage.state == OK``, which D18 made unreachable, and the only override
(``--allow-non-comparable-baseline``) is a blanket escape hatch that stamps the
run gate-invalid and refuses to spend the holdout of record.

This module is the narrow path between those two. It answers one question — *is
this artifact admissible evidence?* — with three answers instead of two, and the
whole point is how little it excuses:

* only the **coverage floor** can be excused, and only for the exact artifact
  named in ``research/bias_acceptances.json`` by sha256;
* same-bar fills, a missing commission floor, and a static present-day universe
  are refused with an acceptance in hand, because those are not what was
  accepted;
* ``MISSING`` coverage cannot be accepted at all — you can only accept a bias
  you measured.

``ExecutionModel.is_like_for_like`` is untouched by design, so the divergence
monitor cannot change behaviour as a side effect. That property is pinned here
too, since it is the reason this module exists as a separate layer rather than
as a fourth branch inside ``ExecutionModel``.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from backtest.bias_acceptance import (
    ACCEPTANCE_REGISTRY_PATH,
    ADMISSIBLE,
    ADMISSIBLE_WITH_ACCEPTED_BIAS,
    INADMISSIBLE,
    REQUIREMENT_COVERAGE_FLOOR,
    BiasAcceptance,
    load_acceptances,
    resolve_admissibility,
)
from backtest.divergence import ExecutionModel
from backtest.membership import (
    COVERAGE_BLOCKED,
    COVERAGE_MISSING,
    COVERAGE_OK,
)

ROOT = Path(__file__).resolve().parents[2]

# The KAN-52 PIT baseline D18 was measured on. ``output/`` is gitignored, so the
# sha is pinned here rather than re-derived; the artifact-backed check below
# skips when the file is absent (CI) and asserts when it is present (the
# operator's machine, where it matters).
KAN52_SHA = "19e130ad8d572ea9eb167df499a3a94b6545f86acb392785a69498b65f480136"
KAN52_ARTIFACT = ROOT / "output/backtest_multi_20260819_183451.json"

OTHER_SHA = "0" * 64


def _model(**overrides) -> ExecutionModel:
    """A baseline that is like-for-like in every respect but coverage."""
    fields = {
        "fill_model": "next_open",
        "commission_minimum": 1.0,
        "point_in_time_universe": True,
        "coverage_state": COVERAGE_BLOCKED,
    }
    fields.update(overrides)
    return ExecutionModel(**fields)


def _coverage(excluded_pct: float = 11.284998021159765, floor_pct: float = 5.0) -> dict:
    """The ``config.coverage`` block as the artifact carries it."""
    return {
        "state": COVERAGE_BLOCKED,
        "total_membership_days": 1265893,
        "excluded_membership_days": 142856,
        "excluded_pct": excluded_pct,
        "floor_pct": floor_pct,
    }


def _acceptance(**overrides) -> BiasAcceptance:
    """The committed D18 acceptance, overridable per test."""
    fields = {
        "decision": "D18",
        "requirement": REQUIREMENT_COVERAGE_FLOOR,
        "source_sha256": KAN52_SHA,
        "excluded_pct": 11.28,
        "floor_pct": 5.0,
        "accepted_at": "2026-08-26",
        "re_evidence": "3 years of forward capture",
        "doc": "docs/designs/project-direction.md",
    }
    fields.update(overrides)
    return BiasAcceptance(**fields)


def _resolve(model=None, *, sha=KAN52_SHA, coverage=None, acceptances=None):
    return resolve_admissibility(
        model if model is not None else _model(),
        source_sha256=sha,
        coverage=_coverage() if coverage is None else coverage,
        acceptances=[_acceptance()] if acceptances is None else acceptances,
    )


# --------------------------------------------------------------------------
# The two states that existed before this module
# --------------------------------------------------------------------------


def test_a_baseline_that_needs_no_excuse_is_plainly_valid() -> None:
    verdict = _resolve(
        _model(coverage_state=COVERAGE_OK),
        coverage={"state": COVERAGE_OK, "excluded_pct": 1.0, "floor_pct": 5.0},
        acceptances=[],
    )

    assert verdict.state == ADMISSIBLE
    assert verdict.accepted_bias is None
    assert verdict.unmet_requirements == []


def test_coverage_blocked_with_no_acceptance_at_all_is_inadmissible() -> None:
    """The pre-KAN-68 behaviour, unchanged when the registry is empty."""
    verdict = _resolve(acceptances=[])

    assert verdict.state == INADMISSIBLE
    assert verdict.accepted_bias is None
    assert any("coverage" in r for r in verdict.unmet_requirements)


# --------------------------------------------------------------------------
# The new state
# --------------------------------------------------------------------------


def test_coverage_blocked_with_a_matching_acceptance_is_valid_with_accepted_bias() -> None:
    verdict = _resolve()

    assert verdict.state == ADMISSIBLE_WITH_ACCEPTED_BIAS
    assert verdict.unmet_requirements == []


def test_the_accepted_bias_stamp_carries_what_a_reader_needs_to_judge_it() -> None:
    """A verdict citing this run must be able to cite the bias from the artifact.

    D18: "a verdict that spends the single-use holdout without that citation is
    not a valid verdict". If the stamp omits the figure, the citation has to be
    re-derived by hand from a gitignored 261MB file.
    """
    stamp = _resolve().accepted_bias

    assert stamp["decision"] == "D18"
    assert stamp["requirement"] == REQUIREMENT_COVERAGE_FLOOR
    assert stamp["coverage_state"] == COVERAGE_BLOCKED
    assert stamp["excluded_pct"] == pytest.approx(11.28, abs=0.01)
    assert stamp["floor_pct"] == 5.0
    assert stamp["source_sha256"] == KAN52_SHA
    assert "3 years" in stamp["re_evidence"]
    assert "project-direction" in stamp["doc"]


# --------------------------------------------------------------------------
# The narrowing rules — what an acceptance must NEVER excuse
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"fill_model": "same_bar"}, "same_bar"),
        ({"commission_minimum": 0.0}, "commission floor"),
        ({"point_in_time_universe": False}, "universe"),
    ],
)
def test_an_acceptance_never_excuses_a_requirement_other_than_coverage(
    overrides, expected_reason
) -> None:
    """The property that makes this different in kind from the blanket override.

    A same-bar or static-universe baseline is not "the accepted bias plus a
    detail" — it is a different, unaccepted defect. If any of these three
    resolved admissible, the narrow path would have become the blanket one.
    """
    verdict = _resolve(_model(**overrides))

    assert verdict.state == INADMISSIBLE
    assert verdict.accepted_bias is None
    assert any(expected_reason in r for r in verdict.unmet_requirements)


def test_coverage_that_was_never_measured_cannot_be_accepted() -> None:
    """``MISSING`` is not a weaker ``BLOCKED`` — it is an unknown.

    You can accept a bias you measured and wrote down. Accepting one you never
    measured would let a baseline with no coverage block at all inherit D18's
    acceptance, which is the "absence reads as a pass" failure the coverage
    states were introduced to prevent.
    """
    verdict = _resolve(
        _model(coverage_state=COVERAGE_MISSING),
        coverage=None,
    )

    assert verdict.state == INADMISSIBLE
    assert verdict.accepted_bias is None
    assert any("never measured" in r or "MISSING" in r for r in verdict.unmet_requirements)


def test_an_acceptance_for_a_different_artifact_does_not_carry() -> None:
    """Acceptance is pinned to one sha256, so it cannot bless the next baseline."""
    verdict = _resolve(sha=OTHER_SHA)

    assert verdict.state == INADMISSIBLE
    assert verdict.accepted_bias is None
    assert any("sha256" in n for n in verdict.notes)


def test_an_acceptance_for_a_different_requirement_does_not_excuse_coverage() -> None:
    verdict = _resolve(acceptances=[_acceptance(requirement="fill_model")])

    assert verdict.state == INADMISSIBLE
    assert verdict.accepted_bias is None


@pytest.mark.parametrize(
    "drift",
    [
        {"excluded_pct": 30.0},
        {"floor_pct": 15.0},
    ],
)
def test_an_acceptance_whose_figures_disagree_with_the_artifact_does_not_carry(
    drift,
) -> None:
    """What was accepted is a measured number, not the category ``BLOCKED``.

    D18 accepted 11.28% against a 5.00% floor. An artifact that has since
    drifted to 30% exclusion is a different, larger bias that nobody decided
    about — and a floor that has moved means the decision's central claim
    ("the floor is not moved") no longer holds.
    """
    verdict = _resolve(coverage=_coverage(**drift))

    assert verdict.state == INADMISSIBLE
    assert verdict.accepted_bias is None
    assert any("disagree" in n or "differs" in n for n in verdict.notes)


# --------------------------------------------------------------------------
# The blast radius stays where the design put it
# --------------------------------------------------------------------------


def test_the_execution_model_still_reports_the_blocked_baseline_as_not_comparable() -> None:
    """D18: "The bias is accepted in this record, *not* by relaxing the gate."

    ``is_like_for_like`` keeps its four strict requirements. Admissibility is a
    layer above it, so a reader who opens the artifact still sees False.
    """
    assert _model().is_like_for_like is False


def test_the_divergence_monitor_does_not_consume_the_acceptance_registry() -> None:
    """Pins the approved blast radius, not merely today's behaviour.

    The divergence monitor's own gate is ``is_like_for_like``, and it must keep
    reporting NO_DATA against a coverage-BLOCKED baseline. Whether it should
    ever learn this tri-state is a separate, undecided question — so an import
    appearing here is a design change that needs its own decision, and a plain
    behavioural test would not catch it being added.
    """
    import backtest.divergence as divergence

    source = inspect.getsource(divergence)
    assert "bias_acceptance" not in source, (
        "backtest/divergence.py now references the acceptance registry. That "
        "un-blinds the daily divergence monitor as a side effect of KAN-68, "
        "which was explicitly out of scope — see the story's Out of scope."
    )


# --------------------------------------------------------------------------
# The committed registry
# --------------------------------------------------------------------------


def test_the_committed_registry_accepts_d18_for_the_kan52_baseline() -> None:
    acceptances = load_acceptances(ACCEPTANCE_REGISTRY_PATH)

    d18 = [a for a in acceptances if a.decision == "D18"]
    assert len(d18) == 1, "expected exactly one D18 acceptance"
    entry = d18[0]
    assert entry.requirement == REQUIREMENT_COVERAGE_FLOOR
    assert entry.source_sha256 == KAN52_SHA
    assert entry.excluded_pct == pytest.approx(11.28, abs=0.01)
    assert entry.floor_pct == 5.0


def test_the_registry_note_says_the_commit_is_the_evidence() -> None:
    """Same honour-system property ``holdout_registry.json`` states about itself.

    ``accepted_at`` is caller-supplied and therefore backdatable; the evidence
    that a bias was accepted before it was spent is this file's git history.
    """
    registry = json.loads(ACCEPTANCE_REGISTRY_PATH.read_text())

    assert "commit" in registry["note"]


@pytest.mark.skipif(
    not KAN52_ARTIFACT.exists(),
    reason="output/ is gitignored; the baseline is only present on the operator's machine",
)
def test_the_committed_acceptance_matches_the_real_baseline_artifact() -> None:
    """The one check CI cannot run, and the one that would catch a wrong sha.

    Everything else about the registry is self-consistent by construction. This
    is where a typo in the 64-char sha, or a re-run that silently replaced the
    artifact, actually surfaces.
    """
    import hashlib

    digest = hashlib.sha256()
    with KAN52_ARTIFACT.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)

    payload = json.loads(KAN52_ARTIFACT.read_text())
    coverage = payload["config"]["coverage"]
    entry = [
        a for a in load_acceptances(ACCEPTANCE_REGISTRY_PATH) if a.decision == "D18"
    ][0]

    assert digest.hexdigest() == entry.source_sha256
    assert coverage["state"] == COVERAGE_BLOCKED
    assert coverage["excluded_pct"] == pytest.approx(entry.excluded_pct, abs=0.01)
    assert coverage["floor_pct"] == entry.floor_pct
