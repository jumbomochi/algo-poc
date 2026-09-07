"""Guards on the dead-man switch runbook (docs/operations/dead-man-switches.md).

The runbook tells an operator how to configure the external checks. Its whole
value is that the schedules it prescribes match the jobs that actually ping, and
that claim had nothing holding it in place: on 2026-09-08 the note said

    ``run_paper.sh`` runs daily **including weekends** ... so a 26 h period
    pages after exactly one missed day.

while ``local.algo-paper-trading.plist`` had been ``Weekday`` 2-6 (Tue-Sat) all
along. The healthchecks.io check was configured from the note rather than from
the plist, so it went red every Sunday and stayed red through Monday — two false
pages a week on the one signal whose entire job is to be believed. Nothing
failed; the note was simply wrong and no test could tell.

So the runbook now carries an explicit **provider schedule** column holding a
cron expression, and these guards read the plists and require the two to agree.
Prose can still say whatever it likes around them; the machine-checkable cell is
what an operator copies into the provider.

Two properties are pinned:

* the **day-of-week set** must match the plist exactly. This is the defect that
  bit: Tue-Sat rendered as "daily" is invisible in prose and produces a check
  that cries wolf on a fixed weekly rhythm.

* the **hour** must match the plist. The minute is deliberately left free: a
  check-in is the *end* of a run, and every wrapper waits on a port before it
  pings, so the ping legitimately lands minutes after the calendar slot. The
  grace period absorbs that; the hour and the weekday set are what a
  misconfiguration gets wrong.

The roster itself is read out of ``secrets.sh`` for the same reason
``test_incident_2026_08_21_note.py`` does it: adding a seventh switch must not
leave a table that silently covers six.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

NOTE = Path("docs/operations/dead-man-switches.md")
SECRETS = Path("deploy/launchd/secrets.sh")
LAUNCHD = Path("deploy/launchd")

# The wrapper that pings each switch, and the plist whose calendar slot decides
# when that ping is due. Two switches have no launchd slot of their own and are
# excluded from the schedule comparison (but not from the roster check):
#   DEADMAN_WATCHDOG_URL  — pinged by Alertmanager on a 5-minute rule, not by a job.
#   (none others today)
SWITCH_TO_PLIST = {
    "ALGO_DEADMAN_PAPER_URL": "local.algo-paper-trading",
    "ALGO_DEADMAN_DIVERGENCE_URL": "local.algo-divergence-monitor",
    "ALGO_DEADMAN_REFRESH_URL": "local.algo-backtest-refresh",
    "ALGO_DEADMAN_BACKUP_URL": "local.algo-db-backup",
    "ALGO_DEADMAN_DIGEST_URL": "local.algo-evidence-digest",
}


def _declared_switches() -> list[str]:
    """The optional-secret roster, read out of the loader rather than restated."""
    text = SECRETS.read_text()
    match = re.search(
        r'ALGO_OPTIONAL_SECRET_NAMES="\$\{ALGO_OPTIONAL_SECRET_NAMES:-([^}]*)\}"',
        text,
    )
    assert match, "could not find ALGO_OPTIONAL_SECRET_NAMES in secrets.sh"
    return match.group(1).split()


def _plist_slots(label: str) -> list[dict]:
    """StartCalendarInterval, normalised to a list of dicts."""
    data = plistlib.loads((LAUNCHD / f"{label}.plist").read_bytes())
    slots = data.get("StartCalendarInterval")
    assert slots is not None, f"{label} has no StartCalendarInterval"
    return slots if isinstance(slots, list) else [slots]


def _plist_weekdays(label: str) -> set[int] | None:
    """The weekday set the job actually runs on, or None for 'every day'.

    launchd omits ``Weekday`` to mean daily. launchd and cron agree that
    Sunday is 0, so the sets are directly comparable.
    """
    slots = _plist_slots(label)
    if any("Weekday" not in slot for slot in slots):
        return None
    return {slot["Weekday"] % 7 for slot in slots}


def _plist_hours(label: str) -> set[int]:
    return {slot["Hour"] for slot in _plist_slots(label)}


def _documented_schedules() -> dict[str, str]:
    """Map env var -> the cron cell of the provider-schedule table.

    The table is found by its header rather than by line number so that editing
    the prose above it does not break the guard.
    """
    rows: dict[str, str] = {}
    for line in NOTE.read_text().splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        var = cells[1].strip("`")
        if var in SWITCH_TO_PLIST or var == "DEADMAN_WATCHDOG_URL":
            rows[var] = cells[2].strip("`")
    return rows


def _cron_field_to_set(field: str, span: range) -> set[int]:
    """Expand a cron field ('*', '2-6', '1', '1,3') into a set of ints."""
    if field == "*":
        return set(span)
    values: set[int] = set()
    for part in field.split(","):
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-"))
            values.update(range(lo, hi + 1))
        else:
            values.add(int(part))
    return values


def test_every_declared_switch_has_a_provider_schedule_row():
    """A switch the loader declares but the table omits is one an operator
    will never configure — which is indistinguishable from not having it."""
    documented = _documented_schedules()
    missing = [name for name in _declared_switches() if name not in documented]
    assert not missing, f"declared in secrets.sh but absent from the schedule table: {missing}"


def test_the_table_documents_no_switch_the_loader_does_not_declare():
    """The reverse drift: a row for a switch that no longer exists sends the
    operator to create a check nothing will ever ping."""
    declared = set(_declared_switches())
    extra = [name for name in _documented_schedules() if name not in declared]
    assert not extra, f"in the schedule table but not declared in secrets.sh: {extra}"


def test_documented_weekdays_match_the_plist():
    """The 2026-09-08 defect. Tue-Sat described as 'daily' produces a check
    that pages every Sunday and goes quiet every Saturday."""
    documented = _documented_schedules()
    for var, label in SWITCH_TO_PLIST.items():
        cron = documented[var]
        fields = cron.split()
        assert len(fields) == 5, f"{var}: {cron!r} is not a 5-field cron expression"
        documented_days = _cron_field_to_set(fields[4], range(0, 7))
        actual = _plist_weekdays(label)
        expected = set(range(0, 7)) if actual is None else actual
        assert documented_days == expected, (
            f"{var}: the note prescribes cron {cron!r} (days {sorted(documented_days)}) "
            f"but {label}.plist runs on days {sorted(expected)}"
        )


def test_documented_hours_match_the_plist():
    """The minute is free — a ping lands after the run — but the hour is not."""
    documented = _documented_schedules()
    for var, label in SWITCH_TO_PLIST.items():
        fields = documented[var].split()
        documented_hours = _cron_field_to_set(fields[1], range(0, 24))
        assert documented_hours == _plist_hours(label), (
            f"{var}: the note prescribes hour(s) {sorted(documented_hours)} "
            f"but {label}.plist fires at {sorted(_plist_hours(label))}"
        )


def test_the_note_warns_that_an_unpinged_check_never_alerts():
    """Both switches that had never been pinged sat grey and inert on
    2026-09-08 — importing the URL was not enough to arm them. An operator
    reading this runbook must be told that the first ping starts the clock."""
    text = NOTE.read_text().lower()
    assert "never been pinged" in text or "never pinged" in text, (
        "the runbook must state that a check with no first ping stays inert"
    )
