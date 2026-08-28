"""Is this baseline admissible evidence, given the biases we formally accepted?

:mod:`backtest.divergence` answers a stricter question — *could live execution
have matched this backtest?* — and ``ExecutionModel.is_like_for_like`` is its
yes/no. That question has exactly one right answer and this module does not
touch it.

Admissibility is a different question, and D18 is why it needs asking. The
point-in-time baseline cannot meet the 5% coverage floor from IB data (11.28%
of membership-days are unpriceable, and the shortfall is not repairable in
code), so on 2026-08-26 the bias was accepted in writing rather than paid for
with vendor history. The floor was deliberately *not* moved: affected artifacts
still report ``BLOCKED`` and ``is_like_for_like`` False, because that flag gates
the divergence monitor too and relaxing it would change what every future run
accepts as evidence.

That left the D10 verdicts (KAN-55) with no way to run at all. The evaluation
refuses a non-like-for-like baseline, and its only override —
``--allow-non-comparable-baseline`` — is a blanket escape hatch that stamps the
run gate-invalid and refuses to spend the holdout of record. Correct for a
same-bar baseline; useless for one whose single defect was decided about in
advance.

So there are three answers here, not two, and the value is in how little the
middle one excuses:

``VALID``
    Nothing needed excusing.
``VALID_WITH_ACCEPTED_BIAS``
    The *only* unmet requirement is the coverage floor, and
    ``research/bias_acceptances.json`` accepts it **for this exact artifact**,
    pinned by sha256, with figures that still match what the artifact reports.
``INVALID``
    Everything else — including a same-bar baseline holding a valid acceptance,
    because a same-bar baseline is not the accepted bias plus a detail. It is a
    different defect that nobody decided about.

Two rules carry most of that weight:

* **only the coverage floor is excusable.** Fills, the commission floor and the
  point-in-time universe are checked against the unmodified
  :class:`~backtest.divergence.ExecutionModel`, so an acceptance can never
  launder them.
* **``MISSING`` coverage is not acceptable.** You can accept a bias you
  measured and wrote down; accepting one that was never measured would let an
  artifact with no coverage block inherit D18's decision, which is the
  "absence reads as a pass" failure the coverage states exist to prevent.

Acceptance is a commit, not a keystroke — the registry follows
``research/holdout_registry.json``'s honour-system property, where the file's
git history is the evidence that a bias was accepted *before* it was spent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from backtest.divergence import ExecutionModel
from backtest.membership import COVERAGE_BLOCKED, COVERAGE_OK

#: Admissibility states. These are strings rather than a bool because the middle
#: one has to survive into the artifact: a reader who finds ``true`` learns
#: nothing about what was excused, and D18 turns on that being visible.
ADMISSIBLE = "VALID"
ADMISSIBLE_WITH_ACCEPTED_BIAS = "VALID_WITH_ACCEPTED_BIAS"
INADMISSIBLE = "INVALID"

#: The only requirement an acceptance may excuse. Named rather than implied so
#: that adding a second one is a deliberate, reviewable act.
REQUIREMENT_COVERAGE_FLOOR = "coverage_floor"

ACCEPTANCE_REGISTRY_PATH = (
    Path(__file__).resolve().parents[1] / "research" / "bias_acceptances.json"
)

#: The registry carries the human-readable two-decimal figure that D18's prose
#: also carries; the artifact carries full float precision. They must agree to
#: within a hundredth of a percentage point -- close enough to prove they
#: describe the same measurement, tight enough to catch a drifted baseline.
_PCT_TOLERANCE = 0.01


@dataclass(frozen=True)
class BiasAcceptance:
    """One formally accepted baseline bias, as committed to the registry.

    ``accepted_at`` is caller-supplied and therefore backdatable. It is here for
    readers, not as evidence -- the evidence is the registry's git history.
    """

    decision: str
    requirement: str
    source_sha256: str
    excluded_pct: float
    floor_pct: float
    accepted_at: str
    re_evidence: str
    doc: str
    direction: str = ""
    note: str = ""


@dataclass(frozen=True)
class AdmissibilityVerdict:
    """Whether a baseline may be cited, and what was excused to get there."""

    state: str
    unmet_requirements: list[str] = field(default_factory=list)
    accepted_bias: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def is_admissible(self) -> bool:
        """True for both admissible states.

        Callers must not test the state string for truthiness: every one of the
        three is a non-empty string, so ``if verdict.state:`` is always true and
        ``INVALID`` would read as a pass.
        """
        return self.state in (ADMISSIBLE, ADMISSIBLE_WITH_ACCEPTED_BIAS)


def load_acceptances(path: Path | str = ACCEPTANCE_REGISTRY_PATH) -> list[BiasAcceptance]:
    """Read the committed acceptance registry.

    A missing registry is an empty list, not an error: no acceptances means
    nothing is excused, which is the safe reading.
    """
    registry = Path(path)
    if not registry.exists():
        return []
    payload = json.loads(registry.read_text())
    return [
        BiasAcceptance(
            decision=str(entry["decision"]),
            requirement=str(entry["requirement"]),
            source_sha256=str(entry["source_sha256"]),
            excluded_pct=float(entry["excluded_pct"]),
            floor_pct=float(entry["floor_pct"]),
            accepted_at=str(entry.get("accepted_at", "")),
            re_evidence=str(entry.get("re_evidence", "")),
            doc=str(entry.get("doc", "")),
            direction=str(entry.get("direction", "")),
            note=str(entry.get("note", "")),
        )
        for entry in payload.get("acceptances", [])
    ]


def resolve_admissibility(
    model: ExecutionModel,
    *,
    source_sha256: str,
    coverage: Mapping[str, Any] | None,
    acceptances: Sequence[BiasAcceptance],
) -> AdmissibilityVerdict:
    """Resolve whether ``model``'s baseline may be cited as evidence.

    Args:
        model: The execution model read from the baseline's own config.
        source_sha256: Checksum of the baseline artifact. An acceptance is
            pinned to one artifact, so this is what makes it non-transferable.
        coverage: The artifact's ``config.coverage`` block, used to check that
            the accepted figures still describe what the artifact reports.
        acceptances: Committed acceptances, normally from
            :func:`load_acceptances`.
    """
    if model.is_like_for_like:
        return AdmissibilityVerdict(state=ADMISSIBLE)

    unmet = model.unmet_requirements()

    # Everything except coverage, taken from the unmodified model so the
    # messages have one source of truth. Neutralising coverage isolates the
    # other three rather than string-matching them out of the list.
    blocking = replace(model, coverage_state=COVERAGE_OK).unmet_requirements()
    if blocking:
        return AdmissibilityVerdict(
            state=INADMISSIBLE,
            unmet_requirements=unmet,
            notes=[
                "an accepted bias covers the coverage floor only; "
                f"{len(blocking)} other requirement(s) are unmet and were "
                "never accepted"
            ],
        )

    if model.coverage_state != COVERAGE_BLOCKED:
        # Reachable only for MISSING: OK would have been like-for-like above.
        return AdmissibilityVerdict(
            state=INADMISSIBLE,
            unmet_requirements=unmet,
            notes=[
                "coverage was never measured, so there is no measured bias to "
                "accept; re-run the baseline with --universe-snapshots"
            ],
        )

    notes: list[str] = []
    for acceptance in acceptances:
        if acceptance.requirement != REQUIREMENT_COVERAGE_FLOOR:
            continue
        if acceptance.source_sha256 != source_sha256:
            notes.append(
                f"acceptance {acceptance.decision} is pinned to sha256 "
                f"{acceptance.source_sha256[:12]}..., which is not this "
                f"artifact's {source_sha256[:12]}..."
            )
            continue
        if coverage is None:
            notes.append(
                f"acceptance {acceptance.decision} matches this artifact by "
                "sha256, but the artifact declares no coverage block, so the "
                "accepted figures cannot be checked against it"
            )
            continue
        mismatch = _figures_mismatch(acceptance, coverage)
        if mismatch:
            notes.append(
                f"acceptance {acceptance.decision} matches this artifact by "
                f"sha256 but {mismatch}"
            )
            continue
        return AdmissibilityVerdict(
            state=ADMISSIBLE_WITH_ACCEPTED_BIAS,
            accepted_bias=_stamp(acceptance, model),
            notes=notes,
        )

    if not notes:
        notes.append(
            "universe coverage is BLOCKED and no accepted bias in "
            f"{ACCEPTANCE_REGISTRY_PATH.name} covers this artifact"
        )
    return AdmissibilityVerdict(
        state=INADMISSIBLE, unmet_requirements=unmet, notes=notes
    )


def _figures_mismatch(
    acceptance: BiasAcceptance, coverage: Mapping[str, Any]
) -> str | None:
    """Why the accepted figures do not describe ``coverage``, or None."""
    try:
        excluded = float(coverage["excluded_pct"])
        floor = float(coverage["floor_pct"])
    except (KeyError, TypeError, ValueError):
        return "the artifact's coverage block has no readable figures to check"

    if abs(excluded - acceptance.excluded_pct) > _PCT_TOLERANCE:
        return (
            f"the artifact's exclusion of {excluded:.2f}% differs from the "
            f"accepted {acceptance.excluded_pct:.2f}% -- what was accepted is a "
            "measured number, not the BLOCKED state"
        )
    if abs(floor - acceptance.floor_pct) > _PCT_TOLERANCE:
        return (
            f"the artifact's floor of {floor:.2f}% differs from the accepted "
            f"{acceptance.floor_pct:.2f}% -- the decision rests on the floor "
            "being unmoved"
        )
    return None


def _stamp(acceptance: BiasAcceptance, model: ExecutionModel) -> dict[str, Any]:
    """What the artifact carries so a verdict can cite the bias from the file."""
    return {
        "decision": acceptance.decision,
        "requirement": acceptance.requirement,
        "coverage_state": model.coverage_state,
        "excluded_pct": acceptance.excluded_pct,
        "floor_pct": acceptance.floor_pct,
        "source_sha256": acceptance.source_sha256,
        "accepted_at": acceptance.accepted_at,
        "direction": acceptance.direction,
        "re_evidence": acceptance.re_evidence,
        "doc": acceptance.doc,
    }
