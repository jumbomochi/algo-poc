from __future__ import annotations

from research.factors.contracts import Factor, FactorSpec


class FactorRegistry:
    def __init__(self) -> None:
        self._factors: dict[str, Factor] = {}

    def register(self, factor: Factor) -> None:
        factor_id = factor.spec.factor_id
        if factor_id in self._factors:
            raise ValueError(f"factor '{factor_id}' is already registered")
        self._factors[factor_id] = factor

    def get(self, factor_id: str) -> Factor:
        try:
            return self._factors[factor_id]
        except KeyError as exc:
            raise KeyError(f"unknown factor '{factor_id}'") from exc

    def list_specs(self) -> tuple[FactorSpec, ...]:
        return tuple(self._factors[key].spec for key in sorted(self._factors))
