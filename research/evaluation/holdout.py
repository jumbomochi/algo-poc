# research/evaluation/holdout.py
"""A pre-registered, single-use holdout.

The nested walk-forward in :mod:`research.evaluation.folds` is
cross-validation: every date is eventually scored, and the whole panel is
available to whoever is iterating. That is the right instrument for choosing
a parameterization and the wrong one for the final claim, because nothing
stops a researcher from re-running it until it looks good.

This module is the other instrument. A split is *registered* -- written to a
committed file with its boundary and a timestamp -- before anyone looks, and
it can be evaluated exactly once. The burn is recorded back into the same
file, so a second attempt fails whether or not the process restarted.

Boundaries are registered as a **date**, not an index. Bars arrive daily, so
an index-registered boundary would quietly slide through the data as the
panel grows -- the one thing a pre-registration must never do. ``resolve()``
maps the date onto whatever date index it is handed and applies the same
``gap = horizon + embargo`` purge that ``folds.py`` uses, so the two cannot
drift apart.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "holdout_registry.json"


class HoldoutAlreadyEvaluated(RuntimeError):
    """Raised when a registered split is evaluated (or re-registered) twice."""


@dataclass(frozen=True)
class HoldoutRegistration:
    """The pre-registration record: what was written down, and when."""

    split_id: str
    holdout_start: str
    horizon: int
    embargo: int
    registered_at: str
    note: str = ""

    @property
    def gap(self) -> int:
        return self.horizon + self.embargo


@dataclass(frozen=True)
class HoldoutEvaluation:
    split_id: str
    label: str
    evaluated_at: str


@dataclass(frozen=True)
class HoldoutSplit:
    """A registration resolved against a concrete date index."""

    split_id: str
    train: tuple[int, int]
    purge: tuple[int, int]
    holdout: tuple[int, int]
    gap: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_iso(value: date | datetime | str) -> str:
    """Normalise a date to ``YYYY-MM-DD``.

    Boundaries are compared lexicographically, so a non-ISO string would pick
    the wrong row rather than fail. Everything is validated through
    ``date.fromisoformat``.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return date.fromisoformat(value[:10]).isoformat()
    raise TypeError(f"expected a date, datetime or ISO date string; got {type(value)!r}")


class HoldoutProtocol:
    """Registry of pre-registered holdout splits, backed by a JSON file."""

    def __init__(
        self,
        path: Path,
        registrations: dict[str, HoldoutRegistration],
        evaluations: tuple[HoldoutEvaluation, ...],
        version: int = 1,
        extra: dict | None = None,
    ) -> None:
        self._path = path
        self._registrations = dict(registrations)
        self._evaluations = list(evaluations)
        self._version = version
        # Keys the file carries that this class does not model -- the prose
        # note, most of all. Recording a burn must not strip the document.
        self._extra = dict(extra or {})

    @classmethod
    def load(cls, path: Path | str | None = None) -> HoldoutProtocol:
        resolved = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
        payload = json.loads(resolved.read_text())
        registrations = {
            row["split_id"]: HoldoutRegistration(
                split_id=str(row["split_id"]),
                holdout_start=str(row["holdout_start"]),
                horizon=int(row["horizon"]),
                embargo=int(row["embargo"]),
                registered_at=str(row["registered_at"]),
                note=str(row.get("note", "")),
            )
            for row in payload.get("splits", [])
        }
        evaluations = tuple(
            HoldoutEvaluation(
                split_id=str(row["split_id"]),
                label=str(row["label"]),
                evaluated_at=str(row["evaluated_at"]),
            )
            for row in payload.get("evaluations", [])
        )
        extra = {
            key: value
            for key, value in payload.items()
            if key not in {"version", "splits", "evaluations"}
        }
        return cls(
            resolved, registrations, evaluations, int(payload.get("version", 1)), extra
        )

    # -- registration ----------------------------------------------------

    def register(
        self,
        *,
        split_id: str,
        holdout_start: date | datetime | str,
        horizon: int,
        embargo: int,
        note: str = "",
        registered_at: str | None = None,
    ) -> HoldoutRegistration:
        """Write down a split before anyone looks at it."""
        if self.is_burned(split_id):
            raise HoldoutAlreadyEvaluated(
                f"holdout {split_id!r} has already been evaluated; "
                "re-registering it would move the goalposts after the fact"
            )
        if horizon < 1 or embargo < 1:
            raise ValueError("horizon and embargo must both be at least 1 date")
        registration = HoldoutRegistration(
            split_id=split_id,
            holdout_start=_as_iso(holdout_start),
            horizon=horizon,
            embargo=embargo,
            registered_at=registered_at or _now(),
            note=note,
        )
        self._registrations[split_id] = registration
        self._save()
        return registration

    def registration(self, split_id: str) -> HoldoutRegistration:
        try:
            return self._registrations[split_id]
        except KeyError:
            raise KeyError(f"unknown holdout split {split_id!r}") from None

    # -- resolution ------------------------------------------------------

    def resolve(self, split_id: str, dates: Sequence[date | datetime | str]) -> HoldoutSplit:
        """Map a registration onto a date index, purging the boundary rows.

        ``dates`` must be sorted ascending -- the panel's own index, not a
        reordering of it.
        """
        registration = self.registration(split_id)
        gap = registration.gap
        n_dates = len(dates)
        start = next(
            (i for i, d in enumerate(dates) if _as_iso(d) >= registration.holdout_start),
            None,
        )
        if start is None:
            raise ValueError(
                f"no dates on or after the registered boundary "
                f"{registration.holdout_start} for holdout {split_id!r}"
            )
        train_end = start - gap
        if train_end <= 0:
            raise ValueError(
                f"not enough dates: only {start} dates precede the registered "
                f"boundary {registration.holdout_start}, which cannot absorb a "
                f"gap of {gap}"
            )
        return HoldoutSplit(
            split_id=split_id,
            train=(0, train_end),
            purge=(train_end, start),
            holdout=(start, n_dates),
            gap=gap,
        )

    # -- single use ------------------------------------------------------

    def is_burned(self, split_id: str) -> bool:
        return any(row.split_id == split_id for row in self._evaluations)

    def evaluations(self, split_id: str) -> tuple[HoldoutEvaluation, ...]:
        return tuple(row for row in self._evaluations if row.split_id == split_id)

    def evaluate(
        self,
        split_id: str,
        dates: Sequence[date | datetime | str],
        *,
        label: str,
        evaluated_at: str | None = None,
    ) -> HoldoutSplit:
        """Claim the split's one evaluation and record the burn on disk.

        The on-disk record is re-read first: another holder of the same file
        may have spent the split since this object was loaded, and a burn lost
        to last-writer-wins would defeat the whole protocol.
        """
        self._refresh_evaluations()
        burned = self.evaluations(split_id)
        if burned:
            first = burned[0]
            raise HoldoutAlreadyEvaluated(
                f"holdout {split_id!r} was already evaluated as {first.label!r} "
                f"on {first.evaluated_at}; a holdout is single-use by "
                "construction"
            )
        split = self.resolve(split_id, dates)
        self._evaluations.append(
            HoldoutEvaluation(
                split_id=split_id, label=label, evaluated_at=evaluated_at or _now()
            )
        )
        self._save()
        return split

    # -- persistence -----------------------------------------------------

    def _refresh_evaluations(self) -> None:
        """Merge in any burns recorded on disk since this object was loaded."""
        if not self._path.exists():
            return
        on_disk = HoldoutProtocol.load(self._path)
        known = {(e.split_id, e.label, e.evaluated_at) for e in self._evaluations}
        for row in on_disk._evaluations:
            if (row.split_id, row.label, row.evaluated_at) not in known:
                self._evaluations.append(row)

    def _save(self) -> None:
        payload = {
            **self._extra,
            "version": self._version,
            "splits": [
                {
                    "split_id": r.split_id,
                    "holdout_start": r.holdout_start,
                    "horizon": r.horizon,
                    "embargo": r.embargo,
                    "registered_at": r.registered_at,
                    "note": r.note,
                }
                for r in self._registrations.values()
            ],
            "evaluations": [
                {
                    "split_id": e.split_id,
                    "label": e.label,
                    "evaluated_at": e.evaluated_at,
                }
                for e in self._evaluations
            ],
        }
        # Write-then-rename: a truncating write that dies mid-flight would take
        # every registration and every recorded burn with it, and this file is
        # the tamper-evident record of a spent holdout. Key order is fixed
        # rather than sorted so recording a burn produces a one-line diff.
        temp = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temp.write_text(json.dumps(payload, indent=2) + "\n")
        temp.replace(self._path)
