from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

import pandas as pd


_MARKET_OBSERVATION_FIELDS = ("close", "open", "high", "low", "volume")
_BROADCAST_FIELD_PREFIXES = ("regime:", "universe:")


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
        if (
            not isinstance(self.factor_id, str)
            or not self.factor_id.strip()
            or "@" in self.factor_id
        ):
            raise ValueError("factor_id must be non-empty and cannot contain '@'")
        if (
            re.fullmatch(
                r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)",
                self.version,
            )
            is None
        ):
            raise ValueError("version must use semantic MAJOR.MINOR.PATCH format")
        if self.lookback_days < 1:
            raise ValueError("lookback_days must be at least 1")
        if self.prediction_horizon_days < 1:
            raise ValueError("prediction_horizon_days must be at least 1")
        if self.direction not in (-1, 1):
            raise ValueError("direction must be -1 or 1")
        for field_name in (
            "family",
            "description",
            "economic_rationale",
            "missing_data_policy",
            "normalization_policy",
            "source",
            "license",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        for field_name in (
            "required_fields",
            "supported_sleeves",
            "supported_universes",
        ):
            values = getattr(self, field_name)
            if (
                not isinstance(values, tuple)
                or not values
                or any(
                    not isinstance(value, str) or not value.strip() for value in values
                )
            ):
                raise ValueError(f"{field_name} must contain non-empty text values")

    @property
    def key(self) -> str:
        return f"{self.factor_id}@{self.version}"


@dataclass(frozen=True, init=False, slots=True)
class FactorPanel:
    _fields: Mapping[str, pd.DataFrame]
    as_of: date

    def __init__(self, fields: Mapping[str, pd.DataFrame], as_of: date) -> None:
        owned = {name: frame.copy(deep=True) for name, frame in fields.items()}
        frames = list(owned.values())
        if not frames:
            raise ValueError("fields must be non-empty")
        first = frames[0]
        for frame in frames[1:]:
            if not frame.index.equals(first.index) or not frame.columns.equals(
                first.columns
            ):
                raise ValueError(
                    "all factor fields must have the same index and columns"
                )
        if len(first.index) and first.index.max().date() > as_of:
            raise ValueError("panel contains observations after as_of")
        object.__setattr__(self, "_fields", MappingProxyType(owned))
        object.__setattr__(self, "as_of", as_of)

    def field(self, name: str) -> pd.DataFrame:
        try:
            return self._fields[name].copy(deep=True)
        except KeyError as exc:
            raise KeyError(f"factor panel is missing required field '{name}'") from exc

    def observation_dates(self) -> tuple[date, ...]:
        first = next(iter(self._fields.values()))
        return tuple(pd.Timestamp(timestamp).date() for timestamp in first.index)

    def input_artifact_checksum(self, as_of: date | None = None) -> str:
        cutoff = as_of or self.as_of
        timestamp = pd.Timestamp(cutoff)
        eligible_columns = list(self._eligible_columns(cutoff))
        field_payloads = {}
        for name, frame in sorted(self._fields.items()):
            eligible = frame.loc[frame.index <= timestamp, eligible_columns]
            if eligible.notna().to_numpy().any():
                field_payloads[name] = _frame_payload(eligible)
        payload = {
            "as_of": cutoff.isoformat(),
            "fields": field_payloads,
        }
        return _sha256(payload)

    def universe_snapshot_id(self, as_of: date | None = None) -> str:
        cutoff = as_of or self.as_of
        if "universe:member" in self._fields and len(
            self._fields["universe:member"].index
        ):
            frame = self._fields["universe:member"]
            eligible = frame.loc[frame.index <= pd.Timestamp(cutoff)]
            if eligible.empty:
                active_members: tuple[str, ...] = ()
                effective_at = None
            else:
                current = eligible.iloc[-1]
                active_members = tuple(
                    str(column) for column in self._eligible_columns(cutoff)
                )
                effective_at = None
                if current.notna().any():
                    effective_timestamp = eligible.index[-1]
                    for timestamp in reversed(eligible.index[:-1]):
                        previous = eligible.loc[timestamp]
                        previous_members = tuple(
                            sorted(
                                str(column)
                                for column, value in previous.items()
                                if value == 1.0
                            )
                        )
                        if (
                            previous.notna().any()
                            and previous_members == active_members
                        ):
                            effective_timestamp = timestamp
                        else:
                            break
                    effective_at = pd.Timestamp(effective_timestamp).isoformat()
            payload: Any = {
                "snapshot_effective_at": effective_at,
                "active_members": active_members,
            }
        else:
            payload = {
                "implicit_tickers": [
                    str(column) for column in self._eligible_columns(cutoff)
                ],
            }
        return _sha256(payload)

    def _eligible_columns(self, as_of: date) -> tuple[Any, ...]:
        timestamp = pd.Timestamp(as_of)
        membership = self._fields.get("universe:member")
        if membership is not None:
            eligible_rows = membership.loc[membership.index <= timestamp]
            if eligible_rows.empty:
                return ()
            latest = eligible_rows.iloc[-1]
            columns = [column for column, value in latest.items() if value == 1.0]
        else:
            if "close" in self._fields:
                evidence_fields = (self._fields["close"],)
            else:
                market_fields = tuple(
                    self._fields[name]
                    for name in _MARKET_OBSERVATION_FIELDS
                    if name in self._fields
                )
                evidence_fields = market_fields or tuple(
                    frame
                    for name, frame in sorted(self._fields.items())
                    if not name.startswith(_BROADCAST_FIELD_PREFIXES)
                )
            columns = []
            for column in next(iter(self._fields.values())).columns:
                if any(
                    frame.loc[frame.index <= timestamp, column].notna().any()
                    for frame in evidence_fields
                ):
                    columns.append(column)
        return tuple(sorted(columns, key=str))


def _sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _frame_payload(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "index": [_canonical_value(value) for value in frame.index],
        "columns": [_canonical_value(value) for value in frame.columns],
        "dtypes": [str(dtype) for dtype in frame.dtypes],
        "values": [
            [_canonical_value(value) for value in row]
            for row in frame.to_numpy(dtype=object).tolist()
        ],
    }


def _canonical_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (date, pd.Timestamp)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, float) and not pd.notna(value):
        return None
    return value


@runtime_checkable
class Factor(Protocol):
    spec: FactorSpec

    def compute(self, panel: FactorPanel) -> pd.DataFrame: ...
