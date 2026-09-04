"""Can this sleeve's shadow fairly grade this sleeve's live equity?

:class:`~backtest.divergence.ExecutionModel` answers that for a **pinned
backtest artifact**, and is deliberately untouched here.
``scripts/run_sleeve_evaluation.py`` still gates on ``is_like_for_like``, D18's
accepted coverage bias still rests on it, and the tripwire in
``tests/backtest/test_bias_acceptance.py`` still holds. This module never
imports it, so re-coupling the two answers has to be a deliberate act.

It does not transfer to a rolling shadow, because every one of its four
requirements is satisfied by construction or is inapplicable to that feed:

* **next-open fills** — ``BacktestRunner.run`` decides on today's close and
  fills entries the *next* session (phase 2c decides, phase 1c fills, phase 1b
  exits at the next open). Same-bar is not reachable.
* **per-order commission floor** — ``CostModel()`` defaults to
  ``commission_minimum = 1.0``.
* **point-in-time universe** and **coverage floor** — the shadow passes no
  ``MembershipCalendar``, so there is no historical-membership question and no
  membership-days to price. The 11.28% exclusion that blinded the monitor is a
  property of the 10-year artifact, not of a 30-session window over live's
  current universe.

What *can* go wrong with a shadow is different, and this is the list:

1. **It is stale.** The 04:15 run failed and yesterday's artifact is still on
   disk, so today's live would be graded against yesterday's model curve.
2. **The sleeve is not in it.** No live history to seed from, or the replay
   produced nothing for it.
3. **There is not enough overlap to compute a return.** One shared session is
   not a short window, it is no window: ``window_return`` needs two points, and
   grading one would report 0.0% as though it had been measured.

Model identity is deliberately **not** checked here. It is enforced where it
already works: the artifact's ``shadow_id`` is written as
``divergence_daily.baseline_id``, and ``breach_streak`` requires that id and
treats rows under any other baseline as "history, not evidence for this epoch".
A model change therefore restarts the streak by construction, with no runtime
comparison that could rot. The monitor could not perform that comparison
honestly in any case: it holds no bars, so it cannot rebuild the live roster to
fingerprint it, and ``gate_epochs`` is empty, so there is no recorded epoch
baseline to anchor against either. A check with no source of truth is
decoration, and decoration on a safety gate is worse than an absence.

Every unmet requirement is reported, never just the first. An operator who
fixes the one reason shown and re-runs, only to meet the next, learns to
distrust the message.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

#: Sessions needed before a return is defined. ``window_return`` is
#: ``last / first - 1``, so two is the floor. Below it there is no measurement
#: to report, which is different from a measurement that happens to be small.
MINIMUM_OVERLAPPING_SESSIONS = 2


@dataclass(frozen=True)
class SleeveComparability:
    """Whether one sleeve's shadow may be graded against its live equity.

    Per-sleeve by design. The pinned-artifact gate was artifact-level, which is
    why a survivorship figure measured over 2016-2020 could blind an ETF sleeve
    that never touched the index. A sleeve missing from the shadow says nothing
    about the five that are in it.
    """

    sleeve: str
    #: The date this monitor run is happening on. Freshness is judged against
    #: this, not against a session date.
    run_date: date
    #: The wall-clock date the shadow was WRITTEN, or ``None`` when this sleeve
    #: is absent from the artifact entirely.
    #:
    #: The first version of this check compared SESSION dates and was wrong
    #: every day. ``equity_snapshots.date`` carries the run's SGT wall-clock
    #: date; the shadow's last bar is the last COMPLETE US session, which at
    #: 04:15 SGT is always the day before. They are one day apart by
    #: construction, so every sleeve was refused as stale, permanently — and
    #: since verdicts are dated by the compared window, the 2026-09-03 run
    #: wrote its NO_DATA rows over session 09-02 and left 09-03 with no
    #: evidence at all.
    shadow_produced_on: date | None
    #: The session actually being compared: the last one both series share.
    #: Reported so a reader knows what the verdict covers; it does NOT decide
    #: freshness.
    graded_session: date
    #: Sessions present on both sides of the comparison.
    overlapping_sessions: int

    @property
    def is_comparable(self) -> bool:
        return not self.unmet_requirements()

    def unmet_requirements(self) -> list[str]:
        """Every reason this sleeve cannot be graded, in the monitor's words."""
        reasons: list[str] = []

        if self.shadow_produced_on is None:
            reasons.append(
                f"no shadow curve for '{self.sleeve}': either the book has no "
                "equity history to seed the window from, or the replay produced "
                "nothing for it"
            )
        elif self.shadow_produced_on != self.run_date:
            reasons.append(
                f"shadow is stale: written on {self.shadow_produced_on}, but "
                f"this run is {self.run_date}. The 04:15 run most likely did "
                "not produce one today, leaving the previous day's artifact on "
                "disk"
            )

        if self.overlapping_sessions < MINIMUM_OVERLAPPING_SESSIONS:
            reasons.append(
                f"insufficient overlap: {self.overlapping_sessions} shared "
                f"session(s), need {MINIMUM_OVERLAPPING_SESSIONS} for a return "
                "to be defined"
            )

        return reasons
