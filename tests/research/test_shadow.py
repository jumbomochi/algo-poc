from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from research.factors.engine import CalculationProvenance, FactorSnapshotIndex
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


def make_snapshots(frames=None, *, input_seed="input"):
    def checksum(value):
        return "sha256:" + value.encode().hex().ljust(64, "0")[:64]

    return FactorSnapshotIndex(
        frames or {},
        provenance=CalculationProvenance(
            data_cutoff=date(2026, 1, 2),
            universe_snapshot_id=checksum("universe"),
            code_revision=checksum("code"),
            input_artifact_checksum=checksum(input_seed),
        ),
    )


def test_in_memory_recorder_attaches_factor_snapshot_and_risk_outcome():
    snapshots = make_snapshots(
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
    assert record.provenance == dict(snapshots.provenance.to_mapping())


def test_candidate_key_includes_snapshot_identity_but_identical_reruns_are_stable():
    kwargs = dict(
        portfolio="momentum",
        ticker="AAPL",
        as_of=date(2026, 1, 2),
        signal={"action": "buy"},
        risk_approved=True,
        risk_reason="approved",
    )
    first = InMemoryShadowRecorder(make_snapshots(input_seed="one"))
    rerun = InMemoryShadowRecorder(make_snapshots(input_seed="one"))
    changed = InMemoryShadowRecorder(make_snapshots(input_seed="two"))

    first.observe(**kwargs)
    rerun.observe(**kwargs)
    changed.observe(**kwargs)

    assert first.records[0].candidate_key == rerun.records[0].candidate_key
    assert first.records[0].candidate_key != changed.records[0].candidate_key
    exported = first.records[0].to_dict()
    exported["provenance"]["code_revision"] = "mutated"
    assert first.records[0].provenance["code_revision"] != "mutated"


def test_candidate_key_is_stable_for_equivalent_signal_mappings():
    snapshots = make_snapshots()
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
    recorder = InMemoryShadowRecorder(make_snapshots())
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
        record.snapshot_identity,
    )


def test_sql_recorder_is_idempotent():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    snapshots = make_snapshots()
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


def test_sql_recorder_persists_provenance_and_distinct_snapshots():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    kwargs = dict(
        portfolio="momentum",
        ticker="AAPL",
        as_of=date(2026, 1, 2),
        signal={"action": "buy"},
        risk_approved=True,
        risk_reason="approved",
    )

    SQLShadowRecorder(session, make_snapshots(input_seed="one")).observe(**kwargs)
    SQLShadowRecorder(session, make_snapshots(input_seed="two")).observe(**kwargs)

    stored = session.scalars(select(ResearchCandidate)).all()
    assert len(stored) == 2
    assert {row.provenance["input_artifact_checksum"] for row in stored} == {
        make_snapshots(input_seed="one").provenance.input_artifact_checksum,
        make_snapshots(input_seed="two").provenance.input_artifact_checksum,
    }


def test_sql_recorder_owns_one_consistent_signal_snapshot_during_persistence():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    signal = {
        "action": "buy",
        "signals": {"momentum": {"score": 0.8}},
    }

    class MutatingSnapshots:
        provenance = make_snapshots().provenance
        snapshot_identity = make_snapshots().snapshot_identity

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
        MutatingSnapshots.snapshot_identity,
    )


def test_sql_recorder_rolls_back_and_reraises_signal_snapshot_failure():
    session, verification_session = database_sessions(RollbackTrackingSession)
    recorder = SQLShadowRecorder(session, make_snapshots())

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
    recorder = SQLShadowRecorder(session, make_snapshots())

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
    recorder = SQLShadowRecorder(session, make_snapshots())

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
