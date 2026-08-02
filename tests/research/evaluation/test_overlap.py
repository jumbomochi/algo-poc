from __future__ import annotations

from datetime import date

import pandas as pd

from research.evaluation.overlap import (
    attribute,
    baseline_selections_from_records,
)


def test_baseline_selections_group_buys_by_date():
    records = [
        {"as_of": "2026-01-05", "ticker": "AAPL", "action": "buy"},
        {"as_of": date(2026, 1, 5), "ticker": "MSFT", "action": "buy"},
        {"as_of": "2026-01-05", "ticker": "NVDA", "action": "sell"},
    ]
    selections = baseline_selections_from_records(records)
    assert selections[date(2026, 1, 5)] == {"AAPL", "MSFT"}


def test_cohorts_partition_and_average_returns():
    day = pd.Timestamp("2026-01-05")
    forward = pd.DataFrame({"A": [0.10], "B": [0.20], "C": [0.30]}, index=[day])
    factor_selections = {day.date(): {"A", "B"}}
    baseline_selections = {day.date(): {"B", "C"}}
    report = attribute(factor_selections, baseline_selections, forward)
    assert report.counts == {"research_only": 1, "overlap": 1, "baseline_only": 1}
    assert round(report.cohort_returns["research_only"], 6) == 0.10  # A
    assert round(report.cohort_returns["overlap"], 6) == 0.20        # B
    assert round(report.cohort_returns["baseline_only"], 6) == 0.30  # C
