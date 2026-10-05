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
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

#: Prefix so a reader of an evidence row can tell a rolling-shadow identity from
#: a pinned-artifact one at a glance. The two mean different things and must
#: never be mistakable.
SHADOW_ID_PREFIX = "shadow:"

#: Length of the truncated digest. Full sha256 in an evidence row is noise; 16
#: hex chars is 64 bits, far past collision risk for a handful of models.
_DIGEST_CHARS = 16


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


def shadow_id_for(portfolios: Mapping[str, Any], *, whole_shares: bool = False) -> str:
    """Stable identity for the model that produced a shadow.

    Derived from each sleeve's name and its ``shadow_params`` — the parameters
    that determine what the sleeve would do — sorted so that dict ordering,
    which is not a model change, cannot move the id.

    ``whole_shares`` changes what the replay would do — a budget below one
    share opens nothing — so it is part of the model (KAN-60). It enters the
    fingerprint only when true, which keeps every fractional id byte-identical
    to the ones already filed in ``divergence_daily``.

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
    )
