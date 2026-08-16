"""KAN-31: a failed publish must not report success.

``run_paper.py`` commits the day's book, then publishes the outbox to
``stream:recommendations`` so risk and execution act on it. That publish is
deliberately *after* the commit (see the comment above the bridge) so a down
pipeline never blocks the simulated book, which is the divergence benchmark.

The failure mode this file pins: before KAN-31 the publish failure printed a
warning, the process still exited 0, and ``run_pipeline_report.sh`` — which
decides the daily Telegram status by grepping the paper log for
``exit code: 0`` — reported "✅ paper run OK". A stack that never received a
single order looked exactly like a clean trading day.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from scripts.paper_state import PaperTradingState
from shared.models.base import Base
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.order_ledger import OrderIntent, OrderStatus
from shared.models.portfolio import Position

REPO_ROOT = Path(__file__).resolve().parents[2]

NOW = datetime(2026, 8, 16, 8, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures — a committed book plus one unpublished intent in the outbox
# ---------------------------------------------------------------------------


def make_session(db_url: str = "sqlite:///:memory:"):
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@pytest.fixture
def session():
    s = make_session()
    yield s
    s.close()


@pytest.fixture
def committed_book(session):
    """The state a daily run has already committed before the publish bridge."""
    PaperTradingState.create_new(
        portfolio_capitals={"momentum": 10_000.0}, session=session
    )
    session.add(
        Position(
            portfolio="momentum",
            ticker="AAPL",
            quantity=2.0,
            avg_entry_price=100.0,
            current_price=101.0,
            peak_price=101.0,
            highest_price_since_entry=101.0,
            opened_at=NOW,
        )
    )
    session.add(
        EquitySnapshot(
            portfolio="momentum",
            date=date(2026, 8, 16),
            equity=10_000.0,
            cash=9_798.0,
            market_value=202.0,
            created_at=NOW,
        )
    )
    session.add(
        OrderIntent(
            recommendation_id="sleeve-2026-08-16-momentum-MSFT-buy",
            account_id="DUN551088",
            mode="paper",
            portfolio="momentum",
            con_id=272093,
            symbol="MSFT",
            exchange="SMART",
            currency="USD",
            action="buy",
            requested_quantity=3.0,
            limit_price=410.0,
            order_type="LMT",
            status=OrderStatus.PROPOSED.value,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.commit()
    return session


class FakeRedis:
    """Records xadds. Raises for streams listed in ``unreachable``."""

    def __init__(self, unreachable: tuple[str, ...] = ()):
        self.unreachable = unreachable
        self.published: list[tuple[str, dict]] = []
        self.closed = False

    def xadd(self, stream: str, payload: dict):
        if stream in self.unreachable:
            raise ConnectionError(f"Error connecting to {stream}")
        self.published.append((stream, payload))
        return b"1-0"

    def close(self):
        self.closed = True


def alerts_from(fake: FakeRedis) -> list[dict]:
    return [p for stream, p in fake.published if stream == "stream:alerts"]


# ---------------------------------------------------------------------------
# AC1/AC3/AC5 — the failure is loud, the book survives it
# ---------------------------------------------------------------------------


class TestPublishFailure:
    def test_an_unreachable_redis_yields_a_nonzero_exit_code(
        self, committed_book, monkeypatch
    ):
        """AC3: the publish bridge reports failure as a process exit code."""
        from scripts import run_paper

        def unreachable(url, **kwargs):
            raise ConnectionError(f"Error connecting to {url}")

        monkeypatch.setattr(run_paper, "_redis_from_url", unreachable)

        assert (
            run_paper.publish_bridge(
                committed_book,
                redis_url="redis://unreachable:6379/0",
                account_id="DUN551088",
                entries_allowed=True,
                broker_snapshot=None,
            )
            == 1
        )

    def test_the_committed_book_survives_a_publish_failure(
        self, committed_book, monkeypatch
    ):
        """AC1: positions and equity for the day are recorded as on a clean run.

        This is the property the post-commit ordering exists to protect — the
        simulated book is the divergence benchmark and must not be hostage to
        the pipeline being up.
        """
        from scripts import run_paper

        monkeypatch.setattr(
            run_paper,
            "_redis_from_url",
            lambda url, **kwargs: FakeRedis(unreachable=("stream:recommendations",)),
        )

        run_paper.publish_bridge(
            committed_book,
            redis_url="redis://localhost:6379/0",
            account_id="DUN551088",
            entries_allowed=True,
            broker_snapshot=None,
        )

        assert committed_book.query(Position).count() == 1
        assert committed_book.query(EquitySnapshot).count() == 1
        intent = committed_book.query(OrderIntent).one()
        assert intent.published_at is None, "an unpublished intent must stay replayable"
        assert intent.status == OrderStatus.PROPOSED.value

    def test_the_diagnostic_warning_is_still_printed(
        self, committed_book, monkeypatch, capsys
    ):
        """AC5: making the failure loud must not cost the diagnosis."""
        from scripts import run_paper

        monkeypatch.setattr(
            run_paper,
            "_redis_from_url",
            lambda url, **kwargs: FakeRedis(unreachable=("stream:recommendations",)),
        )

        run_paper.publish_bridge(
            committed_book,
            redis_url="redis://localhost:6379/0",
            account_id="DUN551088",
            entries_allowed=True,
            broker_snapshot=None,
        )

        out = capsys.readouterr().out
        assert "WARNING: publish to pipeline failed" in out
        assert "intents remain replayable" in out

    def test_a_clean_publish_yields_exit_code_zero(self, committed_book, monkeypatch):
        """A working pipeline is unchanged: the intent goes out and the run passes."""
        from scripts import run_paper

        fake = FakeRedis()
        monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kwargs: fake)

        code = run_paper.publish_bridge(
            committed_book,
            redis_url="redis://localhost:6379/0",
            account_id="DUN551088",
            entries_allowed=True,
            broker_snapshot=None,
        )

        assert code == 0
        assert [s for s, _ in fake.published] == ["stream:recommendations"]
        assert committed_book.query(OrderIntent).one().published_at is not None
        assert alerts_from(fake) == []


# ---------------------------------------------------------------------------
# AC2 — someone is told
# ---------------------------------------------------------------------------


class TestPublishFailureAlert:
    def test_a_high_priority_publish_failed_alert_is_emitted(
        self, committed_book, monkeypatch
    ):
        """AC2: the notifications service pages on stream:alerts."""
        from scripts import run_paper

        fake = FakeRedis(unreachable=("stream:recommendations",))
        monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kwargs: fake)

        run_paper.publish_bridge(
            committed_book,
            redis_url="redis://localhost:6379/0",
            account_id="DUN551088",
            entries_allowed=True,
            broker_snapshot=None,
        )

        alerts = alerts_from(fake)
        assert len(alerts) == 1
        assert alerts[0]["event_type"] == "publish_failed"
        assert alerts[0]["priority"] == "high"

    def test_the_alert_names_the_script_and_the_error(
        self, committed_book, monkeypatch
    ):
        """An operator reading the Telegram message must know what broke."""
        from scripts import run_paper

        fake = FakeRedis(unreachable=("stream:recommendations",))
        monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kwargs: fake)

        run_paper.publish_bridge(
            committed_book,
            redis_url="redis://localhost:6379/0",
            account_id="DUN551088",
            entries_allowed=True,
            broker_snapshot=None,
        )

        message = alerts_from(fake)[0]["message"]
        assert "run_paper.py" in message
        assert "Error connecting to stream:recommendations" in message
        assert "replayable" in message

    def test_the_alert_never_carries_the_redis_password(
        self, committed_book, monkeypatch
    ):
        """stream:alerts fans out to Telegram; a DSN password must not ride along.

        The URL is not percent-encoded — `secrets.sh` interpolates whatever the
        operator typed — so the redaction runs to the last '@'.
        """
        from scripts import run_paper

        fake = FakeRedis(unreachable=("stream:recommendations",))
        secret_url = "redis://default:p@ssw0rd@redis:6379/0"

        def connect(url, **kwargs):
            if url == secret_url and "socket_connect_timeout" not in kwargs:
                raise ConnectionError(f"Error 111 connecting to {url}")
            return fake

        monkeypatch.setattr(run_paper, "_redis_from_url", connect)

        run_paper.publish_bridge(
            committed_book,
            redis_url=secret_url,
            account_id="DUN551088",
            entries_allowed=True,
            broker_snapshot=None,
        )

        payload = alerts_from(fake)[0]
        assert "p@ssw0rd" not in str(payload)
        assert "redis://default:***@" in payload["message"]

    def test_an_unreachable_alert_path_still_exits_one_without_raising(
        self, committed_book, monkeypatch
    ):
        """AC2/AC3: Redis being down breaks the alert too — the exit code must
        survive that, since it is the signal the launchd wrapper acts on."""
        from scripts import run_paper

        def unreachable(url, **kwargs):
            raise ConnectionError("Error 111 connecting to redis:6379")

        monkeypatch.setattr(run_paper, "_redis_from_url", unreachable)

        assert (
            run_paper.publish_bridge(
                committed_book,
                redis_url="redis://redis:6379/0",
                account_id="DUN551088",
                entries_allowed=True,
                broker_snapshot=None,
            )
            == 1
        )

    def test_the_alert_attempt_cannot_hang_the_job(self, committed_book, monkeypatch):
        """A Redis that accepts the socket but never answers would otherwise
        block the 04:15 job past its window; the alert connection is bounded."""
        from scripts import run_paper

        seen: list[dict] = []

        def connect(url, **kwargs):
            seen.append(kwargs)
            if len(seen) == 1:
                raise ConnectionError("publish leg down")
            return FakeRedis()

        monkeypatch.setattr(run_paper, "_redis_from_url", connect)

        run_paper.publish_bridge(
            committed_book,
            redis_url="redis://redis:6379/0",
            account_id="DUN551088",
            entries_allowed=True,
            broker_snapshot=None,
        )

        assert seen[1]["socket_connect_timeout"] > 0
        assert seen[1]["socket_timeout"] > 0


# ---------------------------------------------------------------------------
# AC3 — main()'s return value reaches the process, existing codes unchanged
# ---------------------------------------------------------------------------


class TestProcessExitCodes:
    """Run the real script. `sys.exit(main())` is the wiring under test: a bare
    `return` must still exit 0 and every pre-existing `sys.exit` must keep its
    code."""

    def run_script(self, *argv: str, db_url: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "scripts/run_paper.py", "--db-url", db_url, *argv],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=180,
        )

    def test_the_entry_point_exits_with_mains_return_value(self):
        """The missing link before KAN-31: `main()` was invoked bare, so every
        return — including the publish-failure one — landed on exit 0.

        Asserted on the source because no unit test can observe the interpreter's
        exit status of the daily path, which needs a live IB connection.
        """
        source = (REPO_ROOT / "scripts/run_paper.py").read_text()
        tail = source.split('if __name__ == "__main__":')[-1]

        assert "sys.exit(main())" in tail
        assert "\n    main()" not in tail

    def test_init_exits_zero(self, tmp_path):
        """The bare `return` paths must survive `sys.exit(main())`."""
        db_url = f"sqlite:///{tmp_path / 'paper.db'}"
        make_session(db_url).close()

        result = self.run_script("--init", db_url=db_url)

        assert result.returncode == 0, result.stdout + result.stderr

    def test_status_without_state_still_exits_one(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'paper.db'}"
        make_session(db_url).close()

        result = self.run_script("--status", db_url=db_url)

        assert result.returncode == 1, result.stdout + result.stderr

    def test_status_with_state_exits_zero(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'paper.db'}"
        session = make_session(db_url)
        PaperTradingState.create_new(
            portfolio_capitals={"momentum": 10_000.0}, session=session
        )
        session.commit()
        session.close()

        result = self.run_script("--status", db_url=db_url)

        assert result.returncode == 0, result.stdout + result.stderr

    def test_an_invalid_portfolio_tag_still_exits_two(self, tmp_path):
        db_url = f"sqlite:///{tmp_path / 'paper.db'}"

        result = self.run_script(
            "--portfolio-tag", "momentum", "--portfolio-tag-capital", "500",
            db_url=db_url,
        )

        assert result.returncode == 2, result.stdout + result.stderr
