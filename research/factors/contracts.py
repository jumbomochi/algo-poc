from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Mapping, Protocol, runtime_checkable

import pandas as pd


@dataclass(frozen=True)
class FactorSpec:
    factor_id: str
    version: str
    family: str
    description: str
    economic_rationale: str
    prediction_horizon_days: int
    required_fields: tuple[str, ...]
    supported_sleeves: tuple[str, ...]
    supported_universes: tuple[str, ...]
    lookback_days: int
    direction: int
    missing_data_policy: str
    normalization_policy: str
    source: str
    license: str

    def __post_init__(self) -> None:
        if not self.factor_id or "@" in self.factor_id:
            raise ValueError("factor_id must be non-empty and cannot contain '@'")
        if re.fullmatch(
            r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)",
            self.version,
        ) is None:
            raise ValueError("version must use semantic MAJOR.MINOR.PATCH format")
        if self.lookback_days < 1:
            raise ValueError("lookback_days must be at least 1")
        if self.direction not in (-1, 1):
            raise ValueError("direction must be -1 or 1")
        if not self.required_fields:
            raise ValueError("required_fields must be non-empty")

    @property
    def key(self) -> str:
        return f"{self.factor_id}@{self.version}"


@dataclass(frozen=True)
class FactorPanel:
    fields: Mapping[str, pd.DataFrame]
    as_of: date

    def __post_init__(self) -> None:
        frames = list(self.fields.values())
        if not frames:
            raise ValueError("fields must be non-empty")
        first = frames[0]
        for frame in frames[1:]:
            if not frame.index.equals(first.index) or not frame.columns.equals(first.columns):
                raise ValueError("all factor fields must have the same index and columns")
        if len(first.index) and first.index.max().date() > self.as_of:
            raise ValueError("panel contains observations after as_of")

    def field(self, name: str) -> pd.DataFrame:
        try:
            return self.fields[name]
        except KeyError as exc:
            raise KeyError(f"factor panel is missing required field '{name}'") from exc


@runtime_checkable
class Factor(Protocol):
    spec: FactorSpec

    def compute(self, panel: FactorPanel) -> pd.DataFrame: ...
