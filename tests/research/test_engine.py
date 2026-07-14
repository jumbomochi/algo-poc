from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from research.factors.catalog import DEFAULT_FACTOR_IDS, build_default_registry
from research.factors.contracts import FactorPanel
from research.factors.engine import FactorEngine
from research.factors.panel import build_factor_panel


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

    assert set(values) == {
        f"{factor_id}@1.0.0" for factor_id in DEFAULT_FACTOR_IDS
    }
    assert all(isinstance(value, float) for value in values.values())


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
