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
2. **It came from a different model.** A sleeve parameter changed, or the
   checkout moved, between the shadow being produced and the monitor running.
   The evidence rows would land under the wrong baseline.
3. **The sleeve is not in it.** No live history to seed from, or the replay
   produced nothing for it.
4. **There is not enough overlap to compute a return.** One shared session is
   not a short window, it is no window: ``window_return`` needs two points, and
   grading one would report 0.0% as though it had been measured.

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
    #: The session being graded — live's most recent.
    graded_session: date
    #: The session the shadow was produced for, or ``None`` when this sleeve is
    #: absent from the artifact entirely.
    shadow_session: date | None
    #: Model identity recorded in the artifact.
    shadow_id: str | None
    #: Model identity of the roster live is running right now.
    live_model_id: str
    #: Sessions present on both sides of the comparison.
    overlapping_sessions: int

    @property
    def is_comparable(self) -> bool:
        return not self.unmet_requirements()

    def unmet_requirements(self) -> list[str]:
        """Every reason this sleeve cannot be graded, in the monitor's words."""
        reasons: list[str] = []

        if self.shadow_session is None:
            reasons.append(
                f"no shadow curve for '{self.sleeve}': either the book has no "
                "equity history to seed the window from, or the replay produced "
                "nothing for it"
            )
        elif self.shadow_session != self.graded_session:
            reasons.append(
                f"shadow is stale: produced for session {self.shadow_session}, "
                f"but the session being graded is {self.graded_session}. The "
                "04:15 run most likely did not produce one today, leaving the "
                "previous day's artifact on disk"
            )

        if self.shadow_id != self.live_model_id:
            reasons.append(
                f"shadow came from a different model: artifact is "
                f"{self.shadow_id}, live is running {self.live_model_id}. A "
                "sleeve parameter or the checkout changed between the shadow "
                "being produced and this run"
            )

        if self.overlapping_sessions < MINIMUM_OVERLAPPING_SESSIONS:
            reasons.append(
                f"insufficient overlap: {self.overlapping_sessions} shared "
                f"session(s), need {MINIMUM_OVERLAPPING_SESSIONS} for a return "
                "to be defined"
            )

        return reasons
