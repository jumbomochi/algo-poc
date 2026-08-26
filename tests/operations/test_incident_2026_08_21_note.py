"""Guards on the 2026-08-21 incident note (docs/operations/incident-2026-08-21-gateway-and-docker.md).

The note is the durable record of two overlapping faults: an IB login rejection
at the 23:55 IBC auto-restart, and a dead Docker engine. Its value is entirely in
being *accurate about the code as it stood*, so these guards pin the claims that
would silently become false if the code moved underneath them.

Four ways this record rots:

* the note says the paper run is at **04:15** and that the auto-restart lands
  4h20m before it. Move the run and the arithmetic in the note is wrong while
  reading plausibly, so the run time is read out of the plist rather than
  hardcoded here;

* the note argues at length that ``Weekday`` 2-6 is **Tuesday-Saturday and
  correct by design**, because the 04:15 SGT run covers the US session that
  closed at 04:00 SGT that morning. An earlier pass of the investigation got this
  wrong and over-reported the evidence gaps by two days per fortnight. If someone
  "fixes" the plists to Mon-Fri, that whole section becomes actively misleading
  rather than merely stale;

* the note recorded two findings it deliberately did NOT fix, each owned by its
  own ticket: the auth branch of ``gateway_watchdog.sh`` clearing the two-strike
  marker (KAN-62), and ``run_paper.py`` having no as-of date, which is why
  2026-08-18 cannot be backfilled (KAN-67). Closing either makes the note's
  analysis historical, and it should be corrected in the same change. KAN-62
  landed on 2026-08-26, so its guard is now inverted: the note carries a
  "Resolved" block, and the defect must not come back;

* the note claims **all six** dead-man switches are unarmed and lists them by
  name. Add a seventh and "all six" is false, so the roster is read out of
  ``secrets.sh``.

These are guards on the record, not on the (already recovered) incident.
"""

from __future__ import annotations

import ast
import plistlib
import re
from pathlib import Path

NOTE = Path("docs/operations/incident-2026-08-21-gateway-and-docker.md")
OPS_INDEX = Path("docs/operations/README.md")
PAPER_PLIST = Path("deploy/launchd/local.algo-paper-trading.plist")
DIVERGENCE_PLIST = Path("deploy/launchd/local.algo-divergence-monitor.plist")
WATCHDOG = Path("deploy/launchd/gateway_watchdog.sh")
SECRETS = Path("deploy/launchd/secrets.sh")
RUN_PAPER = Path("scripts/run_paper.py")

# launchd weekdays the two daily trading jobs run on. 2-6 is Tuesday-Saturday
# and is deliberate: see the note's "Weekday 2-6 is correct" section.
EXPECTED_WEEKDAYS = {2, 3, 4, 5, 6}

# Flag spellings that would mean run_paper.py had gained a dated-replay mode.
DATE_FLAGS = {"--as-of", "--as_of", "--date", "--session-date", "--asof"}


def _calendar_entries(plist: Path) -> list[dict]:
    """Return StartCalendarInterval as a list, whether it is one dict or many."""
    with plist.open("rb") as fh:
        parsed = plistlib.load(fh)
    interval = parsed["StartCalendarInterval"]
    return interval if isinstance(interval, list) else [interval]


def _run_paper_option_strings() -> set[str]:
    """Return every option string passed to add_argument in run_paper.py.

    Parsed rather than imported: importing the runner at collection time drags
    in its whole dependency graph, and ``--help`` would need a live DB URL.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(RUN_PAPER.read_text())):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
    if not found:
        raise AssertionError(
            f"no add_argument option strings parsed out of {RUN_PAPER} — the "
            "parser was restructured and this guard needs rewriting"
        )
    return found


def test_note_is_listed_in_the_operations_index() -> None:
    assert NOTE.name in OPS_INDEX.read_text()


def test_note_states_the_paper_run_time_the_plist_actually_schedules() -> None:
    """The note's 04:15 and its 4h20m arithmetic must match the live schedule."""
    entries = _calendar_entries(PAPER_PLIST)
    hours = {e["Hour"] for e in entries}
    minutes = {e["Minute"] for e in entries}
    assert len(hours) == 1 and len(minutes) == 1, (
        f"the paper job no longer runs at a single time of day ({hours}, "
        f"{minutes}) — recheck the note's timeline and its 4h20m gap"
    )
    stamp = f"{hours.pop():02d}:{minutes.pop():02d}"

    assert stamp in NOTE.read_text(), (
        f"the note must state the scheduled run time as {stamp!r}; if the run "
        "moved, its timeline and the 4h20m gap to AutoRestartTime are both stale"
    )


def test_tuesday_to_saturday_is_still_the_schedule() -> None:
    """The note's "this is correct, do not fix it" section must stay true.

    If the plists move to Mon-Fri, the note's argument about SGT-to-US session
    mapping describes a schedule that no longer exists, and anyone reading it
    will mis-count the evidence gaps.
    """
    for plist in (PAPER_PLIST, DIVERGENCE_PLIST):
        weekdays = {e["Weekday"] for e in _calendar_entries(plist)}
        assert weekdays == EXPECTED_WEEKDAYS, (
            f"{plist.name} now runs on weekdays {sorted(weekdays)}, not "
            f"{sorted(EXPECTED_WEEKDAYS)} (Tue-Sat). The "
            '"Weekday 2-6 is correct" section of '
            f"{NOTE.name} is now wrong and must be rewritten."
        )


def test_the_auth_branch_no_longer_clears_the_strike_marker() -> None:
    """KAN-62's defect, closed 2026-08-26 — and it must stay closed.

    The note explained that the auth-failure branch ran ``rm -f "$MARKER"`` and
    so reset the two-strike counter belonging to the kickstart path, costing an
    extra cycle every time the auth condition cleared. This guard used to assert
    the defect was still present, so that closing it would force the note to be
    corrected. It has been, so the assertion is now the other way round: the
    line must not come back, and the note must still say so.

    The behavioural proof lives in
    tests/deploy/test_gateway_watchdog.py::test_a_port_down_then_auth_then_clear_sequence_needs_no_extra_grace_pass.
    This one guards the *record*.
    """
    text = WATCHDOG.read_text()
    # Isolate the auth branch precisely. Slicing only on the trailing comment
    # would leave the recovery path's `rm -f "$MARKER" "$AUTH_MARKER"` (line 72)
    # inside the slice, and that satisfies the substring check on its own — the
    # guard would pass after KAN-62 removed the line it is meant to watch.
    # "Port is down. FIRST" opens the branch and appears exactly once. The grep
    # pattern itself is NOT a usable anchor: "Too many failed login attempts"
    # also appears in the file's header comment, so splitting on it starts the
    # slice at line 11 and swallows the recovery path.
    try:
        auth_branch = text.split("Port is down. FIRST")[1].split(
            "No auth failure in evidence"
        )[0]
    except IndexError as exc:  # pragma: no cover - structural change
        raise AssertionError(
            "could not locate the auth-failure branch in gateway_watchdog.sh; "
            f"the file was restructured and this guard needs rewriting: {exc}"
        ) from exc

    assert 'rm -f "$MARKER" "$AUTH_MARKER"' not in auth_branch, (
        "the slice leaked the recovery path — this guard can no longer tell "
        "line 128 from line 72 and must be rewritten"
    )

    assert 'rm -f "$MARKER"' not in auth_branch, (
        "the auth-failure branch of gateway_watchdog.sh is clearing $MARKER "
        "again. That counter belongs to the kickstart path; clearing it here "
        "cost an extra ~5 min cycle of downtime on 2026-08-21 (KAN-62)."
    )

    resolved = NOTE.read_text().split("Why the watchdog did not fix it")[1]
    assert "Resolved 2026-08-26" in resolved.split("## ")[0], (
        f"{NOTE.name}'s watchdog analysis describes the pre-KAN-62 code and no "
        "longer says so. A reader will take it as current."
    )


def test_open_finding_run_paper_still_has_no_as_of_date() -> None:
    """KAN-67 turns on this. If dated replay lands, 08-18 becomes backfillable.

    The note's central claim about the 2026-08-18 gap is that it *cannot* be
    backfilled, because the runner always executes against the present.
    """
    options = _run_paper_option_strings()
    gained = options & DATE_FLAGS

    assert not gained, (
        f"run_paper.py has gained {sorted(gained)} — dated replay now exists, "
        f"so {NOTE.name}'s claim that 2026-08-18 cannot be backfilled is stale, "
        "and KAN-67's decision should be revisited"
    )


def test_note_names_every_dead_man_switch_the_loader_declares() -> None:
    """The note says "all six" and lists them. The roster must still be six."""
    line = next(
        ln
        for ln in SECRETS.read_text().splitlines()
        if ln.startswith("ALGO_OPTIONAL_SECRET_NAMES=")
    )
    names = re.findall(r"\b(?:ALGO_)?DEADMAN[A-Z_]*URL\b", line)
    # De-duplicated because the line names each once as a default value.
    roster = sorted(set(names))

    text = NOTE.read_text()
    missing = [n for n in roster if n not in text]
    assert not missing, (
        f"{NOTE.name} does not name these dead-man switches: {missing}. "
        "The loader declares them, so the note's roster is incomplete."
    )
    assert len(roster) == 6, (
        f"the dead-man roster is now {len(roster)} entries ({roster}), not 6 — "
        f'{NOTE.name} says "all six" and must be updated'
    )
