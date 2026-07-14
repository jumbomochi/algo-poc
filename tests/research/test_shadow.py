from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from research.factors.engine import FactorSnapshotIndex
from research.shadow import InMemoryShadowRecorder, SQLShadowRecorder, candidate_key
from shared.models.base import Base
from shared.models.research import ResearchCandidate


class RollbackTrackingSession(Session):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rollback_calls = 0

    def rollback(self):
        self.rollback_calls += 1
        super().rollback()


class QueryFailingSession(RollbackTrackingSession):
    def scalar(self, *args, **kwargs):
        raise RuntimeError("query unavailable")


class CommitFailingSession(RollbackTrackingSession):
    def commit(self):
        self.flush()
        raise RuntimeError("commit unavailable")


class Uncopyable:
    def __deepcopy__(self, memo):
        raise RuntimeError("snapshot unavailable")


def database_sessions(session_class):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    failing_session = sessionmaker(bind=engine, class_=session_class)()
    verification_session = sessionmaker(bind=engine)()
    return failing_session, verification_session


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


def test_recorder_snapshot_is_unchanged_after_caller_mutates_original_signal():
    recorder = InMemoryShadowRecorder(FactorSnapshotIndex({}))
    signal = {
        "action": "buy",
        "quantity": 1.0,
        "signals": {"momentum": {"score": 0.8}},
    }

    recorder.observe(
        portfolio="momentum",
        ticker="AAPL",
        as_of=date(2026, 1, 2),
        signal=signal,
        risk_approved=True,
        risk_reason="approved",
    )
    signal["signals"]["momentum"]["score"] = -1.0

    record = recorder.records[0]
    assert record.raw_signal["signals"]["momentum"]["score"] == 0.8
    assert record.candidate_key == candidate_key(
        record.portfolio,
        record.ticker,
        record.as_of,
        record.raw_signal,
    )


def test_sql_recorder_is_idempotent():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    snapshots = FactorSnapshotIndex({})
    recorder = SQLShadowRecorder(session, snapshots)
    kwargs = dict(
        portfolio="momentum",
        ticker="AAPL",
        as_of=date(2026, 1, 2),
        signal={"action": "buy"},
        risk_approved=True,
        risk_reason="approved",
    )
    recorder.observe(**kwargs)
    recorder.observe(**kwargs)
    assert len(session.scalars(select(ResearchCandidate)).all()) == 1


def test_sql_recorder_owns_one_consistent_signal_snapshot_during_persistence():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    signal = {
        "action": "buy",
        "signals": {"momentum": {"score": 0.8}},
    }

    class MutatingSnapshots:
        def values_for(self, as_of, ticker):
            signal["signals"]["momentum"]["score"] = -1.0
            return {}

    recorder = SQLShadowRecorder(session, MutatingSnapshots())

    recorder.observe(
        portfolio="momentum",
        ticker="AAPL",
        as_of=date(2026, 1, 2),
        signal=signal,
        risk_approved=True,
        risk_reason="approved",
    )

    stored = session.scalar(select(ResearchCandidate))
    assert stored.raw_signal["signals"]["momentum"]["score"] == 0.8
    assert stored.candidate_key == candidate_key(
        stored.portfolio,
        stored.ticker,
        stored.as_of,
        stored.raw_signal,
    )


def test_sql_recorder_rolls_back_and_reraises_signal_snapshot_failure():
    session, verification_session = database_sessions(RollbackTrackingSession)
    recorder = SQLShadowRecorder(session, FactorSnapshotIndex({}))

    with pytest.raises(RuntimeError, match="snapshot unavailable"):
        recorder.observe(
            portfolio="momentum",
            ticker="AAPL",
            as_of=date(2026, 1, 2),
            signal={"action": "buy", "nested": Uncopyable()},
            risk_approved=True,
            risk_reason="approved",
        )

    assert session.rollback_calls == 1
    assert verification_session.scalar(select(ResearchCandidate)) is None


def test_sql_recorder_rolls_back_and_reraises_query_failure():
    session, verification_session = database_sessions(QueryFailingSession)
    recorder = SQLShadowRecorder(session, FactorSnapshotIndex({}))

    with pytest.raises(RuntimeError, match="query unavailable"):
        recorder.observe(
            portfolio="momentum",
            ticker="AAPL",
            as_of=date(2026, 1, 2),
            signal={"action": "buy"},
            risk_approved=True,
            risk_reason="approved",
        )

    assert session.rollback_calls == 1
    assert verification_session.scalar(select(ResearchCandidate)) is None


def test_sql_recorder_rolls_back_commit_failure_without_partial_record():
    session, verification_session = database_sessions(CommitFailingSession)
    recorder = SQLShadowRecorder(session, FactorSnapshotIndex({}))

    with pytest.raises(RuntimeError, match="commit unavailable"):
        recorder.observe(
            portfolio="momentum",
            ticker="AAPL",
            as_of=date(2026, 1, 2),
            signal={"action": "buy"},
            risk_approved=True,
            risk_reason="approved",
        )

    assert session.rollback_calls == 1
    assert verification_session.scalar(select(ResearchCandidate)) is None
