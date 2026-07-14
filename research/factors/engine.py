from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date
import hashlib
import inspect
import json
from types import MappingProxyType

import numpy as np
import pandas as pd

import research.factors.operations as factor_operations
import research.factors.panel as factor_panel_module
from research.factors.contracts import FactorPanel
from research.factors.operations import cross_sectional_zscore
from research.factors.registry import FactorRegistry


@dataclass(frozen=True, slots=True)
class CalculationProvenance:
    data_cutoff: date
    universe_snapshot_id: str
    code_revision: str
    input_artifact_checksum: str

    def to_mapping(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                "data_cutoff": self.data_cutoff.isoformat(),
                "universe_snapshot_id": self.universe_snapshot_id,
                "code_revision": self.code_revision,
                "input_artifact_checksum": self.input_artifact_checksum,
            }
        )

    @property
    def identity(self) -> str:
        payload = json.dumps(
            dict(self.to_mapping()), sort_keys=True, separators=(",", ":")
        )
        return _checksum(payload.encode())


@dataclass(frozen=True, init=False, slots=True)
class FactorSnapshotIndex:
    _frames: Mapping[str, pd.DataFrame] = field(repr=False)
    _provenance_by_date: Mapping[date, CalculationProvenance] = field(repr=False)
    provenance: CalculationProvenance

    def __init__(
        self,
        frames: Mapping[str, pd.DataFrame],
        provenance: CalculationProvenance,
        provenance_by_date: Mapping[date, CalculationProvenance] | None = None,
    ) -> None:
        copied_frames = {key: frame.copy(deep=True) for key, frame in frames.items()}
        object.__setattr__(self, "_frames", MappingProxyType(copied_frames))
        dated = dict(provenance_by_date or {provenance.data_cutoff: provenance})
        object.__setattr__(self, "_provenance_by_date", MappingProxyType(dated))
        object.__setattr__(self, "provenance", provenance)

    @property
    def snapshot_identity(self) -> str:
        return self.provenance.identity

    def provenance_for(self, as_of: date) -> CalculationProvenance:
        try:
            return self._provenance_by_date[as_of]
        except KeyError as exc:
            raise KeyError(f"no factor provenance for {as_of.isoformat()}") from exc

    def snapshot_identity_for(self, as_of: date) -> str:
        return self.provenance_for(as_of).identity

    def values_for(self, as_of: date, ticker: str) -> dict[str, float]:
        timestamp = pd.Timestamp(as_of)
        values: dict[str, float] = {}
        for key, frame in self._frames.items():
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
        factors = []
        for factor_id in factor_ids:
            factor = self._registry.get(factor_id)
            factors.append(factor)
            for field_name in factor.spec.required_fields:
                panel.field(field_name)
            output = factor.compute(panel)
            reference = panel.field(factor.spec.required_fields[0])
            if not output.index.equals(reference.index) or not output.columns.equals(
                reference.columns
            ):
                raise ValueError(f"factor '{factor_id}' returned a misaligned frame")
            membership = None
            if factor.spec.normalization_policy == "cross_sectional_zscore":
                try:
                    membership = panel.field("universe:member")
                except KeyError as exc:
                    raise ValueError(
                        f"factor '{factor_id}' cross_sectional_zscore requires dated "
                        "'universe:member' field"
                    ) from exc
            frames[factor.spec.key] = _normalize(
                output.astype(float), factor.spec.normalization_policy, membership
            )
        code_revision = _code_revision(factors)
        provenance_by_date = {
            observation_date: _provenance_for(panel, observation_date, code_revision)
            for observation_date in panel.observation_dates()
        }
        provenance = provenance_by_date.get(panel.as_of) or _provenance_for(
            panel, panel.as_of, code_revision
        )
        return FactorSnapshotIndex(
            frames=frames,
            provenance=provenance,
            provenance_by_date=provenance_by_date,
        )


def _provenance_for(
    panel: FactorPanel,
    as_of: date,
    code_revision: str,
) -> CalculationProvenance:
    return CalculationProvenance(
        data_cutoff=as_of,
        universe_snapshot_id=panel.universe_snapshot_id(as_of=as_of),
        code_revision=code_revision,
        input_artifact_checksum=panel.input_artifact_checksum(as_of=as_of),
    )


def _normalize(
    frame: pd.DataFrame,
    policy: str,
    membership: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if policy == "none":
        return frame.copy(deep=True)
    if policy == "cross_sectional_zscore":
        if membership is None:
            raise ValueError(
                "cross_sectional_zscore requires dated 'universe:member' field"
            )
        return cross_sectional_zscore(frame.where(membership.eq(1.0)))
    raise ValueError(f"unknown normalization policy '{policy}'")


def _code_revision(factors: list[object]) -> str:
    definitions = []
    for factor in factors:
        try:
            source = inspect.getsource(type(factor))
        except (OSError, TypeError):
            source = repr(type(factor))
        definitions.append(
            {
                "type": f"{type(factor).__module__}.{type(factor).__qualname__}",
                "spec": asdict(factor.spec),
                "source": source,
            }
        )
    payload = {
        "engine": inspect.getsource(FactorEngine),
        "panel_contract": inspect.getsource(FactorPanel),
        "panel_builder_module": inspect.getsource(factor_panel_module),
        "operations_module": inspect.getsource(factor_operations),
        "normalizer": inspect.getsource(cross_sectional_zscore),
        "factors": sorted(definitions, key=lambda item: item["type"]),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return _checksum(encoded)


def _checksum(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
