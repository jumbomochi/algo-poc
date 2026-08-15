# research/evaluation/trial_registry.py
"""The declared size of the search, as a committed artifact.

A deflated Sharpe is only as honest as the trial count it deflates against,
and no single evaluation run knows that count: the run scoring four factors
today is one slice of a search that also tried eight sleeves before six
survived. This module reads that history from a committed file so the number
is auditable in git rather than invented per run, or -- worse -- inferred
from however many candidates happened to be in the last run.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "trial_registry.json"


@dataclass(frozen=True)
class TrialEntry:
    """One recorded search: what was tried, when, and how many ways."""

    searched_at: str
    what: str
    n_trials: int
    source: str


@dataclass(frozen=True)
class TrialRegistry:
    version: int
    entries: tuple[TrialEntry, ...]

    @property
    def n_trials(self) -> int:
        """Total configurations searched across every recorded entry.

        Summing across entries is deliberately conservative: a candidate that
        survives today was selected against the whole history of the search,
        not just the batch it was scored in.
        """
        return sum(entry.n_trials for entry in self.entries)


def load_trial_registry(path: Path | str | None = None) -> TrialRegistry:
    resolved = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    payload = json.loads(resolved.read_text())
    entries = payload.get("entries") or []
    if not entries:
        raise ValueError(f"no trial-registry entries declared in {resolved}")
    parsed: list[TrialEntry] = []
    for row in entries:
        n_trials = int(row["n_trials"])
        if n_trials < 1:
            raise ValueError(
                f"n_trials must be at least 1 in {resolved}: got {n_trials} "
                f"for {row.get('what')!r}"
            )
        parsed.append(
            TrialEntry(
                searched_at=str(row["searched_at"]),
                what=str(row["what"]),
                n_trials=n_trials,
                source=str(row["source"]),
            )
        )
    return TrialRegistry(version=int(payload.get("version", 1)), entries=tuple(parsed))


def declared_trial_count(path: Path | str | None = None) -> int:
    return load_trial_registry(path).n_trials
