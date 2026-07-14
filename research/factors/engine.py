from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

import numpy as np
import pandas as pd

from research.factors.contracts import FactorPanel
from research.factors.registry import FactorRegistry


@dataclass(frozen=True)
class FactorSnapshotIndex:
    frames: dict[str, pd.DataFrame]

    def values_for(self, as_of: date, ticker: str) -> dict[str, float]:
        timestamp = pd.Timestamp(as_of)
        values: dict[str, float] = {}
        for key, frame in self.frames.items():
            if timestamp not in frame.index or ticker not in frame.columns:
                continue
            value = frame.at[timestamp, ticker]
            if pd.notna(value) and np.isfinite(float(value)):
                values[key] = float(value)
        return values


class FactorEngine:
    def __init__(self, registry: FactorRegistry) -> None:
        self._registry = registry

    def compute(
        self,
        panel: FactorPanel,
        factor_ids: Iterable[str],
    ) -> FactorSnapshotIndex:
        frames: dict[str, pd.DataFrame] = {}
        for factor_id in factor_ids:
            factor = self._registry.get(factor_id)
            for field in factor.spec.required_fields:
                panel.field(field)
            output = factor.compute(panel)
            reference = panel.field(factor.spec.required_fields[0])
            if not output.index.equals(reference.index) or not output.columns.equals(
                reference.columns
            ):
                raise ValueError(f"factor '{factor_id}' returned a misaligned frame")
            frames[factor.spec.key] = output.astype(float)
        return FactorSnapshotIndex(frames=frames)
