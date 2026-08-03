from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from research.factors.engine import FactorSnapshotIndex
from shared.models.research import ResearchCandidate


@dataclass(frozen=True)
class ShadowCandidateRecord:
    candidate_key: str
    portfolio: str
    ticker: str
    as_of: date
    action: str
    raw_signal: dict[str, Any]
    factor_values: dict[str, float]
    provenance: Mapping[str, str]
    snapshot_identity: str
    risk_approved: bool
    risk_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_key": self.candidate_key,
            "portfolio": self.portfolio,
            "ticker": self.ticker,
            "as_of": self.as_of,
            "action": self.action,
            "raw_signal": deepcopy(self.raw_signal),
            "factor_values": dict(self.factor_values),
            "provenance": dict(self.provenance),
            "snapshot_identity": self.snapshot_identity,
            "risk_approved": self.risk_approved,
            "risk_reason": self.risk_reason,
        }


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
    snapshot_identity: str = "",
) -> str:
    payload = json.dumps(
        [portfolio, ticker, as_of.isoformat(), signal, snapshot_identity],
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
        provenance = self._snapshots.provenance_for(as_of)
        snapshot_identity = self._snapshots.snapshot_identity_for(as_of)
        self.records.append(
            ShadowCandidateRecord(
                candidate_key=candidate_key(
                    portfolio,
                    ticker,
                    as_of,
                    signal_snapshot,
                    snapshot_identity,
                ),
                portfolio=portfolio,
                ticker=ticker,
                as_of=as_of,
                action=str(signal_snapshot["action"]),
                raw_signal=signal_snapshot,
                factor_values=self._snapshots.values_for(as_of, ticker),
                provenance=MappingProxyType(dict(provenance.to_mapping())),
                snapshot_identity=snapshot_identity,
                risk_approved=risk_approved,
                risk_reason=risk_reason,
            )
        )


class SQLShadowRecorder:
    def __init__(self, session: Session, snapshots: FactorSnapshotIndex) -> None:
        self._session = session
        self._snapshots = snapshots

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
        try:
            signal_snapshot = deepcopy(signal)
            provenance = self._snapshots.provenance_for(as_of)
            snapshot_identity = self._snapshots.snapshot_identity_for(as_of)
            key = candidate_key(
                portfolio,
                ticker,
                as_of,
                signal_snapshot,
                snapshot_identity,
            )
            existing_id = self._session.scalar(
                select(ResearchCandidate.id).where(
                    ResearchCandidate.candidate_key == key
                )
            )
            if existing_id is not None:
                return
            self._session.add(
                ResearchCandidate(
                    candidate_key=key,
                    portfolio=portfolio,
                    ticker=ticker,
                    as_of=as_of,
                    action=str(signal_snapshot["action"]),
                    raw_signal=signal_snapshot,
                    factor_values=self._snapshots.values_for(as_of, ticker),
                    provenance=dict(provenance.to_mapping()),
                    risk_approved=risk_approved,
                    risk_reason=risk_reason,
                )
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
