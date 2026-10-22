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
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "trial_registry.json"


#: A trial whose objective was a return (its Sharpe is a draw from the search).
RETURN_OBJECTIVE = "return"
#: A trial built to pay off in crashes; a negative Sharpe is its design, not
#: selection noise, so D22 keeps it out of SR*'s spread (it stays in the count).
INSURANCE_OBJECTIVE = "insurance"
_OBJECTIVES = (RETURN_OBJECTIVE, INSURANCE_OBJECTIVE)


@dataclass(frozen=True)
class TrialRecord:
    """One trial's annualised full-sample Sharpe, as scored at its verdict.

    ``sharpe`` is ``None`` for a candidate registered before it is scored.
    """

    name: str
    sharpe: float | None
    objective: str


@dataclass(frozen=True)
class TrialEntry:
    """One recorded search: what was tried, when, and how many ways."""

    searched_at: str
    what: str
    n_trials: int
    source: str
    trials: tuple[TrialRecord, ...] = ()


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
        trials = tuple(_parse_trial(t, resolved) for t in row.get("trials") or ())
        if len(trials) > n_trials:
            raise ValueError(
                f"{row.get('what')!r} in {resolved} records {len(trials)} trial "
                f"Sharpes but declares only n_trials={n_trials}"
            )
        parsed.append(
            TrialEntry(
                searched_at=str(row["searched_at"]),
                what=str(row["what"]),
                n_trials=n_trials,
                source=str(row["source"]),
                trials=trials,
            )
        )
    names = [t.name for e in parsed for t in e.trials]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        raise ValueError(f"trial names recorded twice in {resolved}: {', '.join(duplicated)}")
    return TrialRegistry(version=int(payload.get("version", 1)), entries=tuple(parsed))


def _parse_trial(row: Mapping, resolved: Path) -> TrialRecord:
    objective = str(row["objective"])
    if objective not in _OBJECTIVES:
        raise ValueError(
            f"trial {row.get('name')!r} in {resolved} has objective "
            f"{objective!r}; expected one of {_OBJECTIVES}"
        )
    sharpe = None if row.get("sharpe") is None else float(row["sharpe"])
    if sharpe is not None and not math.isfinite(sharpe):
        raise ValueError(f"trial {row.get('name')!r} in {resolved} has a non-finite Sharpe")
    return TrialRecord(name=str(row["name"]), sharpe=sharpe, objective=objective)


def declared_trial_count(path: Path | str | None = None) -> int:
    return load_trial_registry(path).n_trials


def spread_sharpes(
    in_run: Mapping[str, float],
    *,
    periods_per_year: float,
    path: Path | str | None = None,
) -> list[float]:
    """D22's sample for SR*'s spread, in the per-period units ``control`` uses.

    Every return-objective trial on record, not only the ones in this run: the
    run holds the survivors, and estimating the spread from survivors alone
    under-deflates (edge-validation-framework.md, limit 1). A trial in the run
    contributes its in-run Sharpe (``in_run``, per-period); one that is not
    contributes the annualised Sharpe recorded at its verdict. Insurance trials
    are left out. Every name in ``in_run`` must be registered first, because
    the registry is where its objective is declared.
    """
    records = {t.name: t for e in load_trial_registry(path).entries for t in e.trials}
    unregistered = sorted(set(in_run) - set(records))
    if unregistered:
        raise ValueError(
            f"{', '.join(unregistered)} not in the trial registry; register a "
            "candidate (with its objective) before scoring it"
        )
    unscored = sorted(
        name for name, rec in records.items()
        if name not in in_run and rec.objective == RETURN_OBJECTIVE and rec.sharpe is None
    )
    if unscored:
        raise ValueError(
            f"{', '.join(unscored)} registered with no Sharpe and not in this run; "
            "record its verdict's Sharpe before scoring anything after it"
        )
    scale = math.sqrt(periods_per_year)
    return [
        float(in_run[name]) if name in in_run else rec.sharpe / scale
        for name, rec in records.items()
        if rec.objective == RETURN_OBJECTIVE
    ]
