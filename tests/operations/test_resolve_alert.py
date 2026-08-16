"""Gate 5 counts *unresolved* criticals, so there has to be a way to resolve
one. Without this the first critical alert of an epoch blocks the gate forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from scripts.ops.resolve_alert import main
from shared.models.alerts import AlertRecord
from shared.models.base import Base


NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def _seed(database_url: str) -> None:
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        session.add(
            AlertRecord(
                message_id="1-0",
                event_type="ib_disconnected",
                priority="critical",
                message="IB gateway unreachable",
                context={},
                raised_at=NOW - timedelta(days=2),
                recorded_at=NOW - timedelta(days=2),
            )
        )
        session.add(
            AlertRecord(
                message_id="2-0",
                event_type="heartbeat",
                priority="low",
                message="daily run finished",
                context={},
                raised_at=NOW - timedelta(days=1),
                recorded_at=NOW - timedelta(days=1),
            )
        )
        session.commit()


def _records(database_url: str) -> list[AlertRecord]:
    with sessionmaker(bind=create_engine(database_url))() as session:
        return list(session.scalars(select(AlertRecord).order_by(AlertRecord.id)))


class TestResolveAlert:
    def test_list_shows_unresolved_criticals_only(self, tmp_path, capsys):
        database_url = f"sqlite:///{tmp_path / 'a.db'}"
        _seed(database_url)

        code = main(["--database-url", database_url, "--list"])

        out = capsys.readouterr().out
        assert code == 0
        assert "ib_disconnected" in out
        assert "heartbeat" not in out

    def test_resolving_stamps_who_and_when(self, tmp_path):
        database_url = f"sqlite:///{tmp_path / 'b.db'}"
        _seed(database_url)

        code = main(
            ["--database-url", database_url, "--id", "1", "--resolved-by", "huiliang"]
        )

        assert code == 0
        resolved = _records(database_url)[0]
        assert resolved.resolved_by == "huiliang"
        assert resolved.resolved_at is not None

    def test_an_unknown_id_is_an_error_not_a_silent_success(self, tmp_path, capsys):
        database_url = f"sqlite:///{tmp_path / 'c.db'}"
        _seed(database_url)

        code = main(
            ["--database-url", database_url, "--id", "99", "--resolved-by", "huiliang"]
        )

        assert code == 1
        assert "99" in capsys.readouterr().err

    def test_resolving_twice_does_not_rewrite_the_first_resolution(self, tmp_path):
        """The audit trail is who called it first, not who ran the command last."""
        database_url = f"sqlite:///{tmp_path / 'd.db'}"
        _seed(database_url)
        main(["--database-url", database_url, "--id", "1", "--resolved-by", "first"])

        code = main(
            ["--database-url", database_url, "--id", "1", "--resolved-by", "second"]
        )

        assert code == 1
        assert _records(database_url)[0].resolved_by == "first"
