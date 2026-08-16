"""Guards on the approved-orders DLQ audit note (docs/operations/dlq-audit-2026-08.md).

The note is the durable record of a one-time audit (KAN-21): it says what was
in ``stream:approved_orders:dlq`` before anything was drained, and it carries
the operator's verification command. Two ways that record rots:

* the commands name a Redis key built from ``APPROVED_ORDERS_STREAM`` +
  ``DEAD_LETTER_SUFFIX``. Change either constant's *value* and the operator's
  post-check addresses a key that cannot exist — ``EXISTS`` returns 0 for the
  wrong key just as convincingly as for a drained one, so the check would
  "pass" while verifying nothing. Both halves are therefore read out of the
  code rather than hardcoded here;
* the note records one finding it deliberately did NOT fix (out of scope per
  the issue): no *depth* check covers the approved-orders DLQ, because
  risk-management's ``_check_dlq_depths`` watches the three streams it consumes
  and execution has no equivalent. If someone closes that gap, the note's
  open-findings section becomes false. Pinning it here means closing the gap
  forces the record to be updated rather than quietly going stale.

These are guards on the record, not on the (already completed) audit.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

from shared.redis_client import DEAD_LETTER_SUFFIX

NOTE = Path("docs/operations/dlq-audit-2026-08.md")
OPS_INDEX = Path("docs/operations/README.md")
ALERT_RULES = Path("config/alert_rules.yml")
RISK_RUNNER = Path("services/risk_management/runner.py")
EXECUTION_RUNNER = Path("services/execution/runner.py")


def _module_constant(path: Path, name: str) -> str:
    """Return the value of a module-level ``name = "literal"`` assignment.

    Parsed rather than imported: importing a service runner at collection time
    is expensive and drags in its whole dependency graph for one string.
    """
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            if not isinstance(node.value, ast.Constant) or not isinstance(
                node.value.value, str
            ):
                raise AssertionError(
                    f"{name} in {path} is no longer a string literal"
                )
            return node.value.value
    raise AssertionError(f"{name} not found at module level in {path}")


# Built from the code exactly the way the running services build it, so a
# rename of either half fails here instead of silently invalidating the
# operator's post-check.
APPROVED_ORDERS_DLQ = (
    _module_constant(EXECUTION_RUNNER, "APPROVED_ORDERS_STREAM")
    + DEAD_LETTER_SUFFIX
)


def _dlq_streams_checked_by_risk() -> set[str]:
    """Return the stream constants ``_check_dlq_depths`` iterates over.

    Parsed rather than imported so the guard costs nothing at collection time
    and cannot be satisfied by a same-named attribute elsewhere. Every element
    of the loop tuple must be a bare name: an added literal or dotted
    attribute is exactly how the gap could be closed without this guard
    noticing, so anything else raises rather than being filtered out.
    """
    tree = ast.parse(RISK_RUNNER.read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_check_dlq_depths"
        ):
            for inner in ast.walk(node):
                if isinstance(inner, ast.For) and isinstance(
                    inner.iter, ast.Tuple
                ):
                    unexpected = [
                        ast.dump(elt)
                        for elt in inner.iter.elts
                        if not isinstance(elt, ast.Name)
                    ]
                    if unexpected:
                        raise AssertionError(
                            "_check_dlq_depths now iterates over something "
                            f"other than bare stream constants: {unexpected}. "
                            "Recheck docs/operations/dlq-audit-2026-08.md's "
                            "open finding A against the new code."
                        )
                    return {elt.id for elt in inner.iter.elts}
    raise AssertionError(
        "_check_dlq_depths not found in services/risk_management/runner.py, "
        "or it no longer loops over a literal tuple of stream constants — "
        "docs/operations/dlq-audit-2026-08.md's open finding needs rechecking"
    )


def test_audit_note_is_listed_in_the_operations_index() -> None:
    assert NOTE.name in OPS_INDEX.read_text()


def test_note_addresses_the_dlq_key_the_code_actually_builds() -> None:
    """Every ``stream:*:dlq`` the note names must be the real approved-orders DLQ."""
    text = NOTE.read_text()
    named = set(re.findall(r"stream:[a-z_]+:dlq", text))

    assert APPROVED_ORDERS_DLQ in named, (
        f"the note must name {APPROVED_ORDERS_DLQ!r}, built from "
        "APPROVED_ORDERS_STREAM + DEAD_LETTER_SUFFIX"
    )
    # stream:fills:dlq is the 2026-08-10 precedent the note cites; anything
    # else means a constant moved under the note's feet.
    assert named <= {APPROVED_ORDERS_DLQ, "stream:fills:dlq"}, named


def test_note_quotes_the_live_alert_rule_and_threshold() -> None:
    """The note's claim about when the alert fires must match alert_rules.yml."""
    rules = yaml.safe_load(ALERT_RULES.read_text())
    dlq_rule = next(
        rule
        for group in rules["groups"]
        for rule in group["rules"]
        if rule.get("alert") == "DeadLetterQueueBacklog"
    )
    threshold = re.search(r">\s*(\d+)", dlq_rule["expr"]).group(1)

    text = NOTE.read_text()
    assert "DeadLetterQueueBacklog" in text
    # In context, not as a bare substring: the note is full of unrelated
    # numbers (dates, prices) that a bare `threshold in text` would match, so
    # a real threshold change could slip through green.
    assert f"> {threshold}" in text, (
        f"the note must state the live firing threshold as '> {threshold}'; "
        f"alert_rules.yml expr is {dlq_rule['expr']!r}"
    )


def test_open_finding_is_still_open() -> None:
    """No depth check covers the approved-orders DLQ.

    The note records this as an open finding left out of scope. If it gets
    closed, this test fails and the note must be corrected.
    """
    checked = _dlq_streams_checked_by_risk()

    assert "APPROVED_ORDERS_STREAM" not in checked, (
        "a depth check now covers the approved-orders DLQ — open finding A in "
        "docs/operations/dlq-audit-2026-08.md is stale and should be marked "
        "resolved"
    )
    assert checked == {
        "RECOMMENDATIONS_STREAM",
        "KILL_STREAM",
        "FILLS_STREAM",
    }, checked
