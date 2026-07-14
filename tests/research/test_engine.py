from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import numpy as np
import pytest

from research.factors.catalog import DEFAULT_FACTOR_IDS, build_default_registry
from research.factors.contracts import FactorPanel, FactorSpec
from research.factors.engine import (
    CalculationProvenance,
    FactorEngine,
    FactorSnapshotIndex,
)
from research.factors.registry import FactorRegistry
from research.factors.panel import build_factor_panel


def _test_provenance() -> CalculationProvenance:
    return CalculationProvenance(
        data_cutoff=date(2025, 1, 1),
        universe_snapshot_id="sha256:" + "1" * 64,
        code_revision="sha256:" + "2" * 64,
        input_artifact_checksum="sha256:" + "3" * 64,
    )


def test_engine_returns_versioned_finite_snapshot_values() -> None:
    start = date(2025, 1, 1)
    bars = {
        ticker: [
            {
                "date": start + timedelta(days=i),
                "open": base + i,
                "high": base + i + 1,
                "low": base + i - 1,
                "close": base + i,
                "volume": 1_000 + i,
            }
            for i in range(260)
        ]
        for ticker, base in {"A": 100.0, "B": 200.0}.items()
    }
    panel = build_factor_panel(bars)

    snapshots = FactorEngine(build_default_registry()).compute(
        panel, DEFAULT_FACTOR_IDS
    )
    values = snapshots.values_for(panel.as_of, "A")

    assert set(values) == {f"{factor_id}@1.0.0" for factor_id in DEFAULT_FACTOR_IDS}
    assert all(isinstance(value, float) for value in values.values())
    assert snapshots.provenance.data_cutoff == panel.as_of
    assert snapshots.provenance.universe_snapshot_id.startswith("sha256:")
    assert snapshots.provenance.code_revision.startswith("sha256:")
    assert snapshots.provenance.input_artifact_checksum.startswith("sha256:")
    assert snapshots.snapshot_identity.startswith("sha256:")


def test_default_engine_output_matches_every_catalog_normalization_policy():
    start = date(2025, 1, 1)
    bars = {
        ticker: [
            {
                "date": start + timedelta(days=i),
                "open": base + slope * i,
                "high": base + slope * i + 1,
                "low": base + slope * i - 1,
                "close": base + slope * i,
                "volume": volume + i,
            }
            for i in range(260)
        ]
        for ticker, (base, slope, volume) in {
            "A": (100.0, 1.0, 1_000.0),
            "B": (150.0, 0.5, 2_000.0),
            "C": (200.0, 0.2, 4_000.0),
        }.items()
    }
    panel = build_factor_panel(bars)
    registry = build_default_registry()
    snapshots = FactorEngine(registry).compute(panel, DEFAULT_FACTOR_IDS)

    for factor_id in DEFAULT_FACTOR_IDS:
        factor = registry.get(factor_id)
        assert factor.spec.normalization_policy == "none"
        expected = factor.compute(panel).loc[pd.Timestamp(panel.as_of)]
        actual = {
            ticker: snapshots.values_for(panel.as_of, ticker)[factor.spec.key]
            for ticker in expected.dropna().index
        }
        assert actual == pytest.approx(expected.dropna().to_dict())


def test_engine_applies_declared_cross_sectional_zscore_to_raw_factor_output():
    class RawFactor:
        spec = FactorSpec(
            factor_id="raw",
            version="1.0.0",
            family="test",
            description="raw values",
            economic_rationale="test normalization",
            prediction_horizon_days=1,
            required_fields=("close",),
            supported_sleeves=("momentum",),
            supported_universes=("sp500",),
            lookback_days=1,
            direction=1,
            missing_data_policy="allow_missing",
            normalization_policy="cross_sectional_zscore",
            source="test",
            license="test",
        )

        def compute(self, panel):
            return panel.field("close")

    registry = FactorRegistry()
    factor = RawFactor()
    registry.register(factor)
    panel = FactorPanel(
        fields={
            "close": pd.DataFrame(
                {"A": [1.0], "B": [2.0], "C": [3.0]},
                index=pd.to_datetime(["2026-01-02"]),
            ),
            "universe:member": pd.DataFrame(
                {"A": [1.0], "B": [1.0], "C": [1.0]},
                index=pd.to_datetime(["2026-01-02"]),
            ),
        },
        as_of=date(2026, 1, 2),
    )

    snapshots = FactorEngine(registry).compute(panel, ["raw"])

    assert factor.compute(panel).loc["2026-01-02", "A"] == 1.0
    assert snapshots.values_for(panel.as_of, "A")["raw@1.0.0"] == pytest.approx(
        -np.sqrt(1.5)
    )


def test_cross_sectional_normalization_requires_dated_universe_membership():
    class CrossSectionalFactor:
        spec = FactorSpec(
            **{
                **build_default_registry().get("price_momentum_126d").spec.__dict__,
                "factor_id": "cross_sectional",
                "normalization_policy": "cross_sectional_zscore",
            }
        )

        def compute(self, panel):
            return panel.field("close")

    registry = FactorRegistry()
    registry.register(CrossSectionalFactor())
    panel = FactorPanel(
        fields={
            "close": pd.DataFrame(
                {"A": [1.0], "B": [2.0]},
                index=pd.to_datetime(["2026-01-02"]),
            )
        },
        as_of=date(2026, 1, 2),
    )

    with pytest.raises(
        ValueError,
        match="cross_sectional.*requires dated 'universe:member'",
    ):
        FactorEngine(registry).compute(panel, ["cross_sectional"])


def test_cross_sectional_normalization_masks_additions_removals_and_nonmembers():
    class CrossSectionalFactor:
        spec = FactorSpec(
            **{
                **build_default_registry().get("price_momentum_126d").spec.__dict__,
                "factor_id": "cross_sectional",
                "normalization_policy": "cross_sectional_zscore",
            }
        )

        def compute(self, panel):
            return panel.field("close")

    registry = FactorRegistry()
    registry.register(CrossSectionalFactor())
    index = pd.to_datetime(["2026-01-02", "2026-01-05"])
    membership = pd.DataFrame(
        {"A": [1.0, 0.0], "B": [1.0, 1.0], "C": [0.0, 1.0]}, index=index
    )

    def compute(a_values, c_values):
        panel = FactorPanel(
            fields={
                "close": pd.DataFrame(
                    {"A": a_values, "B": [3.0, 3.0], "C": c_values}, index=index
                ),
                "universe:member": membership,
            },
            as_of=date(2026, 1, 5),
        )
        return FactorEngine(registry).compute(panel, ["cross_sectional"])

    baseline = compute([1.0, 1_000.0], [1_000.0, 5.0])
    mutated = compute([1.0, -999_999.0], [999_999.0, 5.0])

    assert baseline.values_for(date(2026, 1, 2), "A")["cross_sectional@1.0.0"] == -1.0
    assert baseline.values_for(date(2026, 1, 2), "B")["cross_sectional@1.0.0"] == 1.0
    assert baseline.values_for(date(2026, 1, 2), "C") == {}
    assert baseline.values_for(date(2026, 1, 5), "A") == {}
    assert baseline.values_for(date(2026, 1, 5), "B")["cross_sectional@1.0.0"] == -1.0
    assert baseline.values_for(date(2026, 1, 5), "C")["cross_sectional@1.0.0"] == 1.0
    assert baseline.values_for(date(2026, 1, 2), "B") == mutated.values_for(
        date(2026, 1, 2), "B"
    )
    assert baseline.values_for(date(2026, 1, 5), "B") == mutated.values_for(
        date(2026, 1, 5), "B"
    )


def test_engine_rejects_unknown_normalization_policy():
    class UnknownPolicyFactor:
        spec = FactorSpec(
            **{
                **build_default_registry().get("price_momentum_126d").spec.__dict__,
                "factor_id": "unknown_policy",
                "normalization_policy": "mystery",
            }
        )

        def compute(self, panel):
            return panel.field("close")

    registry = FactorRegistry()
    registry.register(UnknownPolicyFactor())
    panel = build_factor_panel({"A": []}, as_of=date.min)

    with pytest.raises(ValueError, match="unknown normalization policy 'mystery'"):
        FactorEngine(registry).compute(panel, ["unknown_policy"])


def test_mutating_factor_cannot_change_later_factor_input_or_caller_frame():
    seen = []

    class MutatingFactor:
        spec = FactorSpec(
            **{
                **build_default_registry().get("price_momentum_126d").spec.__dict__,
                "factor_id": "mutator",
                "normalization_policy": "none",
            }
        )

        def compute(self, panel):
            frame = panel.field("close")
            frame.iloc[:, :] = 999.0
            return frame

    class LaterFactor:
        spec = FactorSpec(**{**MutatingFactor.spec.__dict__, "factor_id": "later"})

        def compute(self, panel):
            frame = panel.field("close")
            seen.append(float(frame.iloc[0, 0]))
            return frame

    registry = FactorRegistry()
    registry.register(MutatingFactor())
    registry.register(LaterFactor())
    caller = pd.DataFrame({"A": [10.0]}, index=pd.to_datetime(["2026-01-02"]))
    panel = FactorPanel(fields={"close": caller}, as_of=date(2026, 1, 2))

    FactorEngine(registry).compute(panel, ["mutator", "later"])

    assert seen == [10.0]
    assert caller.iloc[0, 0] == 10.0


def test_engine_provenance_and_snapshot_identity_are_deterministic_and_sensitive():
    bars = {
        "A": [
            {
                "date": date(2026, 1, 2),
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
            }
        ],
        "B": [
            {
                "date": date(2026, 1, 2),
                "open": 2,
                "high": 2,
                "low": 2,
                "close": 2,
                "volume": 2,
            }
        ],
    }
    engine = FactorEngine(build_default_registry())
    first = engine.compute(build_factor_panel(bars), ["liquidity_20d"])
    rerun = engine.compute(build_factor_panel(bars), ["liquidity_20d"])
    mutated = {ticker: [dict(row) for row in rows] for ticker, rows in bars.items()}
    mutated["A"][0]["close"] = 5
    changed = engine.compute(build_factor_panel(mutated), ["liquidity_20d"])

    assert first.provenance == rerun.provenance
    assert first.snapshot_identity == rerun.snapshot_identity
    assert (
        first.provenance.input_artifact_checksum
        != changed.provenance.input_artifact_checksum
    )
    assert first.snapshot_identity != changed.snapshot_identity
    serialized = first.provenance.to_mapping()
    with pytest.raises(TypeError):
        serialized["data_cutoff"] = "changed"


def test_snapshot_provenance_is_point_in_time_for_each_candidate_date():
    days = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
    bars = {
        ticker: [
            {
                "date": day,
                "open": value,
                "high": value,
                "low": value,
                "close": value,
                "volume": 1,
            }
            for day, value in zip(days, values, strict=True)
        ]
        for ticker, values in {"A": [1, 2, 3], "B": [2, 3, 4], "C": [3, 4, 5]}.items()
    }
    membership = {days[0]: {"A", "B"}, days[2]: {"B", "C"}}
    panel = build_factor_panel(bars, universe_membership_by_date=membership)
    snapshots = FactorEngine(build_default_registry()).compute(panel, ["liquidity_20d"])

    first = snapshots.provenance_for(days[0])
    middle = snapshots.provenance_for(days[1])
    last = snapshots.provenance_for(days[2])
    assert first.data_cutoff == days[0]
    assert middle.data_cutoff == days[1]
    assert last.data_cutoff == days[2]
    assert first.universe_snapshot_id == middle.universe_snapshot_id
    assert middle.universe_snapshot_id != last.universe_snapshot_id
    assert snapshots.snapshot_identity_for(days[0]) == first.identity
    assert first.input_artifact_checksum != middle.input_artifact_checksum
    with pytest.raises(KeyError, match="no factor provenance for 2026-01-03"):
        snapshots.provenance_for(date(2026, 1, 3))


def test_candidate_date_provenance_ignores_future_panel_mutations():
    days = [date(2026, 1, 2), date(2026, 1, 5)]
    bars = {
        "A": [
            {
                "date": day,
                "open": value,
                "high": value,
                "low": value,
                "close": value,
                "volume": 1,
            }
            for day, value in zip(days, [1, 2], strict=True)
        ]
    }
    original = FactorEngine(build_default_registry()).compute(
        build_factor_panel(bars), ["liquidity_20d"]
    )
    bars["A"][-1]["close"] = 999_999
    mutated = FactorEngine(build_default_registry()).compute(
        build_factor_panel(bars), ["liquidity_20d"]
    )

    assert original.provenance_for(days[0]) == mutated.provenance_for(days[0])
    assert original.snapshot_identity_for(days[0]) == mutated.snapshot_identity_for(
        days[0]
    )


@pytest.mark.parametrize("explicit_membership", [False, True])
def test_future_only_ticker_does_not_change_prior_provenance(explicit_membership):
    first = date(2026, 1, 2)
    future = date(2026, 1, 5)

    def bars(include_b_on):
        result = {
            "A": [
                {
                    "date": first,
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                },
                {
                    "date": future,
                    "open": 2,
                    "high": 2,
                    "low": 2,
                    "close": 2,
                    "volume": 2,
                },
            ]
        }
        if include_b_on is not None:
            result["B"] = [
                {
                    "date": include_b_on,
                    "open": 3,
                    "high": 3,
                    "low": 3,
                    "close": 3,
                    "volume": 3,
                }
            ]
        return result

    def provenance(include_b_on):
        memberships = None
        if explicit_membership:
            memberships = {first: {"A"}}
            if include_b_on == future:
                memberships[future] = {"A", "B"}
            elif include_b_on == first:
                memberships[first] = {"A", "B"}
        panel = build_factor_panel(
            bars(include_b_on), universe_membership_by_date=memberships
        )
        snapshots = FactorEngine(build_default_registry()).compute(
            panel, ["liquidity_20d"]
        )
        return snapshots.provenance_for(first), snapshots.snapshot_identity_for(first)

    baseline, baseline_identity = provenance(None)
    future_only, future_only_identity = provenance(future)
    backfilled, backfilled_identity = provenance(first)

    assert future_only == baseline
    assert future_only_identity == baseline_identity
    assert backfilled.input_artifact_checksum != baseline.input_artifact_checksum
    assert backfilled.universe_snapshot_id != baseline.universe_snapshot_id
    assert backfilled_identity != baseline_identity


@pytest.mark.parametrize("explicit_membership", [False, True])
def test_empty_eligible_universe_has_deterministic_provenance(explicit_membership):
    cutoff = date(2026, 1, 2)
    future = date(2026, 1, 5)
    bars = {
        "B": [{"date": future, "open": 3, "high": 3, "low": 3, "close": 3, "volume": 3}]
    }
    membership = {cutoff: set(), future: {"B"}} if explicit_membership else None
    panel = build_factor_panel(
        bars, as_of=future, universe_membership_by_date=membership
    )

    first = panel.universe_snapshot_id(as_of=cutoff)
    second = panel.universe_snapshot_id(as_of=cutoff)

    assert first == second
    assert panel.input_artifact_checksum(as_of=cutoff).startswith("sha256:")


def test_unknown_date_or_ticker_returns_empty_snapshot() -> None:
    panel = build_factor_panel({"A": []}, as_of=date.min)
    index = FactorEngine(build_default_registry()).compute(panel, [])

    assert index.values_for(date(2026, 1, 1), "MISSING") == {}


def test_snapshot_omits_non_finite_values() -> None:
    panel = build_factor_panel({"A": []}, as_of=date.min)
    index = FactorEngine(build_default_registry()).compute(
        panel, ["price_momentum_126d"]
    )

    assert index.values_for(date.min, "A") == {}


def test_engine_rejects_misaligned_factor_output() -> None:
    registry = build_default_registry()
    factor = registry.get("price_momentum_126d")
    object.__setattr__(
        factor,
        "compute",
        lambda panel: pd.DataFrame(
            [[1.0]], index=pd.DatetimeIndex(["2025-01-01"]), columns=["WRONG"]
        ),
    )
    panel: FactorPanel = build_factor_panel(
        {
            "A": [
                {
                    "date": date(2025, 1, 1),
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                }
            ]
        }
    )

    with pytest.raises(
        ValueError, match="factor 'price_momentum_126d' returned a misaligned frame"
    ):
        FactorEngine(registry).compute(panel, ["price_momentum_126d"])


def test_snapshot_defensively_copies_input_frames() -> None:
    timestamp = pd.Timestamp("2025-01-01")
    frame = pd.DataFrame({"A": [1.5]}, index=[timestamp])
    frames = {"factor@1.0.0": frame}
    index = FactorSnapshotIndex(frames=frames, provenance=_test_provenance())

    frame.at[timestamp, "A"] = 99.0
    frames.clear()

    assert index.values_for(date(2025, 1, 1), "A") == {"factor@1.0.0": 1.5}


def test_snapshot_exposes_only_lookup_not_mutable_frames() -> None:
    index = FactorSnapshotIndex(
        frames={
            "factor@1.0.0": pd.DataFrame(
                {"A": [1.5]}, index=[pd.Timestamp("2025-01-01")]
            )
        },
        provenance=_test_provenance(),
    )

    assert not hasattr(index, "frames")
