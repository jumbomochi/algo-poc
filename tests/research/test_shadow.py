from __future__ import annotations

from datetime import date

import pandas as pd

from research.factors.engine import FactorSnapshotIndex
from research.shadow import InMemoryShadowRecorder


def test_in_memory_recorder_attaches_factor_snapshot_and_risk_outcome():
    snapshots = FactorSnapshotIndex(
        {
            "momentum@1.0.0": pd.DataFrame(
                {"AAPL": [0.2]}, index=pd.to_datetime(["2026-01-02"])
            )
        }
    )
    recorder = InMemoryShadowRecorder(snapshots)

    recorder.observe(
        portfolio="momentum",
        ticker="AAPL",
        as_of=date(2026, 1, 2),
        signal={"action": "buy", "quantity": 1.0, "limit_price": 100.0},
        risk_approved=False,
        risk_reason="position cap",
    )

    record = recorder.records[0]
    assert record.factor_values == {"momentum@1.0.0": 0.2}
    assert record.risk_approved is False


def test_candidate_key_is_stable_for_equivalent_signal_mappings():
    snapshots = FactorSnapshotIndex({})
    first = InMemoryShadowRecorder(snapshots)
    second = InMemoryShadowRecorder(snapshots)

    first.observe(
        portfolio="momentum",
        ticker="AAPL",
        as_of=date(2026, 1, 2),
        signal={"action": "buy", "quantity": 1.0},
        risk_approved=True,
        risk_reason="approved",
    )
    second.observe(
        portfolio="momentum",
        ticker="AAPL",
        as_of=date(2026, 1, 2),
        signal={"quantity": 1.0, "action": "buy"},
        risk_approved=True,
        risk_reason="approved",
    )

    assert first.records[0].candidate_key == second.records[0].candidate_key
