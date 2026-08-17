"""Guards on the per-sleeve kill criteria and the two governance amendments (KAN-37).

``docs/operations/sleeve-kill-criteria.md`` is a rulebook, not a description:
the direction doc's universal rule is "no written kill criteria → no live
promotion", and these tests are what make that rule mechanical rather than
aspirational. Four ways the rulebook rots, each pinned below:

* **A sleeve goes live without criteria.** ``ACTIVE_SLEEVES`` is the list the
  paper runner and the risk service actually trade. Adding a seventh sleeve
  there without writing its kill criteria is exactly the failure the universal
  rule forbids, so the section set is read out of the code rather than
  hardcoded here — the test fails on the *code* change, before the sleeve ever
  reaches the account.
* **A budget stops being a number.** AC1 admits no "TBD" and no uncomputed
  formula. The budget is ``round(max-DD × 1.5, 2)``, so a hand-edited table
  where the two columns disagree is a silent loosening (or tightening) of a
  capital rule; the arithmetic is re-derived here.
* **The thresholds drift from the engine.** The 10-session divergence trigger
  and the 5-session blindness bound are enforced by
  ``shared/evidence_store.py``. If the constants move and the doc does not, an
  operator walks a rule the code will not agree with — so both are read from
  the module.
* **The budgets are provisional and someone forgets.** The numbers in the doc
  come from a survivorship-biased artifact because the point-in-time baseline
  has not been regenerated yet (KAN-23 shipped the machinery, not the run).
  While any row reads PROVISIONAL the doc must carry the arming block and cite
  the artifact it came from — otherwise a stale budget silently acquires the
  authority to arm real capital.

The checklist and IPS assertions guard the two amendments the same story makes:
a solo operator cannot satisfy the two-person gate as written, and the IPS's
§5 deployment path and the capital ladder cannot both govern.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from shared.evidence_store import (
    DEFAULT_BLINDNESS_INCIDENT_SESSIONS,
    DEFAULT_TRIGGER_SESSIONS,
)
from shared.universe import ACTIVE_SLEEVES

KILL_CRITERIA = Path("docs/operations/sleeve-kill-criteria.md")
CHECKLIST = Path("docs/operations/go-live-checklist.md")
IPS = Path("docs/operations/investment-policy-statement.md")
OPS_INDEX = Path("docs/operations/README.md")

#: The four D3.3 triggers. Every sleeve section must make all four concrete —
#: a section missing one is a rule with a hole an operator has to fill by
#: judgement, which is precisely what AC6 forbids.
TRIGGERS = ("Divergence", "Drawdown", "Signal staleness", "Safety incident")

BUDGET_ROW = re.compile(
    r"^\|\s*`(?P<sleeve>\w+)`\s*\|"
    r"\s*(?P<max_dd>[\d.]+)%\s*\|"
    r"\s*(?P<budget>[\d.]+)%\s*\|"
    r"\s*(?P<status>[A-Z]+)\s*\|",
    re.MULTILINE,
)


def _capital_allocations() -> dict[str, float]:
    """``CAPITAL_ALLOCATIONS`` from ``scripts/run_paper.py``, parsed not imported."""
    source = Path("scripts/run_paper.py").read_text()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "CAPITAL_ALLOCATIONS"
            for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("CAPITAL_ALLOCATIONS not found in scripts/run_paper.py")


def _sections(text: str) -> dict[str, str]:
    """Map ``### `sleeve``` heading -> that section's body."""
    parts = re.split(r"^### `(\w+)`\s*$", text, flags=re.MULTILINE)
    # parts[0] is the preamble; then (name, body) pairs.
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_every_active_sleeve_has_a_kill_criteria_section() -> None:
    """The six trading sleeves — and only those — carry written criteria."""
    documented = set(_sections(KILL_CRITERIA.read_text()))

    assert documented == set(ACTIVE_SLEEVES), (
        "sleeve-kill-criteria.md must document exactly the sleeves in "
        f"ACTIVE_SLEEVES. Missing: {sorted(set(ACTIVE_SLEEVES) - documented)}; "
        f"unexpected: {sorted(documented - set(ACTIVE_SLEEVES))}"
    )


def test_each_sleeve_section_makes_all_four_triggers_concrete() -> None:
    for sleeve, body in _sections(KILL_CRITERIA.read_text()).items():
        for trigger in TRIGGERS:
            assert trigger in body, f"{sleeve} section has no {trigger!r} trigger"
        assert "Demotion:" in body, f"{sleeve} section states no demotion action"


def test_every_drawdown_budget_is_a_number() -> None:
    text = KILL_CRITERIA.read_text()
    rows = {m.group("sleeve"): m for m in BUDGET_ROW.finditer(text)}

    assert set(rows) == set(ACTIVE_SLEEVES), (
        "the drawdown-budget table must have one row per active sleeve; got "
        f"{sorted(rows)}"
    )
    assert "TBD" not in text, "a kill budget left as TBD is not a kill budget"


def test_each_budget_is_its_backtest_max_drawdown_times_the_multiplier() -> None:
    """The default budget rule is max-DD × 1.5, re-derived rather than trusted."""
    for sleeve, row in {
        m.group("sleeve"): m for m in BUDGET_ROW.finditer(KILL_CRITERIA.read_text())
    }.items():
        max_dd = float(row.group("max_dd"))
        expected = round(max_dd * 1.5, 2)
        assert float(row.group("budget")) == expected, (
            f"{sleeve}: budget {row.group('budget')}% does not equal "
            f"max-DD {max_dd}% x 1.5 = {expected}%"
        )


def test_each_sleeve_section_states_the_weight_the_runner_actually_trades() -> None:
    """A demotion's rebalance is sized off these weights, so they cannot drift.

    ``CAPITAL_ALLOCATIONS`` is read out of ``scripts/run_paper.py`` rather than
    imported, because importing the paper runner pulls in its IB and database
    machinery for what is a two-line literal.
    """
    allocations = _capital_allocations()

    for sleeve, body in _sections(KILL_CRITERIA.read_text()).items():
        expected = f"{allocations[sleeve] * 100:.2f}%"
        assert f"**Weight:** {expected}" in body, (
            f"{sleeve} section must state its CAPITAL_ALLOCATIONS weight ({expected})"
        )


def test_trigger_thresholds_match_the_evidence_store_constants() -> None:
    """The doc's session counts are the ones ``breach_streak``/``blindness`` use."""
    text = KILL_CRITERIA.read_text()

    assert f"{DEFAULT_TRIGGER_SESSIONS} consecutive sessions" in text, (
        "the divergence trigger must quote DEFAULT_TRIGGER_SESSIONS "
        f"({DEFAULT_TRIGGER_SESSIONS}) — the count breach_streak() actually fires on"
    )
    assert f"{DEFAULT_BLINDNESS_INCIDENT_SESSIONS} consecutive" in text, (
        "the blindness bound must quote DEFAULT_BLINDNESS_INCIDENT_SESSIONS "
        f"({DEFAULT_BLINDNESS_INCIDENT_SESSIONS})"
    )


def test_provisional_budgets_carry_the_arming_block_and_cite_their_artifact() -> None:
    """Numbers off a non-like-for-like baseline may not authorize real capital."""
    text = KILL_CRITERIA.read_text()
    statuses = {m.group("sleeve"): m.group("status") for m in BUDGET_ROW.finditer(text)}

    if not any(status == "PROVISIONAL" for status in statuses.values()):
        return

    assert re.search(r"backtest_multi_\d{8}_\d{6}\.json", text), (
        "provisional budgets must cite the artifact filename they were derived from"
    )
    assert "do not authorize Rung 0" in text, (
        "while any budget is PROVISIONAL the doc must say, in terms, that these "
        "numbers do not authorize arming the live account"
    )


def test_cross_references_resolve() -> None:
    """Every relative link in the kill-criteria doc points at a file that exists."""
    text = KILL_CRITERIA.read_text()
    for target in re.findall(r"\]\((?!https?:)([^)#]+)", text):
        assert (KILL_CRITERIA.parent / target).resolve().exists(), (
            f"sleeve-kill-criteria.md links {target}, which does not exist"
        )


def test_kill_criteria_links_the_ladder_and_the_promotion_pipeline() -> None:
    text = KILL_CRITERIA.read_text()
    assert "project-direction.md" in text, (
        "the criteria must link the capital ladder / promotion pipeline they serve"
    )


def test_ops_index_lists_the_kill_criteria_doc() -> None:
    assert "sleeve-kill-criteria.md" in OPS_INDEX.read_text()


def test_checklist_replaces_two_person_approval_with_the_solo_substitute() -> None:
    """A gate no solo operator can satisfy is a gate that gets quietly skipped."""
    text = CHECKLIST.read_text()

    assert "sleeve-kill-criteria.md" in text, (
        "the checklist must link the kill-criteria doc (AC7)"
    )
    for part in (
        "drafted in writing",
        "adversarial review",
        "7-day cooling-off",
        "unresolved challenge",
    ):
        assert part in text, f"the D14 substitute does not define {part!r}"

    assert "Two-Person Approval" not in text, (
        "the section is amended, not appended to — a solo operator cannot sign "
        "a two-person gate, and leaving both in place lets the reader pick one"
    )
    assert text.count("## Gate Approval") == 1, (
        "the approval section must be amended exactly once (AC4)"
    )


def test_ips_records_the_ladder_supersession_and_its_standing_constraints() -> None:
    text = IPS.read_text()

    assert "superseded" in text.lower(), "§5 must be marked superseded by the ladder"
    assert "sleeve-kill-criteria.md" in text or "project-direction.md" in text, (
        "the IPS must point at the ladder that now governs deployment"
    )
    assert "capital-decision input" in text, (
        "the amendment must resolve the divergence monitor's 'not a kill switch' "
        "wording against the ladder, which makes it one by rule"
    )
    assert "30%" in text and "W-8BEN" in text, (
        "the standing constraints the ladder does not override must be restated"
    )


def test_ips_amendment_is_logged_in_the_amendment_table() -> None:
    """§9 requires every amendment to appear, dated, in the §11 log."""
    log = IPS.read_text().split("### Amendment log", 1)[1]
    assert "2026-08-17" in log, "the ladder amendment is not logged in §11"


def test_promotion_funding_rule_is_written_down() -> None:
    text = KILL_CRITERIA.read_text()
    assert "pro-rata" in text and "10% of portfolio per promotion" in text, (
        "the funding rule for a newly promoted sleeve must be recorded (AC5)"
    )
