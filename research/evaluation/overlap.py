from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

_COHORTS = ("research_only", "overlap", "baseline_only")


@dataclass(frozen=True)
class OverlapReport:
    counts: dict[str, int]
    cohort_returns: dict[str, float]


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def baseline_selections_from_records(records: list[dict]) -> dict[date, set[str]]:
    selections: dict[date, set[str]] = {}
    for record in records:
        if str(record.get("action")) != "buy":
            continue
        day = _as_date(record["as_of"])
        selections.setdefault(day, set()).add(str(record["ticker"]))
    return selections


def attribute(
    factor_selections: dict[date, set[str]],
    baseline_selections: dict[date, set[str]],
    forward: pd.DataFrame,
) -> OverlapReport:
    buckets: dict[str, list[float]] = {cohort: [] for cohort in _COHORTS}
    for day, factor_names in factor_selections.items():
        baseline_names = baseline_selections.get(day, set())
        timestamp = pd.Timestamp(day)
        if timestamp not in forward.index:
            continue
        fwd_row = forward.loc[timestamp]
        for ticker in factor_names | baseline_names:
            if ticker not in fwd_row or pd.isna(fwd_row[ticker]):
                continue
            in_factor = ticker in factor_names
            in_baseline = ticker in baseline_names
            if in_factor and in_baseline:
                cohort = "overlap"
            elif in_factor:
                cohort = "research_only"
            else:
                cohort = "baseline_only"
            buckets[cohort].append(float(fwd_row[ticker]))
    counts = {cohort: len(values) for cohort, values in buckets.items()}
    cohort_returns = {
        cohort: (float(np.mean(values)) if values else 0.0) for cohort, values in buckets.items()
    }
    return OverlapReport(counts=counts, cohort_returns=cohort_returns)
