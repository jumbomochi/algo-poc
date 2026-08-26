#!/usr/bin/env python3
"""Record epoch manifests and run the capital ladder's rung transitions.

KAN-28. The ladder's rules (``docs/designs/project-direction.md``, D11-D16)
were written but nothing executed them: deciding "is this epoch clean, and does
the rung change" was a judgment call made on gate day, under time pressure,
from scrollback. This CLI turns the ladder into arithmetic over the evidence
store.

Four subcommands, every one of them append-only::

    record_epoch.py start    --label v2 --rung 0 [--manifest-file overrides.json]
    record_epoch.py event    --epoch v2 --type breached [--reason ...]
    record_epoch.py drill    --epoch v2 --type restart_halt --passed
    record_epoch.py evaluate --epoch v2 [--as-of YYYY-MM-DD] [--json] [--dry-run]

No row is ever mutated to a different verdict — a correction is a new event —
so the history stays auditable and the store keeps holding observations rather
than derived truth (D15). All read-side arithmetic is delegated to
``shared/evidence_store.py``; this module owns only the transition rules and
the manifest capture.

**What this does NOT do.** It records and reports; it does not enforce. A
``disarmed`` event is the written trigger for the operator to turn the live
account off — not the act of turning it off. Nothing in the money path or the
daily job calls this module.

Two ordering rules carry most of the safety weight:

1. **A breach outranks drift.** If a money-path change and a breach are both
   detectable at evaluate time, the breach wins. The opposite order would let
   an epoch's bad evidence be voided by landing a code change, which is exactly
   the pressure-time escape hatch the ladder exists to remove.
2. **A chain writes its nonterminal records first.** ``current_epoch_state``
   reports any nonterminal event that follows a terminal one as an anomaly, so
   a legitimate de-scale must record ``rung_change`` before ``breached``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

# Executed by path as well as imported, so pin the repo root rather than
# trusting whatever tree an editable install points at.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from shared.evidence_store import current_epoch_state, epoch_progress  # noqa: E402
from shared.models.evidence import (  # noqa: E402
    EPOCH_EVENT_TYPES,
    MANIFEST_MONEY_PATH_KEYS,
    DrillOutcome,
    DrillType,
    EpochState,
    GateEpoch,
    GateEpochEvent,
    validate_manifest,
)

__all__ = [
    "MAX_RUNG",
    "RESPONSE_PRECEDENCE",
    "RUNG_ENTRY_PREREQUISITES",
    "Decision",
    "EpochAlreadyRunningError",
    "Evaluation",
    "PlannedEvent",
    "build_manifest",
    "evaluate_epoch",
    "main",
    "manifest_drift",
    "money_path_hashes",
    "plan_transition",
    "record_drill",
    "record_event",
    "start_epoch",
]

#: The ladder's written top rung. Beyond it, rungs exist only by between-epoch
#: amendment (D16) — a clean epoch at the top does NOT promote, because "the
#: ladder never scales silently" has to be code, not a footnote.
MAX_RUNG = 3

#: Rungs whose entry requires something this CLI cannot observe. A clean epoch
#: below such a rung records the ``clean`` verdict but withholds the
#: ``rung_change``; the operator records it by hand once the prerequisite is met.
RUNG_ENTRY_PREREQUISITES: dict[int, str] = {
    3: "an explicit capacity review (slippage vs backtest at size)",
}

#: D16's response ladder, weakest first: safety halt -> sleeve demotion -> rung
#: de-scale. The chain's PRIMARY event is the highest of these that applies;
#: everything else in the chain is a subsumed record sharing its incident id.
#: One incident therefore yields one punishment plus its paper trail, never
#: three independent ones.
RESPONSE_PRECEDENCE = (
    "safety_incident",
    "breached",
    "clean",
    "extended",
    "restarted",
    "rung_change",
    "disarmed",
)

#: Criteria that can fail an epoch. The other two (``drills``,
#: ``evidence_quantum``) are amber-only by construction — D12 makes a shortfall
#: extend an epoch rather than fail one.
_FAILING_CRITERIA = ("divergence", "drawdown", "safety")

#: Manifest keys :func:`manifest_drift` can recompute. ``baseline_id`` is
#: deliberately absent: the newest file in ``output/`` changes for reasons that
#: have nothing to do with the pinned baseline, so comparing it would emit a
#: restart every time a backtest is run.
_DRIFT_KEYS = ("sleeves", "weights", "divergence", "cost_model")

_TERMINAL_EVENT_TYPES = frozenset({"clean", "breached", "disarmed"})

DEFAULT_MEMBERSHIP_SNAPSHOT = "data/universe/sp500_membership.json"


class EpochAlreadyRunningError(RuntimeError):
    """Raised when starting an epoch while another has not yet ended."""


class EpochNotFoundError(RuntimeError):
    """Raised when a subcommand names a label no epoch carries."""


# ---------------------------------------------------------------------------
# manifest capture (D13)
# ---------------------------------------------------------------------------


def _git(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        raise ValueError(
            f"git {' '.join(args)} failed in {repo_root}: {detail.strip()}"
        ) from error
    return result.stdout.strip()


def money_path_hashes(repo_root: Path | str) -> dict[str, str]:
    """The committed tree/blob hash of each money path, from ``HEAD``.

    A per-path hash rather than one repo-wide commit sha: the whole point is
    that a docs commit must not look like a money-path change (D13's scoped
    restart), and ``git rev-parse HEAD:<path>`` changes if and only if that
    path's committed content did.
    """
    root = Path(repo_root)
    return {
        name: _git(root, "rev-parse", f"HEAD:{name}")
        for name in MANIFEST_MONEY_PATH_KEYS
    }


def _capital_allocations() -> dict[str, float]:
    """The live sleeve weights, read from the one place that owns them.

    Imported lazily: ``scripts/run_paper.py`` pulls in the broker stack, and
    ``record_epoch.py evaluate`` should not need IB imports to score an epoch.
    """
    from scripts.run_paper import CAPITAL_ALLOCATIONS

    return dict(CAPITAL_ALLOCATIONS)


def _divergence_pins() -> dict[str, Any]:
    from backtest.divergence import DEFAULT_THRESHOLD, DEFAULT_WINDOW_DAYS

    # The monitor names its window in days; the ladder names it in sessions
    # (D11 pins them to the same 30). Recorded under the manifest's key so the
    # read side needs no translation.
    return {"window_sessions": int(DEFAULT_WINDOW_DAYS), "threshold": DEFAULT_THRESHOLD}


def _cost_model() -> dict[str, float]:
    from backtest.costs import (
        DEFAULT_COMMISSION_MINIMUM,
        DEFAULT_COMMISSION_PER_SHARE,
        DEFAULT_SLIPPAGE_BPS,
    )

    return {
        "slippage_bps": float(DEFAULT_SLIPPAGE_BPS),
        "commission_per_share": float(DEFAULT_COMMISSION_PER_SHARE),
        "commission_minimum": float(DEFAULT_COMMISSION_MINIMUM),
    }


def _newest_baseline(repo_root: Path) -> str:
    """The basename of the newest ``output/backtest_multi_*.json``.

    Ordered by name, not mtime: the names carry the run timestamp, so name
    order is the run order, and a file that was merely re-copied does not
    become "newest".
    """
    candidates = sorted(
        path.name for path in (repo_root / "output").glob("backtest_multi_*.json")
    )
    if not candidates:
        raise ValueError(
            f"no baseline found: {repo_root / 'output'} contains no "
            "backtest_multi_*.json. Generate one (docs/operations/"
            "backtest-baseline.md) or pass it via --manifest-file."
        )
    return candidates[-1]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(
    *,
    repo_root: Path | str,
    membership_snapshot: str = DEFAULT_MEMBERSHIP_SNAPSHOT,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture the D13 manifest, collecting every item it can rather than
    trusting a human to type it.

    ``overrides`` (``--manifest-file``) replaces whole top-level keys. It is
    applied BEFORE the missing-input checks, so an operator whose membership
    snapshot or baseline lives outside this checkout can still record an honest
    manifest — but cannot silently skip one.
    """
    root = Path(repo_root)
    overrides = dict(overrides or {})
    weights = _capital_allocations()

    manifest: dict[str, Any] = {
        "sleeves": list(weights),
        "weights": weights,
        "divergence": _divergence_pins(),
        "cost_model": _cost_model(),
        "money_path": money_path_hashes(root),
        "membership_snapshot": membership_snapshot,
    }

    if "baseline_id" not in overrides:
        manifest["baseline_id"] = _newest_baseline(root)

    if "membership_snapshot_sha256" not in overrides:
        snapshot = root / membership_snapshot
        if not snapshot.is_file():
            raise ValueError(
                f"membership snapshot {snapshot} does not exist, so its hash "
                "cannot be recorded. Point --membership-snapshot at the file "
                "the baseline was run against, or supply "
                "'membership_snapshot_sha256' via --manifest-file."
            )
        manifest["membership_snapshot_sha256"] = _sha256_file(snapshot)

    manifest.update(overrides)
    validate_manifest(manifest)
    return manifest


def manifest_drift(
    manifest: Mapping[str, Any], *, repo_root: Path | str
) -> list[str]:
    """Manifest items whose live value no longer matches what was recorded.

    Returns money-path entries under their own path name (``services/execution``)
    and everything else under its manifest key, sorted — so the restart reason
    names what actually moved.

    ``membership_snapshot`` is compared only when the recorded path is readable:
    a snapshot that lives outside this checkout is unverifiable, and reporting
    unverifiable as changed would restart every epoch on every evaluation.
    """
    root = Path(repo_root)
    drift: list[str] = []

    recorded_money = dict(manifest.get("money_path", {}))
    live_money = money_path_hashes(root)
    for name in sorted(set(recorded_money) | set(live_money)):
        if recorded_money.get(name) != live_money.get(name):
            drift.append(name)

    weights = _capital_allocations()
    live_values: dict[str, Any] = {
        "sleeves": list(weights),
        "weights": weights,
        "divergence": _divergence_pins(),
        "cost_model": _cost_model(),
    }
    for key in _DRIFT_KEYS:
        if manifest.get(key) != live_values[key]:
            drift.append(key)

    snapshot_path = manifest.get("membership_snapshot")
    recorded_hash = manifest.get("membership_snapshot_sha256")
    if isinstance(snapshot_path, str):
        snapshot = root / snapshot_path
        if snapshot.is_file() and _sha256_file(snapshot) != recorded_hash:
            drift.append("membership_snapshot")

    return sorted(drift)


# ---------------------------------------------------------------------------
# writes — every one an append
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch_by_label(session: Session, label: str) -> GateEpoch:
    epoch = session.scalars(
        select(GateEpoch).where(GateEpoch.label == label)
    ).one_or_none()
    if epoch is None:
        raise EpochNotFoundError(f"no epoch labelled {label!r}")
    return epoch


def _open_epoch(session: Session) -> GateEpoch | None:
    """The epoch that has not ended, if there is one.

    "Open" is derived, not stored: an epoch is open while its folded state is
    not terminal, which is the same definition the read side uses.
    """
    for epoch in session.scalars(
        select(GateEpoch).order_by(GateEpoch.started_at.desc(), GateEpoch.id.desc())
    ).all():
        state, _ = current_epoch_state(session, epoch_id=epoch.id)
        if state not in {
            EpochState.CLEAN.value,
            EpochState.BREACHED.value,
            EpochState.DISARMED.value,
        }:
            return epoch
    return None


def start_epoch(
    session: Session,
    *,
    label: str,
    rung: int,
    manifest: Mapping[str, Any],
    now: datetime | None = None,
) -> GateEpoch:
    """Record an epoch's start facts and its ``started`` event.

    Refuses while another epoch is open. Two concurrent epochs would make "the
    current rung" ambiguous, and the ambiguity would only surface on gate day.
    """
    validate_manifest(manifest)
    open_epoch = _open_epoch(session)
    if open_epoch is not None:
        raise EpochAlreadyRunningError(
            f"epoch {open_epoch.label!r} (rung {open_epoch.rung}) has not ended; "
            "record its terminal event before starting another"
        )

    moment = now or _now()
    epoch = GateEpoch(
        label=label, rung=rung, manifest=dict(manifest), started_at=moment
    )
    session.add(epoch)
    session.flush()
    session.add(
        GateEpochEvent(
            epoch_id=epoch.id,
            event_type="started",
            rung_after=rung,
            reason=f"epoch {label} started at rung {rung}",
            detail={"manifest_keys": sorted(manifest)},
            occurred_at=moment,
        )
    )
    session.flush()
    return epoch


def record_event(
    session: Session,
    *,
    label: str,
    event_type: str,
    reason: str | None = None,
    detail: Mapping[str, Any] | None = None,
    rung_after: int | None = None,
    incident_id: str | None = None,
    now: datetime | None = None,
) -> GateEpochEvent:
    """Append one transition event to an epoch.

    Deliberately does not refuse an epoch that has already ended: a late
    correction is a real operator need, and the store already surfaces a
    post-terminal event as a named anomaly rather than silently absorbing it.
    """
    epoch = _epoch_by_label(session, label)
    event = GateEpochEvent(
        epoch_id=epoch.id,
        event_type=event_type,
        rung_after=rung_after,
        incident_id=incident_id,
        reason=reason,
        detail=dict(detail) if detail is not None else None,
        occurred_at=now or _now(),
    )
    session.add(event)
    session.flush()
    return event


def record_drill(
    session: Session,
    *,
    label: str,
    drill_type: str,
    passed: bool,
    detail: str | None = None,
    now: datetime | None = None,
) -> DrillOutcome:
    """Append one drill outcome, tied to the epoch it was run in."""
    epoch = _epoch_by_label(session, label)
    outcome = DrillOutcome(
        epoch_id=epoch.id,
        drill_type=drill_type,
        passed=passed,
        detail=detail,
        occurred_at=now or _now(),
    )
    session.add(outcome)
    session.flush()
    return outcome


# ---------------------------------------------------------------------------
# the transition engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedEvent:
    """One event a transition would append. Planned, not yet written."""

    event_type: str
    reason: str
    rung_after: int | None = None
    incident_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    """The verdict for one evaluation plus the exact chain it would write."""

    verdict: str
    events: tuple[PlannedEvent, ...]
    reasons: tuple[str, ...]

    @property
    def rung_after(self) -> int | None:
        for event in self.events:
            if event.rung_after is not None and event.event_type in {
                "rung_change",
                "disarmed",
            }:
                return event.rung_after
        return None


def _chain(events: list[PlannedEvent], incident_id: str) -> tuple[PlannedEvent, ...]:
    """Stamp one incident id across a chain and mark its single primary event.

    Ordering is deliberate and load-bearing: nonterminal records go first, the
    terminal verdict last, because ``current_epoch_state`` flags any nonterminal
    event that follows a terminal one as an anomaly.
    """
    if not events:
        return ()
    primary = max(events, key=lambda event: RESPONSE_PRECEDENCE.index(event.event_type))
    ordered = [e for e in events if e.event_type not in _TERMINAL_EVENT_TYPES]
    ordered += [e for e in events if e.event_type in _TERMINAL_EVENT_TYPES]
    return tuple(
        PlannedEvent(
            event_type=event.event_type,
            reason=event.reason,
            rung_after=event.rung_after,
            incident_id=incident_id,
            detail={
                **event.detail,
                "chain_role": "primary" if event is primary else "record",
            },
        )
        for event in ordered
    )


def plan_transition(
    *,
    rung: int,
    criteria: Mapping[str, str],
    window_complete: bool,
    drift: Sequence[str] = (),
    previous_breached: bool = False,
    incident_id: str,
) -> Decision:
    """Apply the ladder's written rules to one epoch's scored criteria.

    Pure: no session, no clock, no repository. Every branch below is traceable
    to ``docs/designs/project-direction.md``'s ladder section.

    Precedence, highest first:

    1. any failing criterion -> BREACHED (acts immediately, window or not)
    2. manifest/money-path drift -> RESTARTED
    3. window still open -> RUNNING, nothing recorded
    4. a shortfall (drills or evidence quantum) -> EXTENDED
    5. everything green and the window closed -> CLEAN
    """
    failed = [name for name in _FAILING_CRITERIA if criteria.get(name) == "red"]
    if failed:
        return _breach(rung, failed, previous_breached, incident_id)

    if drift:
        listed = ", ".join(drift)
        reason = (
            "Epoch restarted: the manifest no longer describes the running "
            f"system — {listed} changed since the epoch started (D13). The "
            "evidence collected so far describes two different systems; end "
            "this epoch and start a fresh one at the same rung."
        )
        return Decision(
            verdict=EpochState.RESTARTED.value,
            events=_chain(
                [
                    PlannedEvent(
                        event_type="restarted",
                        reason=reason,
                        detail={"drift": list(drift)},
                    )
                ],
                incident_id,
            ),
            reasons=(reason,),
        )

    if not window_complete:
        return Decision(
            verdict=EpochState.RUNNING.value,
            events=(),
            reasons=(
                "The epoch's scoring window has not closed yet; nothing is "
                "recorded until it does.",
            ),
        )

    short = [
        name
        for name in ("drills", "evidence_quantum")
        if criteria.get(name) == "amber"
    ]
    if short:
        reason = (
            "Epoch extended: "
            + ", ".join(short)
            + " fell short of the bar. A shortfall extends an epoch, it never "
            "fails one (D12)."
        )
        return Decision(
            verdict=EpochState.EXTENDED.value,
            events=_chain(
                [
                    PlannedEvent(
                        event_type="extended",
                        reason=reason,
                        detail={"blocking_criteria": short},
                    )
                ],
                incident_id,
            ),
            reasons=(reason,),
        )

    return _clean(rung, incident_id)


def _clean(rung: int, incident_id: str) -> Decision:
    reasons = [
        f"Epoch clean at rung {rung}: every criterion is green and the window "
        "has closed."
    ]
    events = [
        PlannedEvent(
            event_type="clean",
            reason=reasons[0],
            detail={"rung": rung},
        )
    ]

    next_rung = rung + 1
    if next_rung > MAX_RUNG:
        reasons.append(
            f"Rung {rung} is the ladder's written top rung; a further rung "
            "exists only by between-epoch amendment (D16), so no promotion is "
            "recorded."
        )
    elif next_rung in RUNG_ENTRY_PREREQUISITES:
        reasons.append(
            f"Promotion to rung {next_rung} additionally requires "
            f"{RUNG_ENTRY_PREREQUISITES[next_rung]}; record the rung_change by "
            "hand once it is done."
        )
    else:
        reasons.append(f"Promoting rung {rung} -> {next_rung}.")
        events.append(
            PlannedEvent(
                event_type="rung_change",
                reason=reasons[-1],
                rung_after=next_rung,
                detail={"direction": "promote", "rung_before": rung},
            )
        )

    return Decision(
        verdict=EpochState.CLEAN.value,
        events=_chain(events, incident_id),
        reasons=tuple(reasons),
    )


def _breach(
    rung: int, failed: Sequence[str], previous_breached: bool, incident_id: str
) -> Decision:
    listed = ", ".join(failed)
    reasons = [f"Epoch breached at rung {rung}: {listed} red."]
    events = [
        PlannedEvent(
            event_type="breached",
            reason=reasons[0],
            detail={"failed_criteria": list(failed), "rung": rung},
        )
    ]

    if rung == 0:
        # The Rung-0 floor: there is no lower rung to fall to, so the response
        # is to stop trading real money entirely.
        reasons.append(
            "Rung-0 floor: the live account is disarmed and returns to paper. "
            "Re-entry requires a fresh go-live gate review on a new clean epoch."
        )
        events.append(
            PlannedEvent(
                event_type="disarmed",
                reason=reasons[-1],
                rung_after=0,
                detail={"operator_action": "disarm the live IB account"},
            )
        )
    elif previous_breached:
        reasons.append(
            f"Second consecutive breached epoch: rung {rung} -> 0 and a full "
            "incident review."
        )
        events.append(
            PlannedEvent(
                event_type="rung_change",
                reason=reasons[-1],
                rung_after=0,
                detail={"direction": "de-scale", "rung_before": rung, "strikes": 2},
            )
        )
        events.append(
            PlannedEvent(
                event_type="safety_incident",
                reason="Two consecutive breached epochs — incident review owed.",
                detail={"trigger": "two_consecutive_breached_epochs"},
            )
        )
    else:
        reasons.append(f"De-scaling one rung: {rung} -> {rung - 1}.")
        events.append(
            PlannedEvent(
                event_type="rung_change",
                reason=reasons[-1],
                rung_after=rung - 1,
                detail={"direction": "de-scale", "rung_before": rung},
            )
        )

    verdict = (
        EpochState.DISARMED.value if rung == 0 else EpochState.BREACHED.value
    )
    return Decision(
        verdict=verdict, events=_chain(events, incident_id), reasons=tuple(reasons)
    )


# ---------------------------------------------------------------------------
# evaluate — read the store, apply the rules, append the outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evaluation:
    """One evaluation's full result — the verdict, the chain, and the evidence."""

    label: str
    rung: int
    as_of: date
    #: The epoch's state BEFORE this evaluation, folded from its events.
    state: str
    verdict: str
    rung_after: int | None
    incident_id: str
    drift: list[str]
    criteria: dict[str, str]
    blocking: list[str]
    reasons: list[str]
    events_written: list[PlannedEvent]
    dry_run: bool
    progress: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "epoch": self.label,
            "rung": self.rung,
            "as_of": self.as_of.isoformat(),
            "state": self.state,
            "verdict": self.verdict,
            "rung_after": self.rung_after,
            "incident_id": self.incident_id,
            "drift": list(self.drift),
            "criteria": dict(self.criteria),
            "blocking": list(self.blocking),
            "reasons": list(self.reasons),
            "dry_run": self.dry_run,
            "progress": dict(self.progress),
            "events_written": [
                {
                    "event_type": event.event_type,
                    "rung_after": event.rung_after,
                    "incident_id": event.incident_id,
                    "reason": event.reason,
                    "detail": event.detail,
                }
                for event in self.events_written
            ],
        }


def _previous_epoch_breached(session: Session, epoch: GateEpoch) -> bool:
    previous = session.scalars(
        select(GateEpoch)
        .where(GateEpoch.started_at < epoch.started_at)
        .order_by(GateEpoch.started_at.desc(), GateEpoch.id.desc())
        .limit(1)
    ).one_or_none()
    if previous is None:
        return False
    state, _ = current_epoch_state(session, epoch_id=previous.id)
    return state in {EpochState.BREACHED.value, EpochState.DISARMED.value}


def _incident_id(session: Session, epoch: GateEpoch, as_of: date) -> str:
    """Reuse the id of the incident that caused this outcome, if one exists.

    D16 forbids punishing one incident three times. An operator who recorded a
    ``safety_incident`` (a halt, a sleeve demotion) already opened the chain;
    the de-scale that follows belongs to it, so evaluate adopts its id instead
    of minting a rival one.
    """
    existing = session.scalars(
        select(GateEpochEvent.incident_id)
        .where(
            GateEpochEvent.epoch_id == epoch.id,
            GateEpochEvent.event_type == "safety_incident",
            GateEpochEvent.incident_id.is_not(None),
        )
        .order_by(GateEpochEvent.occurred_at.desc(), GateEpochEvent.id.desc())
        .limit(1)
    ).one_or_none()
    if existing:
        return existing
    return f"{epoch.label}:{as_of.isoformat()}"[:64]


def _already_recorded(
    session: Session, epoch: GateEpoch, planned: Sequence[PlannedEvent]
) -> bool:
    """True when this exact nonterminal outcome is already the epoch's latest.

    ``evaluate`` is meant to be run repeatedly — daily, or twice in one morning.
    Re-appending an identical ``extended`` every time would bury the events that
    matter under noise, so an unchanged nonterminal verdict is a no-op. Terminal
    verdicts never reach here: they are blocked earlier.
    """
    if not planned:
        return False
    latest = session.scalars(
        select(GateEpochEvent)
        .where(GateEpochEvent.epoch_id == epoch.id)
        .order_by(GateEpochEvent.occurred_at.desc(), GateEpochEvent.id.desc())
        .limit(1)
    ).one_or_none()
    if latest is None:
        return False
    head = planned[0]
    return (
        len(planned) == 1
        and latest.event_type == head.event_type
        and (latest.detail or {}) == head.detail
    )


def evaluate_epoch(
    session: Session,
    *,
    label: str,
    as_of: date | None = None,
    repo_root: Path | str = _REPO_ROOT,
    calendar: object | None = None,
    dry_run: bool = False,
) -> Evaluation:
    """Score one epoch and append the resulting transition chain.

    Pure read-through to :func:`shared.evidence_store.epoch_progress` plus the
    transition rules — this function computes no criterion of its own, so the
    digest and the gate evaluator cannot disagree with it.
    """
    epoch = _epoch_by_label(session, label)
    scored_on = as_of or date.today()
    progress = epoch_progress(
        session, epoch_id=epoch.id, as_of=scored_on, calendar=calendar
    )
    drift = manifest_drift(epoch.manifest, repo_root=repo_root)
    incident_id = _incident_id(session, epoch, scored_on)

    decision = plan_transition(
        rung=epoch.rung,
        criteria=progress.criteria,
        window_complete=scored_on >= progress.scoring_floor,
        drift=drift,
        previous_breached=_previous_epoch_breached(session, epoch),
        incident_id=incident_id,
    )

    reasons = list(decision.reasons)
    written: list[PlannedEvent] = []
    terminal = progress.state in {
        EpochState.CLEAN.value,
        EpochState.BREACHED.value,
        EpochState.DISARMED.value,
    }

    if terminal:
        reasons.insert(
            0,
            f"Epoch {label} already ended in a terminal {progress.state} state; "
            "nothing was recorded. Start a new epoch instead.",
        )
    elif dry_run:
        reasons.append("--dry-run: no events were written.")
    elif _already_recorded(session, epoch, decision.events):
        reasons.append(
            f"This {decision.verdict} outcome is already the epoch's latest "
            "event; nothing was appended."
        )
    else:
        moment = _now()
        for event in decision.events:
            session.add(
                GateEpochEvent(
                    epoch_id=epoch.id,
                    event_type=event.event_type,
                    rung_after=event.rung_after,
                    incident_id=event.incident_id,
                    reason=event.reason,
                    detail=event.detail,
                    occurred_at=moment,
                )
            )
            written.append(event)
        session.flush()

    return Evaluation(
        label=label,
        rung=epoch.rung,
        as_of=scored_on,
        state=progress.state,
        verdict=decision.verdict,
        rung_after=decision.rung_after,
        incident_id=incident_id,
        drift=drift,
        criteria=dict(progress.criteria),
        blocking=list(progress.blocking),
        reasons=reasons,
        events_written=written,
        dry_run=dry_run,
        progress={
            "sessions_elapsed": progress.sessions_elapsed,
            "sessions_paused": progress.sessions_paused,
            # A SUBSET of sessions_paused, not a separate bucket: the accepted
            # absences are the paused sessions that have a cause on record. Do
            # not sum the two. Stated even when zero (KAN-67 AC6), because a
            # count that only appears once it is nonzero is a count nobody
            # notices changing.
            "sessions_absent": progress.sessions_absent,
            "round_trips": progress.round_trips,
            "exposure_session_pct": round(progress.exposure_session_pct, 2),
            "max_drawdown_pct": round(progress.max_drawdown_pct, 2),
            "scoring_floor": progress.scoring_floor.isoformat(),
        },
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _open_session() -> tuple[Session, Any]:
    from shared.config import load_config

    config = load_config("config/default.yaml")  # applies ALGO_DATABASE_URL
    engine = create_engine(config.database.url)
    return Session(engine), engine


def _load_json(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    with open(path) as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record epoch manifests and run rung transitions."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Record a new epoch's manifest.")
    start.add_argument("--label", required=True, help="e.g. v2")
    start.add_argument("--rung", type=int, required=True)
    start.add_argument(
        "--manifest-file",
        default=None,
        help="JSON object whose keys override the captured manifest items.",
    )
    start.add_argument(
        "--membership-snapshot",
        default=DEFAULT_MEMBERSHIP_SNAPSHOT,
        help="Path (repo-relative) to the point-in-time membership JSON.",
    )

    event = subparsers.add_parser("event", help="Append one transition event.")
    event.add_argument("--epoch", required=True)
    event.add_argument("--type", required=True, choices=sorted(EPOCH_EVENT_TYPES))
    event.add_argument("--reason", default=None)
    event.add_argument("--rung-after", type=int, default=None)
    event.add_argument("--incident-id", default=None)
    event.add_argument("--detail-file", default=None, help="JSON object.")

    drill = subparsers.add_parser("drill", help="Append one drill outcome.")
    drill.add_argument("--epoch", required=True)
    drill.add_argument(
        "--type", required=True, choices=sorted(d.value for d in DrillType)
    )
    outcome = drill.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--passed", action="store_true")
    outcome.add_argument("--failed", action="store_true")
    drill.add_argument("--detail", default=None)

    evaluate = subparsers.add_parser(
        "evaluate", help="Score an epoch and record the transition."
    )
    evaluate.add_argument("--epoch", required=True)
    evaluate.add_argument(
        "--as-of",
        default=None,
        type=lambda value: date.fromisoformat(value),
        help="YYYY-MM-DD (default: today).",
    )
    evaluate.add_argument("--json", action="store_true", dest="as_json")
    evaluate.add_argument(
        "--dry-run",
        action="store_true",
        help="Score and report without appending any event.",
    )
    return parser


def _print_evaluation(result: Evaluation) -> None:
    print(f"Epoch {result.label} (rung {result.rung}) as of {result.as_of}")
    print(f"  state before : {result.state}")
    print(f"  verdict      : {result.verdict}")
    if result.rung_after is not None:
        print(f"  rung after   : {result.rung_after}")
    print("  criteria     : " + ", ".join(
        f"{name}={value}" for name, value in result.criteria.items()
    ))
    if result.drift:
        print("  drift        : " + ", ".join(result.drift))
    for reason in result.reasons:
        print(f"  - {reason}")
    for note in result.blocking:
        print(f"  · {note}")
    if result.events_written:
        print("  written      : " + ", ".join(
            event.event_type for event in result.events_written
        ))


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    session, engine = _open_session()
    try:
        if args.command == "start":
            manifest = build_manifest(
                repo_root=_REPO_ROOT,
                membership_snapshot=args.membership_snapshot,
                overrides=_load_json(args.manifest_file),
            )
            epoch = start_epoch(
                session, label=args.label, rung=args.rung, manifest=manifest
            )
            session.commit()
            print(f"Started epoch {epoch.label} at rung {epoch.rung} (id {epoch.id}).")
            for key in sorted(manifest):
                print(f"  {key}: {json.dumps(manifest[key], sort_keys=True)}")
            return 0

        if args.command == "event":
            event = record_event(
                session,
                label=args.epoch,
                event_type=args.type,
                reason=args.reason,
                detail=_load_json(args.detail_file),
                rung_after=args.rung_after,
                incident_id=args.incident_id,
            )
            session.commit()
            print(f"Recorded {event.event_type!r} on epoch {args.epoch}.")
            return 0

        if args.command == "drill":
            outcome = record_drill(
                session,
                label=args.epoch,
                drill_type=args.type,
                passed=args.passed,
                detail=args.detail,
            )
            session.commit()
            verdict = "passed" if outcome.passed else "FAILED"
            print(f"Recorded {outcome.drill_type} drill: {verdict}.")
            return 0

        result = evaluate_epoch(
            session,
            label=args.epoch,
            as_of=args.as_of,
            repo_root=_REPO_ROOT,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            session.rollback()
        else:
            session.commit()
        if args.as_json:
            print(json.dumps(result.to_json(), indent=2, sort_keys=True))
        else:
            _print_evaluation(result)
        # Exit codes follow scripts/divergence_monitor.py's convention so a
        # wrapper can treat both jobs the same way: 1 means the run worked and
        # the answer is bad, 2 means the run itself failed.
        return 1 if result.verdict in {
            EpochState.BREACHED.value,
            EpochState.DISARMED.value,
        } else 0
    except (EpochAlreadyRunningError, EpochNotFoundError, ValueError) as error:
        session.rollback()
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
