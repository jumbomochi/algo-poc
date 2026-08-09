"""Tests for the content_hash backfill operator script.

This script is never run against a real database by an agent — only tested
here against ephemeral sqlite files under tmp_path, same pattern as the
migration tests (tests/migrations/*).
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scripts.ops.backfill_model_hashes import (
    backfill_content_hashes,
    find_rows_needing_backfill,
)
from shared.models.ml_models import ModelVersion


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill.db'}")
    ModelVersion.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    yield db
    db.close()


def _make_row(session, version: str, model_path: str, content_hash: str | None = None) -> ModelVersion:
    row = ModelVersion(
        version=version,
        training_window_start=date(2024, 1, 1),
        training_window_end=date(2024, 6, 30),
        metrics={"accuracy": 0.8},
        model_path=model_path,
        is_active=False,
        created_at=datetime.now(timezone.utc),
        content_hash=content_hash,
    )
    session.add(row)
    session.commit()
    return row


class TestFindRowsNeedingBackfill:
    def test_finds_rows_with_null_content_hash(self, session, tmp_path):
        model_file = tmp_path / "v1.0.0.joblib"
        model_file.write_bytes(b"fake model bytes")
        _make_row(session, "v1.0.0", str(model_file))

        rows = find_rows_needing_backfill(session)

        assert [r.version for r in rows] == ["v1.0.0"]

    def test_excludes_rows_that_already_have_a_hash(self, session, tmp_path):
        model_file = tmp_path / "v1.0.0.joblib"
        model_file.write_bytes(b"fake model bytes")
        _make_row(session, "v1.0.0", str(model_file), content_hash="a" * 64)

        rows = find_rows_needing_backfill(session)

        assert rows == []


class TestBackfillContentHashes:
    def test_dry_run_reports_without_writing(self, session, tmp_path):
        model_file = tmp_path / "v1.0.0.joblib"
        model_file.write_bytes(b"fake model bytes")
        row = _make_row(session, "v1.0.0", str(model_file))
        expected_hash = hashlib.sha256(b"fake model bytes").hexdigest()

        outcomes = backfill_content_hashes(session, apply=False)

        assert len(outcomes) == 1
        assert outcomes[0].status == "would_write"
        assert outcomes[0].content_hash == expected_hash
        # Dry run must not mutate the row.
        assert row.content_hash is None

    def test_apply_writes_and_persists_the_hash(self, session, tmp_path):
        model_file = tmp_path / "v1.0.0.joblib"
        model_file.write_bytes(b"fake model bytes")
        row = _make_row(session, "v1.0.0", str(model_file))
        expected_hash = hashlib.sha256(b"fake model bytes").hexdigest()

        outcomes = backfill_content_hashes(session, apply=True)

        assert outcomes[0].status == "written"
        assert row.content_hash == expected_hash

        # Confirm it's actually committed, not just set on this object.
        session.expire_all()
        reloaded = session.query(ModelVersion).filter_by(version="v1.0.0").one()
        assert reloaded.content_hash == expected_hash

    def test_row_with_missing_model_file_is_skipped_not_written(self, session, tmp_path):
        missing_path = str(tmp_path / "does-not-exist.joblib")
        row = _make_row(session, "v1.0.0", missing_path)

        outcomes = backfill_content_hashes(session, apply=True)

        assert outcomes[0].status == "missing_file"
        assert row.content_hash is None

    def test_rows_with_existing_hash_are_left_untouched(self, session, tmp_path):
        model_file = tmp_path / "v1.0.0.joblib"
        model_file.write_bytes(b"fake model bytes")
        existing_hash = "b" * 64
        row = _make_row(session, "v1.0.0", str(model_file), content_hash=existing_hash)

        outcomes = backfill_content_hashes(session, apply=True)

        assert outcomes == []
        assert row.content_hash == existing_hash

    def test_multiple_rows_are_each_reported(self, session, tmp_path):
        for i in range(3):
            model_file = tmp_path / f"v{i}.joblib"
            model_file.write_bytes(f"model-{i}".encode())
            _make_row(session, f"v{i}", str(model_file))

        outcomes = backfill_content_hashes(session, apply=True)

        assert {o.version for o in outcomes} == {"v0", "v1", "v2"}
        assert all(o.status == "written" for o in outcomes)
