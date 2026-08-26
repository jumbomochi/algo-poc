"""Sessions on which no graded paper run happened — recorded, not backfilled.

The evidence store derives blindness from absence (``shared/models/evidence.py``):
a missing row on an NYSE trading day IS the signal, so a dead monitor cannot
hide by staying silent. That rule is right, and it has one gap: it cannot tell
an absence whose cause is known and accepted from one nobody has noticed yet.
On 2026-08-21 that gap cost three days — 2026-08-18 had been missing from
``equity_snapshots`` since the day it happened, and was found only by hand,
during an unrelated investigation.

This module closes it. It is the registry of sessions the record accepts as
permanently absent, each with its cause. :mod:`shared.evidence_store` reads it
so a blind session that is *not* listed here is reported as unexplained, which
is the case that needs a human.

THE DECISION (KAN-67, 2026-08-26)
---------------------------------
See :data:`DECISION`. In short: 2026-08-13 and 2026-08-18 are accepted as
permanent holes rather than backfilled, because ``scripts/run_paper.py`` has no
as-of date and building one would put look-ahead risk into the exact runner
that produces the gate evidence.

WHAT THIS REGISTRY DOES NOT DO
------------------------------
It does not change any grade. A listed session pauses the epoch clock exactly
as an unlisted blind session already does, and it still counts toward the
consecutive-blindness safety incident. If registering a date could suppress
that trigger, a real monitor outage could be laundered by adding a line to this
file. The registry classifies and reports; it never scores.

Adding an entry is therefore a claim about the past, reviewed like any other
code change, and it is the only sanctioned way to accept a gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

__all__ = [
    "DECISION",
    "KNOWN_ABSENT_SESSIONS",
    "AbsentSession",
    "absent_session",
    "absent_sessions_in",
    "known_absent_dates",
]


@dataclass(frozen=True)
class AbsentSession:
    """One NYSE trading day on which no graded run produced evidence."""

    session_date: date
    #: Why no run happened. Prose, because the next reader is a human deciding
    #: whether the gap is still acceptable.
    cause: str
    #: Where the incident is written up — a JIRA key, a story, or a log.
    reference: str

    def describe(self) -> str:
        return f"{self.session_date.isoformat()} ({self.cause} — {self.reference})"


DECISION = """\
KAN-67, decided 2026-08-26: accept 2026-08-13 and 2026-08-18 as permanent
evidence gaps (Option A). Neither is backfilled.

Two of the 9 NYSE sessions in 2026-08-11..2026-08-21 are absent, not one. Any
streak, continuity or epoch computation spanning that range must treat both as
absent — neither zero nor present.

Why not backfill (Option B): scripts/run_paper.py has no as-of date, so a dated
replay would have to be built first. That adds look-ahead risk to the one
runner whose output IS the gate evidence, and the row it produced would be
evidence of a simulation three days late, with different provenance from every
other row in equity_snapshots. An acknowledged absence is stronger evidence
than a reconstructed presence. Dated replay is worth building as a capability
if it is wanted for its own sake — never as a backfill.
"""


#: Ascending by date. Every entry must be an NYSE trading day — a holiday or
#: weekend entry would be silently inert, because ``blindness`` only ever asks
#: about dates the calendar yields. Asserted against the real calendar in
#: ``tests/shared/test_absent_sessions.py``.
KNOWN_ABSENT_SESSIONS: tuple[AbsentSession, ...] = (
    AbsentSession(
        session_date=date(2026, 8, 13),
        cause=(
            "the 04:15 paper run and 04:45 divergence monitor both aborted on a "
            ".env named pipe installed by 1Password, and every alert path "
            "self-disabled because [ -f ] is false for a FIFO"
        ),
        reference="KAN-16",
    ),
    AbsentSession(
        session_date=date(2026, 8, 18),
        cause=(
            "IB Gateway never came up on 7497 after the 23:55 auto-restart "
            "rejected the login, so the paper run aborted; the abort alerted "
            "but nothing reported the resulting gap for three days"
        ),
        reference="KAN-67",
    ),
)

_BY_DATE = {entry.session_date: entry for entry in KNOWN_ABSENT_SESSIONS}


def known_absent_dates() -> frozenset[date]:
    """Every registered absent session, as a set for membership tests."""
    return frozenset(_BY_DATE)


def absent_session(day: date) -> AbsentSession | None:
    """The registry entry for ``day``, or ``None`` if it is not registered."""
    return _BY_DATE.get(day)


def absent_sessions_in(start: date, end: date) -> list[AbsentSession]:
    """Registered absences in ``[start, end]``, inclusive, ascending."""
    return [
        entry
        for entry in KNOWN_ABSENT_SESSIONS
        if start <= entry.session_date <= end
    ]
