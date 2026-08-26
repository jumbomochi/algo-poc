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

* **the clock is honest.** The decision calls the bias "time-bounded", but
  that rests on forward capture having started. PR #91 connected
  ``data_ingestion`` to IB, so the mechanism is wired end-to-end — but
  ``ohlcv_daily`` is still empty (verified 2026-08-26) because the change has
  not reached a running container. The capture-clock test below tracks all
  three states (dark / wired / running), so the document can never claim more
  than the mechanism has actually done — nor less.
"""

from __future__ import annotations

import ast
import re
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


#: What D18 must say while the connect exists but no session has been captured.
CAPTURE_WIRED_MARKER = "WIRED, AWAITING FIRST SESSION"

#: What D18 must say while ``data_ingestion`` still cannot dial IB at all.
CAPTURE_DARK_MARKER = "NOT YET STARTED"


def _recorded_capture_start(text: str) -> str | None:
    """Return the ISO date D18 records as the capture start, or None.

    A real date is the terminal state: once one is written down the clock is
    running and both interim markers must be gone.
    """
    m = re.search(r"Capture start date:\s*(\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else None


def test_the_capture_clock_state_matches_the_code() -> None:
    """D18's capture-clock wording must track reality, in three states.

    The bias is only "time-bounded" once forward capture is actually running, so
    the document is not allowed to drift ahead of the mechanism. Three states,
    each pinned:

    1. **dark** — ``data_ingestion`` cannot dial IB. D18 must say NOT YET STARTED
       and name the fix. (Regression guard: if the connect is ever reverted, the
       document has to go back with it.)
    2. **wired, never run** — the connect exists but no session has been
       captured, so the remaining step is a deploy. D18 must say
       WIRED, AWAITING FIRST SESSION, and must NOT still claim NOT YET STARTED.
    3. **running** — a real ``Capture start date: YYYY-MM-DD`` is recorded. Both
       interim markers must be gone, and that date is the start of the 3-year
       re-evidence clock.

    State 3 cannot be asserted from CI — ``ohlcv_daily`` is not reachable here —
    so it is enforced negatively: the moment a date appears, the interim wording
    must not survive alongside it.
    """
    connects = _awaits_a_connect_call(INGEST_RUNNER)
    text = _prose(DIRECTION)
    recorded = _recorded_capture_start(text)

    if recorded is not None:
        # State 3: the clock is running.
        assert CAPTURE_DARK_MARKER not in text, (
            f"D18 records a capture start date ({recorded}) but still says "
            f"{CAPTURE_DARK_MARKER!r} — one of the two is wrong"
        )
        assert CAPTURE_WIRED_MARKER not in text, (
            f"D18 records a capture start date ({recorded}) but still says "
            f"{CAPTURE_WIRED_MARKER!r}; the interim wording must be replaced, "
            "not appended to"
        )
        return

    if connects:
        # State 2: wired, awaiting a session.
        assert CAPTURE_WIRED_MARKER in text, (
            "services/data_ingestion/runner.py now connects to IB, so capture is "
            f"wired and D18 must say {CAPTURE_WIRED_MARKER!r}. The remaining step "
            "is a deploy, not a code change."
        )
        assert CAPTURE_DARK_MARKER not in text, (
            f"capture is wired, so {CAPTURE_DARK_MARKER!r} is stale — D18 would "
            "understate how far the mechanism has got"
        )
        return

    # State 1: dark.
    assert CAPTURE_DARK_MARKER in text, (
        "capture is not wired, so D18 must continue to say so — otherwise the "
        "decision reads as time-bounded when it is open-ended"
    )
    assert "PR #91" in text
