from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Protocol

from research.factors.engine import FactorSnapshotIndex


@dataclass(frozen=True)
class ShadowCandidateRecord:
    candidate_key: str
    portfolio: str
    ticker: str
    as_of: date
    action: str
    raw_signal: dict[str, Any]
    factor_values: dict[str, float]
    risk_approved: bool
    risk_reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CandidateObserver(Protocol):
    def observe(
        self,
        *,
        portfolio: str,
        ticker: str,
        as_of: date,
        signal: dict[str, Any],
        risk_approved: bool,
        risk_reason: str,
    ) -> None: ...


def candidate_key(
    portfolio: str,
    ticker: str,
    as_of: date,
    signal: dict[str, Any],
) -> str:
    payload = json.dumps(
        [portfolio, ticker, as_of.isoformat(), signal],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class InMemoryShadowRecorder:
    def __init__(self, snapshots: FactorSnapshotIndex) -> None:
        self._snapshots = snapshots
        self.records: list[ShadowCandidateRecord] = []

    def observe(
        self,
        *,
        portfolio: str,
        ticker: str,
        as_of: date,
        signal: dict[str, Any],
        risk_approved: bool,
        risk_reason: str,
    ) -> None:
        signal_snapshot = deepcopy(signal)
        self.records.append(
            ShadowCandidateRecord(
                candidate_key=candidate_key(
                    portfolio, ticker, as_of, signal_snapshot
                ),
                portfolio=portfolio,
                ticker=ticker,
                as_of=as_of,
                action=str(signal_snapshot["action"]),
                raw_signal=signal_snapshot,
                factor_values=self._snapshots.values_for(as_of, ticker),
                risk_approved=risk_approved,
                risk_reason=risk_reason,
            )
        )
