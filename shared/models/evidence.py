"""Evidence state store — immutable observations behind the capital ladder.

Every capital decision (promote a rung, de-scale, disarm at the Rung-0 floor,
extend an epoch) is supposed to be mechanical: read the evidence, apply the
written rule. These four tables are that evidence.

The architectural rule (direction doc D15): **the store holds observations,
never derived truth.** ``divergence_daily`` holds exactly the per-sleeve
verdicts the monitor produced; ``gate_epochs`` holds the manifest and the
transition *events*; drill outcomes are rows. Streak lengths, "is this epoch
clean", and the current rung are computed at read time — never stored.

The consequence that makes this design work: **blindness is derived from
absence.** A missing ``divergence_daily`` row on an NYSE trading day IS the
blind signal, so a dead monitor cannot hide by staying silent. That is why
there is no ``is_blind``, ``streak_length`` or ``is_clean`` column here, and
why such a column should be rejected in review if one is ever proposed.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Mapping

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base


class DivergenceStatus(StrEnum):
    OK = "OK"
    WARNING = "WARNING"
    BREACH = "BREACH"
    NO_DATA = "NO_DATA"


class EpochState(StrEnum):
    """DERIVED epoch state — the vocabulary of folded events, never a column.

    Deliberately not stored: an epoch's state is the fold of its event rows.
    Storing it would make :class:`GateEpoch` hold derived truth, which is
    exactly what D15 forbids.
    """

    RUNNING = "RUNNING"
    CLEAN = "CLEAN"
    BREACHED = "BREACHED"
    EXTENDED = "EXTENDED"
    RESTARTED = "RESTARTED"
    DISARMED = "DISARMED"


EPOCH_EVENT_TYPES = (
    "started",
    "clean",
    "breached",
    "extended",
    "restarted",
    "rung_change",
    "disarmed",
    "safety_incident",
    "round_trip_shortfall",
)


class DrillType(StrEnum):
    RESTART_HALT = "restart_halt"
    SYNTHETIC_STOP = "synthetic_stop"


DIVERGENCE_STATUS_VALUES = tuple(status.value for status in DivergenceStatus)
DIVERGENCE_STATUS_CHECK = "status IN ({})".format(
    ", ".join(f"'{status}'" for status in DIVERGENCE_STATUS_VALUES)
)

DRILL_TYPE_VALUES = tuple(drill.value for drill in DrillType)
DRILL_TYPE_CHECK = "drill_type IN ({})".format(
    ", ".join(f"'{drill}'" for drill in DRILL_TYPE_VALUES)
)


class DivergenceDaily(Base):
    """One per-sleeve divergence verdict, exactly as the monitor produced it.

    Observation only (D15). Absence of a row on an NYSE trading day IS the
    blind signal, so a run that produces no verdict must write no row rather
    than a placeholder. The pins (``baseline_id`` / ``window_sessions`` /
    ``threshold``) are stored because a verdict is uninterpretable without the
    parameters it was scored under.
    """

    __tablename__ = "divergence_daily"
    __table_args__ = (
        UniqueConstraint(
            "sleeve",
            "session_date",
            "baseline_id",
            name="uq_divergence_daily_sleeve_date_baseline",
        ),
        CheckConstraint(DIVERGENCE_STATUS_CHECK, name="ck_divergence_daily_status"),
        Index("ix_divergence_daily_session_date", "session_date"),
        Index("ix_divergence_daily_sleeve_date", "sleeve", "session_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sleeve: Mapped[str] = mapped_column(String(64), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    baseline_id: Mapped[str] = mapped_column(String(128), nullable=False)
    window_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    metric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class GateEpoch(Base):
    """The immutable start-facts of one evidence epoch at one rung.

    ``manifest`` carries the D13 items: portfolio weights, sleeve set, baseline
    id, membership snapshot, divergence window/threshold, cost model, and the
    money-path commit hashes. :func:`validate_manifest` is its schema.

    Deliberately has NO ``status`` and NO ``ended_at`` column. An epoch's
    current state and its end are DERIVED by folding its ``gate_epoch_events``
    rows — storing them here would make this table hold derived truth, which
    D15 forbids, and would create two sources for "is this epoch clean". A
    restart, breach, or promotion is an appended event; this row never changes
    after insert.

    ``label`` is unique: one epoch per label, so "v2" is unambiguous forever.
    """

    __tablename__ = "gate_epochs"
    __table_args__ = (
        UniqueConstraint("label", name="uq_gate_epochs_label"),
        Index("ix_gate_epochs_rung_started", "rung", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(32), nullable=False)  # e.g. "v2"
    rung: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class GateEpochEvent(Base):
    """An immutable transition event for one epoch.

    These rows ARE the epoch's state machine: folding them yields the current
    state (see :class:`EpochState`). ``event_type`` is intentionally an open
    string rather than a CheckConstraint, because the ladder's amendment rule
    adds new event kinds between epochs and a migration per event name would be
    friction with no safety benefit. Known values are :data:`EPOCH_EVENT_TYPES`.

    ``rung_after`` is populated on ``rung_change`` / ``disarmed`` events so the
    rung history is readable without replaying every event's detail blob.
    """

    __tablename__ = "gate_epoch_events"
    __table_args__ = (
        Index("ix_gate_epoch_events_epoch_occurred", "epoch_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # gate_epochs.id — no ForeignKey, per the repo-wide convention.
    epoch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    rung_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # D16 precedence chain.
    incident_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class DrillOutcome(Base):
    """One per-epoch drill result (restart/halt or synthetic stop-loss)."""

    __tablename__ = "drill_outcomes"
    __table_args__ = (
        CheckConstraint(DRILL_TYPE_CHECK, name="ck_drill_outcomes_type"),
        Index("ix_drill_outcomes_epoch_type", "epoch_id", "drill_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    epoch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    drill_type: Mapped[str] = mapped_column(String(32), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detail: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


# ---------------------------------------------------------------------------
# Manifest schema — pinned here so the writer and the reader cannot drift.
# ---------------------------------------------------------------------------

MANIFEST_MONEY_PATH_KEYS = (
    "scripts/run_paper.py",
    "services/execution",
    "services/risk_management",
    "shared/liquidation.py",
    "shared/order_ledger.py",
)

WEIGHTS_SUM_TOLERANCE = 0.001

_HEX = set("0123456789abcdefABCDEF")


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(char in _HEX for char in value)


def _require_mapping(value: Any, key: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"manifest key {key!r} must be an object")
    return value


def _validate_baseline_id(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("manifest key 'baseline_id' must be a non-empty string")


def _validate_cost_model(value: Any) -> None:
    mapping = _require_mapping(value, "cost_model")
    for name in ("commission_minimum", "commission_per_share", "slippage_bps"):
        if name not in mapping:
            raise ValueError(f"manifest key 'cost_model' is missing {name!r}")
        if not isinstance(mapping[name], (int, float)) or isinstance(
            mapping[name], bool
        ):
            raise ValueError(f"manifest key 'cost_model.{name}' must be a number")


def _validate_divergence(value: Any) -> None:
    mapping = _require_mapping(value, "divergence")
    if "threshold" not in mapping:
        raise ValueError("manifest key 'divergence' is missing 'threshold'")
    if not isinstance(mapping["threshold"], (int, float)) or isinstance(
        mapping["threshold"], bool
    ):
        raise ValueError("manifest key 'divergence.threshold' must be a number")
    if "window_sessions" not in mapping:
        raise ValueError("manifest key 'divergence' is missing 'window_sessions'")
    if not isinstance(mapping["window_sessions"], int) or isinstance(
        mapping["window_sessions"], bool
    ):
        raise ValueError("manifest key 'divergence.window_sessions' must be an int")


def _validate_membership_snapshot(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(
            "manifest key 'membership_snapshot' must be a non-empty string"
        )


def _validate_membership_snapshot_sha256(value: Any) -> None:
    if not isinstance(value, str) or not _is_hex(value, 64):
        raise ValueError(
            "manifest key 'membership_snapshot_sha256' must be 64 hex characters"
        )


def _validate_money_path(value: Any) -> None:
    mapping = _require_mapping(value, "money_path")
    for name in MANIFEST_MONEY_PATH_KEYS:
        if name not in mapping:
            raise ValueError(f"manifest key 'money_path' is missing {name!r}")
    for name in sorted(mapping):
        entry = mapping[name]
        if not isinstance(entry, str) or not _is_hex(entry, 40):
            raise ValueError(
                f"manifest key 'money_path.{name}' must be a 40-character git sha"
            )


def _validate_sleeves(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise ValueError("manifest key 'sleeves' must be a non-empty list of strings")
    for sleeve in value:
        if not isinstance(sleeve, str):
            raise ValueError("manifest key 'sleeves' must contain only strings")


def _validate_weights(value: Any) -> None:
    mapping = _require_mapping(value, "weights")
    if not mapping:
        raise ValueError("manifest key 'weights' must not be empty")
    for name in sorted(mapping):
        weight = mapping[name]
        if not isinstance(name, str):
            raise ValueError("manifest key 'weights' must be keyed by sleeve name")
        if not isinstance(weight, (int, float)) or isinstance(weight, bool):
            raise ValueError(f"manifest key 'weights.{name}' must be a number")
    total = sum(float(weight) for weight in mapping.values())
    if abs(total - 1.0) > WEIGHTS_SUM_TOLERANCE:
        raise ValueError(
            f"manifest key 'weights' must sum to 1.0 "
            f"(+/-{WEIGHTS_SUM_TOLERANCE}), got {total:.6f}"
        )


# Alphabetical: the documented check order, so the reported error is
# deterministic for a manifest with more than one problem.
_MANIFEST_VALIDATORS = (
    ("baseline_id", _validate_baseline_id),
    ("cost_model", _validate_cost_model),
    ("divergence", _validate_divergence),
    ("membership_snapshot", _validate_membership_snapshot),
    ("membership_snapshot_sha256", _validate_membership_snapshot_sha256),
    ("money_path", _validate_money_path),
    ("sleeves", _validate_sleeves),
    ("weights", _validate_weights),
)

MANIFEST_REQUIRED_KEYS = tuple(key for key, _ in _MANIFEST_VALIDATORS)


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    """Validate a :attr:`GateEpoch.manifest` blob, or raise ``ValueError``.

    Called by the epoch writer before insert and by the read-time helper after
    load, so a hand-edited manifest fails loudly at read time instead of
    producing a silently wrong epoch score.

    Checks run in a documented order — every top-level key's *presence* first,
    alphabetically, then each key's type and nested contents, also
    alphabetically — so the first offending key named in the message is
    deterministic. Unknown extra keys are permitted and ignored: the ladder's
    amendment rule adds manifest items between epochs, and rejecting them would
    make every amendment a code change.
    """
    if not isinstance(manifest, Mapping):
        raise ValueError("manifest must be a mapping")

    for key in MANIFEST_REQUIRED_KEYS:
        if key not in manifest:
            raise ValueError(f"manifest missing required key: {key!r}")

    for key, validator in _MANIFEST_VALIDATORS:
        validator(manifest[key])
