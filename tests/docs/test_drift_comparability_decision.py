"""Guards on D19 — drift comparability is window-scoped and per-sleeve.

The divergence monitor used to grade live against a pinned 10-year backtest and
gate on ``ExecutionModel.is_like_for_like``. Both halves of that changed:

* the **feed** is now a rolling shadow, because a pinned artifact cannot score
  sessions past its own last bar (six consecutive runs in August 2026 all
  reported ``window_end=2026-08-14`` and rewrote the same evidence row);
* the **gate** is now :class:`~backtest.sleeve_comparability.SleeveComparability`,
  per sleeve, because every requirement of an execution model is satisfied by
  construction on a replay against live's own bars.

That leaves D18's stated reasoning out of date in a way that matters. D18
declined to move the coverage floor *because* ``is_like_for_like`` gated the
divergence monitor **and** ``run_sleeve_evaluation.py``. It now gates only the
second. The conclusion still holds; the argument for it is narrower, and a
reader must not be told the wider one.

Each claim below is pinned against the CODE, not just against other prose. A
decision record that drifts from what the system does is worse than none: it
reads as evidence.
"""
from __future__ import annotations

import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIRECTION = ROOT / "docs/designs/project-direction.md"


def _prose(path: Path) -> str:
    """Document text with runs of whitespace collapsed.

    These are hand-wrapped markdown files; a phrase that reads as one line to a
    human is often split across two in the source, so a raw substring check
    breaks the moment someone reflows a paragraph — a false alarm that teaches
    people to delete the guard.
    """
    return " ".join(path.read_text().split())


def _section(path: Path, heading: str) -> str:
    """Just one section's prose, so a document-wide match cannot stand in for it.

    Both of the assertions using this passed against the summary table and a
    cross-link before the section they describe had been written.
    """
    text = _prose(path)
    start = text.index(heading)
    # The next LEVEL-2 heading, not the next "## " substring: after whitespace
    # collapsing, "### The defect" contains "## " too, so a naive find stopped
    # at the first subsection and every assertion below it failed to see the
    # body it was checking.
    cursor = start + len(heading)
    while True:
        nxt = text.find("## ", cursor)
        if nxt == -1:
            return text[start:]
        if text[nxt - 1] != "#":
            return text[start:nxt]
        cursor = nxt + 3


# ---------------------------------------------------------------------------
# D19 is on the record
# ---------------------------------------------------------------------------


def test_the_decision_is_recorded_with_its_own_heading() -> None:
    """The `## ` prefix is load-bearing: without it this matched the anchor
    text of the cross-link in D18's amendment and passed with no section
    written at all."""
    assert "## Drift comparability is window-scoped and per-sleeve (D19)" in _prose(
        DIRECTION
    )


def test_the_decision_appears_in_the_decisions_table() -> None:
    """The table is what a reader scans; a section nobody is pointed to is
    documentation that only the author will ever find."""
    text = _prose(DIRECTION)

    assert "| D19 |" in text


def test_the_decision_names_the_defect_that_forced_it() -> None:
    """"We changed the feed" is not a decision record. The frozen window is the
    fact that made a pinned artifact untenable as a daily feed."""
    # Scoped to the section, not the whole document: the summary table row
    # also carries the date, so a document-wide search passed before the
    # section existed.
    section = _section(DIRECTION, "## Drift comparability is window-scoped")

    assert "2026-08-14" in section, "D19 must state the session the window froze at"


def test_the_two_baseline_state_is_stated() -> None:
    """The pin did not go away — it is still the baseline of record for edge
    evidence. A reader who finds two baselines must find out that is deliberate
    rather than assume one is stale."""
    section = _section(DIRECTION, "## Drift comparability is window-scoped")

    assert "run_sleeve_evaluation.py" in section
    assert "rolling shadow" in section


# ---------------------------------------------------------------------------
# D18's reasoning matches what the code now does
# ---------------------------------------------------------------------------


def test_d18_no_longer_claims_the_monitor_gates_on_like_for_like() -> None:
    """The specific sentence this change falsified.

    D18 declined to move the floor because is_like_for_like gated BOTH the
    divergence monitor and run_sleeve_evaluation.py. It gates only the second
    now, and a reader deciding whether to touch the floor must be told the
    argument that actually applies.
    """
    text = _prose(DIRECTION)

    # Backticks included deliberately: the doc writes the symbol as
    # `is_like_for_like`, and an assertion without them matches nothing and
    # passes vacuously — which is exactly what the first draft of this test did.
    assert "`is_like_for_like` gates both the divergence monitor" not in text, (
        "D18 still states the pre-D19 reasoning; is_like_for_like no longer "
        "gates the divergence monitor"
    )


def test_the_surviving_consumer_really_does_still_gate_on_it() -> None:
    """D18's amended floor reasoning rests entirely on this one consumer. If it
    ever stops gating, the floor has no stated defender left and D18 needs
    rewriting again — so the claim is pinned against the source."""
    source = (ROOT / "scripts/run_sleeve_evaluation.py").read_text()

    assert "is_like_for_like" in source, (
        "run_sleeve_evaluation.py no longer references is_like_for_like, so "
        "D18's amended reasoning for keeping the coverage floor is now unsupported"
    )


def test_the_monitor_really_does_gate_per_sleeve_now() -> None:
    """The doc's claim, checked against the code that implements it."""
    source = (ROOT / "scripts/divergence_monitor.py").read_text()

    assert "SleeveComparability" in source


def test_the_two_gates_stay_uncoupled() -> None:
    """D19's blast radius, pinned rather than trusted. Shadow comparability
    must not learn about D18's coverage acceptance by the back door — that
    coupling is exactly what D19 separates."""
    import backtest.sleeve_comparability as module

    assert "from backtest.divergence import" not in inspect.getsource(module)
