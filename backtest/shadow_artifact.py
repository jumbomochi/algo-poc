"""Serialising the rolling shadow between the 05:15 and 05:45 jobs.

The 05:15 paper run already holds the fetched bars, the sleeve configs and the
risk engines, so it produces the shadow (``backtest.shadow_series``) for about
0.06s of work on data it has anyway, and writes it here. The 05:45 monitor
reads it. The alternative — a second IB historical fetch at 05:45 — was
rejected because the gateway is the dependency that has already killed
scheduled runs twice.

That split has one deliberate consequence worth stating: the monitor's feed now
depends on the 05:15 job. A **missing** artifact therefore means the paper run
did not happen, which is exactly the blind signal
``shared.evidence_store.blindness`` derives from absence. It must never be read
as "no sleeves were gradeable", so :func:`load_shadow` raises rather than
returning an empty result.

``shadow_id`` is the load-bearing field. ``breach_streak`` keys evidence rows on
``(sleeve, session_date, baseline_id)`` and treats rows under any other baseline
as history rather than evidence. A shadow regenerated nightly under a fresh id
would scatter every session into its own baseline, no streak could reach the
10-session trigger, and the monitor would be as useless as the frozen pin it
replaces. So the id is derived from the **model** — the sleeve roster and each
sleeve's parameters — and is stable night to night. A parameter change moves it,
which is what direction-doc D13 means when it requires a baseline change to
restart the epoch.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from backtest.shadow_series import SHADOW_WARMUP_SESSIONS
from services.risk_management.funding import DEFAULT_SLEEVE_CASH_BUFFER_BPS

#: Prefix so a reader of an evidence row can tell a rolling-shadow identity from
#: a pinned-artifact one at a glance. The two mean different things and must
#: never be mistakable.
SHADOW_ID_PREFIX = "shadow:"

#: Length of the truncated digest. Full sha256 in an evidence row is noise; 16
#: hex chars is 64 bits, far past collision risk for a handful of models.
_DIGEST_CHARS = 16

#: How live is dated when this shadow is graded against it. ``session_date``
#: since KAN-103: live keyed by the US session each snapshot valued. Before
#: it, live was keyed by the SGT run date and every compared pair was one
#: session apart (docs/decisions/divergence-session-dating-2026-10.md). The
#: two are different comparisons, so the version is part of the model: every
#: verdict after the switch files under a new ``baseline_id``, and
#: ``breach_streak`` treats the run-date-keyed rows as history.
SHADOW_DATING_VERSION = "session_date"

#: How the replay funds its entries (KAN-111). ``sleeve_cash``: an entry is
#: capped at the sleeve's own cash, net of entries already queued that session,
#: and same-session sell proceeds do not count — exactly what the paper run and
#: the fill projector allow. Before it the replay could run a sleeve's cash
#: negative up to the risk engine's 150% exposure limit, which live never can,
#: so the curves it drew are a different model. The version and the buffer both
#: enter the fingerprint, and every verdict after the switch files under a new
#: ``baseline_id``.
SHADOW_FUNDING_VERSION = "sleeve_cash"

#: How the replay opens its window (KAN-105). ``gated:curve-normalised``: the
#: signal function is never called before the warm-up's first session, the
#: replay trades from there, and the curve (not the book) is scaled to live's
#: NAV on the window's first session. Before it the warm-up bars reached the
#: signal function with their buys refused, so every window opened on a
#: phantom book and in cash (docs/decisions/shadow-seeding-2026-10.md). The
#: version and the warm-up length both enter the fingerprint, so every verdict
#: after the switch — and after any change of N — files under a new
#: ``baseline_id``; momentum's recorded BREACH streak restarts with it.
SHADOW_SEEDING_VERSION = "gated:curve-normalised"

#: Where the replay's signal functions read their positions from (KAN-116).
#: ``runner_book``: a ``PortfolioContext`` of the replay's own filled lots,
#: re-bound before every session — the structure and code path live reads from
#: the ``positions`` table. Before it each signal function kept a private
#: record of the lots it had *asked* for, so an entry the replay did not fill
#: stayed "held" and was never retried (20–49% of buy signals per sleeve), and
#: entry prices, the earnings-drift hold clock and the tail-risk regime-at-entry
#: were taken from the signal bar rather than the fill
#: (docs/decisions/backtest-position-source-2026-10.md). A different model, so
#: it enters the fingerprint and every verdict after the switch files under a
#: new ``baseline_id``.
SHADOW_POSITION_SOURCE_VERSION = "runner_book"


@dataclass(frozen=True)
class ShadowArtifact:
    """What the monitor reads: the curves, and what produced them."""

    series: dict[str, dict[date, float]]
    shadow_id: str
    window_sessions: int
    #: The session this shadow was produced for. Carried *inside* the file
    #: rather than inferred from its name, because a stale shadow is otherwise
    #: indistinguishable from a fresh one: if the 05:15 run fails, yesterday's
    #: artifact is still on disk and the monitor would grade today's live
    #: against yesterday's model curve. Filenames cannot settle it — a copy or
    #: a re-run rewrites them, which is the same reason ``baseline_age``
    #: distrusts mtime for the pinned artifact.
    session_date: date
    #: The wall-clock date the run WROTE this artifact.
    #:
    #: Separate from ``session_date`` because they are different facts and
    #: conflating them broke the freshness check outright. ``session_date`` is
    #: the last COMPLETE market session the curve covers; at 05:15 SGT that is
    #: always the previous calendar day, while the live book's
    #: ``equity_snapshots`` row is stamped with the run's own SGT date. The two
    #: are therefore always one day apart, so comparing them reported "stale"
    #: every single day (measured 2026-09-04: live 09-04, shadow 09-03).
    #:
    #: Freshness asks "did today's 05:15 run write this", which is exactly this
    #: field — and it still catches the case the check exists for, a failed
    #: morning leaving yesterday's artifact on disk.
    produced_on: date
    #: KAN-104 provenance: the newest NYSE session (ET date) the bars behind
    #: this curve cover, and the instant the fetch of those bars STARTED. The
    #: monitor uses the pair to refuse a shadow that was priced before its
    #: session closed. ``None`` on artifacts written before the field existed,
    #: which the monitor reports and grades as before.
    bars_session: date | None = None
    priced_at: datetime | None = None
    #: KAN-109: sleeves the producing run did not replay because their
    #: fundamentals/earnings cache was missing or stale, with the reason. The
    #: monitor records each as NO_DATA with that reason instead of grading it.
    #: Empty on artifacts written before the field existed.
    data_degraded: dict[str, str] = field(default_factory=dict)
    #: KAN-105: the warm-up the replay traded before each window. ``None`` on
    #: artifacts written before the field existed (they were not warmed).
    warmup_sessions: int | None = None
    #: KAN-105: per sleeve, ``warm_from`` (ISO date) and
    #: ``invested_at_window_start`` — how invested the model's own book was
    #: when grading began. Empty on artifacts written before the field existed.
    seeding: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: KAN-105: sleeves whose replay raised in the producing run, with
    #: ``"<ExceptionType>: <message>"``. They have no curve; the monitor
    #: records each NO_DATA with the reason first. Empty on artifacts written
    #: before the field existed.
    replay_failed: dict[str, str] = field(default_factory=dict)


def shadow_id_for(
    portfolios: Mapping[str, Any],
    *,
    whole_shares: bool = False,
    cash_buffer_bps: float = DEFAULT_SLEEVE_CASH_BUFFER_BPS,
    warmup_sessions: int = SHADOW_WARMUP_SESSIONS,
) -> str:
    """Stable identity for the model that produced a shadow.

    Derived from each sleeve's name and its ``shadow_params`` — the parameters
    that determine what the sleeve would do — sorted so that dict ordering,
    which is not a model change, cannot move the id.

    ``whole_shares`` changes what the replay would do — a budget below one
    share opens nothing — so it is part of the model (KAN-60). It enters the
    fingerprint only when true.

    :data:`SHADOW_DATING_VERSION` always enters it (KAN-103). That changed
    every id on purpose: the verdicts filed before it compared live one
    session off its shadow, and must not share a streak with ones that do not.

    :data:`SHADOW_FUNDING_VERSION` and ``cash_buffer_bps`` always enter it
    (KAN-111), for the same reason: a replay that funds entries from the
    sleeve's cash is a different model from one that could borrow to 150%.

    :data:`SHADOW_SEEDING_VERSION` and ``warmup_sessions`` always enter it
    (KAN-105): a window opened on the model's warmed book is a different
    comparison from one opened in cash on a phantom book, and a different N
    is a different warm book.

    :data:`SHADOW_POSITION_SOURCE_VERSION` always enters it (KAN-116): a
    replay whose signal functions read the fills is a different model from one
    that read a record of what it had asked to buy.

    Raises:
        ValueError: A sleeve exposes no ``shadow_params``. Defaulting to an
            empty mapping would make the id track the roster and nothing else,
            so changing ``top_n`` from 5 to 8 would leave the id, the epoch and
            the breach streak all untouched while the model quietly became a
            different one. D13 requires a baseline change to restart the epoch;
            that can only hold if a missing fingerprint is loud.
    """
    unfingerprinted = sorted(
        name for name, sleeve in portfolios.items()
        if getattr(sleeve, "shadow_params", None) is None
    )
    if unfingerprinted:
        raise ValueError(
            "shadow_params missing for: " + ", ".join(unfingerprinted) + ". "
            "Every sleeve must declare the parameters that determine what it "
            "would do, or the shadow id cannot detect a model change and the "
            "epoch would never restart (direction doc D13)."
        )

    fingerprint = sorted(
        (name, json.dumps(sleeve.shadow_params, sort_keys=True, default=str))
        for name, sleeve in portfolios.items()
    )
    if whole_shares:
        fingerprint.append(("__sizing__", "whole_shares"))
    fingerprint.append(("__dating__", SHADOW_DATING_VERSION))
    fingerprint.append(
        ("__funding__", f"{SHADOW_FUNDING_VERSION}@{float(cash_buffer_bps):g}bps")
    )
    fingerprint.append(
        ("__seeding__", f"warm{int(warmup_sessions)}:{SHADOW_SEEDING_VERSION}")
    )
    fingerprint.append(("__positions__", SHADOW_POSITION_SOURCE_VERSION))
    digest = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True).encode()
    ).hexdigest()[:_DIGEST_CHARS]
    return f"{SHADOW_ID_PREFIX}{digest}"


def dump_shadow(
    path: str | Path,
    *,
    series: Mapping[str, Mapping[date, float]],
    shadow_id: str,
    window_sessions: int,
    session_date: date,
    produced_on: date,
    bars_session: date | None = None,
    priced_at: datetime | None = None,
    data_degraded: Mapping[str, str] | None = None,
    warmup_sessions: int | None = None,
    seeding: Mapping[str, Mapping[str, Any]] | None = None,
    replay_failed: Mapping[str, str] | None = None,
) -> None:
    """Write the shadow artifact.

    Dates are ISO strings: the artifact is read by tools other than the monitor,
    and a JSON date key that is not ISO is a trap for every one of them.

    No aggregate is stored. It is a derived roll-up (direction-doc D15) and the
    digest recomputes it from the per-sleeve rows; persisting one would create a
    second authority that can disagree with the sum of its parts.
    """
    payload = {
        "shadow_id": shadow_id,
        "window_sessions": window_sessions,
        "session_date": session_date.isoformat(),
        "produced_on": produced_on.isoformat(),
        "bars_session": bars_session.isoformat() if bars_session else None,
        "priced_at": priced_at.isoformat() if priced_at else None,
        "data_degraded": dict(sorted((data_degraded or {}).items())),
        "warmup_sessions": warmup_sessions,
        "seeding": {
            sleeve: dict(detail) for sleeve, detail in sorted((seeding or {}).items())
        },
        "replay_failed": dict(sorted((replay_failed or {}).items())),
        "series": {
            sleeve: {session.isoformat(): value for session, value in curve.items()}
            for sleeve, curve in series.items()
        },
    }
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True))


def load_shadow(path: str | Path) -> ShadowArtifact:
    """Read a shadow artifact.

    Raises:
        FileNotFoundError: The artifact is absent, which means the 05:15 job did
            not run. That is the blind signal and the caller has to see it as
            one — returning an empty result here would launder a dead paper run
            into "nothing was gradeable today".
    """
    raw = json.loads(Path(path).read_text())
    return ShadowArtifact(
        series={
            sleeve: {
                date.fromisoformat(session): float(value)
                for session, value in curve.items()
            }
            for sleeve, curve in raw.get("series", {}).items()
        },
        shadow_id=str(raw["shadow_id"]),
        window_sessions=int(raw["window_sessions"]),
        session_date=date.fromisoformat(raw["session_date"]),
        # Artifacts written before this field existed fall back to the session
        # date, which makes them look one day stale — correct, since they
        # predate the fix and should not be graded against.
        produced_on=date.fromisoformat(
            raw.get("produced_on") or raw["session_date"]
        ),
        bars_session=(
            date.fromisoformat(raw["bars_session"])
            if raw.get("bars_session") else None
        ),
        priced_at=(
            datetime.fromisoformat(raw["priced_at"])
            if raw.get("priced_at") else None
        ),
        data_degraded={
            str(k): str(v) for k, v in (raw.get("data_degraded") or {}).items()
        },
        warmup_sessions=(
            int(raw["warmup_sessions"])
            if raw.get("warmup_sessions") is not None else None
        ),
        seeding={
            str(k): dict(v) for k, v in (raw.get("seeding") or {}).items()
        },
        replay_failed={
            str(k): str(v) for k, v in (raw.get("replay_failed") or {}).items()
        },
    )
