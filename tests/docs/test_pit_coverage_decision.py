"""Guards on the accepted PIT coverage bias (D18, KAN-59).

2026-08-26: the point-in-time baseline cannot meet the 5% coverage floor from IB
data — the measured figure is 11.28% and the shortfall is not repairable in code.
Rather than buy vendor history, the bias was accepted in writing, bounded by
forward capture, and required to be cited wherever it is spent.

That decision is only safe while three things stay true, so each is pinned here:

* **the floor is not moved.** Accepting the bias in prose and then quietly
  relaxing ``DEFAULT_COVERAGE_FLOOR_PCT`` would be the very thing the decision
  says it is not doing — and because ``is_like_for_like`` gates both the
  divergence monitor and ``run_sleeve_evaluation.py``, it would change what every
  future run accepts as evidence, not just this one artifact;

* **the numbers stay attached to the claim.** A documented bias whose size has
  drifted out of the document is not documented. ``output/`` is gitignored, so
  CI cannot re-derive them from the artifact — they are pinned here instead, and
  the doc must carry them;

* **the clock is honest.** The decision calls the bias "time-bounded", but that
  rests on forward capture having started, and it has not: ``data_ingestion``
  still never dials IB (PR #91), so ``ohlcv_daily`` stays empty. The doc says so
  explicitly. When #91 lands, the capture-start test below fails on purpose, to
  force the real start date into the document rather than leaving a stale
  "NOT YET STARTED" behind.
"""

from __future__ import annotations

import ast
from pathlib import Path

from backtest.membership import DEFAULT_COVERAGE_FLOOR_PCT


ROOT = Path(__file__).resolve().parents[2]
DIRECTION = ROOT / "docs/designs/project-direction.md"
BACKLOG = ROOT / "docs/superpowers/plans/2026-08-12-readiness-closure-story-backlog.md"
INGEST_RUNNER = ROOT / "services/data_ingestion/runner.py"


def _prose(path: Path) -> str:
    """Document text with runs of whitespace collapsed to single spaces.

    These are hand-wrapped markdown files. A phrase that reads as one line to a
    human is frequently split across two in the source, so a raw substring check
    fails the moment someone reflows a paragraph — a false alarm that teaches
    people to delete the guard. Normalising makes the assertions test the words,
    not the line breaks.
    """
    return " ".join(path.read_text().split())


# Measured on output/backtest_multi_20260819_183451.json (KAN-52, 2026-08-19).
EXCLUDED_PCT = "11.28"
FLOOR_PCT = 5.0
TOTAL_MEMBERSHIP_DAYS = "1,265,893"
EXCLUDED_MEMBERSHIP_DAYS = "142,856"
RE_EVIDENCE_YEARS = "3 years"


def test_the_coverage_floor_was_not_moved() -> None:
    """The decision's central claim. If this fails, the doc is lying."""
    assert DEFAULT_COVERAGE_FLOOR_PCT == FLOOR_PCT, (
        f"backtest/membership.py's coverage floor is now "
        f"{DEFAULT_COVERAGE_FLOOR_PCT}, not {FLOOR_PCT}. D18 in "
        "docs/designs/project-direction.md states the bias was accepted in "
        "writing rather than by relaxing the gate. Either revert the floor or "
        "rewrite D18 — it cannot stand as written."
    )


def test_the_decision_states_the_number_being_accepted() -> None:
    """A bias documented without its size is not documented."""
    text = _prose(DIRECTION)

    assert "The accepted PIT coverage bias (D18)" in text
    for figure in (
        EXCLUDED_PCT,
        TOTAL_MEMBERSHIP_DAYS,
        EXCLUDED_MEMBERSHIP_DAYS,
        "backtest_multi_20260819_183451.json",
    ):
        assert figure in text, (
            f"D18 must state {figure!r} — the measured coverage shortfall and "
            "the artifact it came from. output/ is gitignored, so this document "
            "is the only durable record of it."
        )

    # The direction of the bias matters as much as its size: excluding departed
    # members flatters returns, it does not merely add noise.
    assert "survivorship-biased upward" in text


def test_the_re_evidence_trigger_is_written_down() -> None:
    text = _prose(DIRECTION)

    assert RE_EVIDENCE_YEARS in text, (
        f"D18 must name the re-evidence trigger ({RE_EVIDENCE_YEARS} of forward "
        "capture). A bias with no trigger is permanent, whatever the prose says."
    )
    assert "Re-evidence trigger" in text


def test_the_d10_verdicts_must_cite_the_limitation() -> None:
    """KAN-55 spends a single-use holdout; it may not do so silently."""
    text = _prose(DIRECTION)

    assert "must cite this limitation" in text
    assert "KAN-55" in text
    assert "incumbent_sleeves_2026" in text


def test_the_backlog_plan_points_at_the_decision() -> None:
    """The floor is defined there; a reader must be able to find why it stands."""
    text = _prose(BACKLOG)

    assert "Baseline coverage floor (D14)" in text
    assert "D18" in text, (
        "the coverage-floor bullet must reference D18, or a reader who finds the "
        "5% floor has no way to learn it is currently unmet and why"
    )


def _awaits_a_connect_call(path: Path) -> bool:
    """True if the module awaits something's ``.connect(...)``.

    Parsed rather than grepped: ``runner.py`` contains the word "connect" in
    prose comments (e.g. "when two clients connect with the same id"), so a
    substring check reports a connection that does not exist.
    """
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {
            "connect",
            "ensure_ib_connected",
        }:
            return True
        if isinstance(func, ast.Name) and func.id == "ensure_ib_connected":
            return True
    return False


def test_capture_has_not_started_so_the_doc_must_still_say_so() -> None:
    """Open finding. This test failing is GOOD NEWS and means: update the doc.

    D18 calls the bias time-bounded, which is only true once forward capture is
    running. It is not: ``data_ingestion`` constructs an ``IBClient`` and never
    dials it, so ``ohlcv_daily`` sits at 0 rows (verified 2026-08-26). PR #91 is
    the fix.

    When #91 lands, this fails — deliberately. Record the first captured
    session's date in D18 as the start of the 3-year clock, and replace the
    "NOT YET STARTED" wording. Do not simply delete this test: change it to pin
    the recorded start date instead.
    """
    connects = _awaits_a_connect_call(INGEST_RUNNER)
    text = _prose(DIRECTION)

    if connects:
        raise AssertionError(
            "services/data_ingestion/runner.py now connects to IB, so forward "
            "capture can start and D18's 'NOT YET STARTED' is stale. Record the "
            "first captured session date in docs/designs/project-direction.md "
            "as the start of the 3-year re-evidence clock, then update this test."
        )

    assert "NOT YET STARTED" in text, (
        "capture still has not started, so D18 must continue to say so — "
        "otherwise the decision reads as time-bounded when it is open-ended"
    )
    assert "PR #91" in text
