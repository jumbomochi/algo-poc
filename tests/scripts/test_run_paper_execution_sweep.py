"""KAN-87 Task 4: connect the pure sweep decision to IB and the daily run.

``services.execution.execution_sweep.plan_sweep`` (Task 3) decides what to do
with a broker execution; this file exercises the two things Task 4 adds
around it in ``scripts/run_paper.py``:

- ``executions_from_ib_fills`` normalizes ``ib_insync`` Fill objects without
  importing ``ib_insync`` itself (tested directly against the module it
  lives on, since that is what's under test here, not run_paper).
- ``run_paper.sweep_executions_best_effort`` is the AC5 boundary: IB being
  unreachable, or anything else going wrong, must report and return, never
  raise — because a raise here would abort the daily run before
  reconciliation runs.

No live IB, no real Redis: IB is monkeypatched at ``run_paper.
read_broker_executions`` and Redis is a small in-memory fake, following the
pattern in ``test_run_paper_publish_failure.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scripts import run_paper
from services.execution.execution_sweep import SweptExecution, executions_from_ib_fills
from shared.models import Base, OrderIntent, OrderStatus

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _submitted_intent(
    session: Session,
    *,
    recommendation_id: str = "rec-unh",
    ib_order_id: str = "148",
    account_id: str = "DUN551088",
    portfolio: str = "momentum",
    quantity: float = 6.0,
) -> OrderIntent:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    intent = OrderIntent(
        recommendation_id=recommendation_id,
        account_id=account_id,
        mode="paper",
        portfolio=portfolio,
        con_id=756733,
        symbol="UNH",
        exchange="SMART",
        currency="USD",
        action="SELL",
        requested_quantity=quantity,
        limit_price=340.0,
        order_type="LMT",
        status=OrderStatus.SUBMITTED.value,
        ib_order_id=ib_order_id,
        created_at=now,
        updated_at=now,
        submitted_at=now,
    )
    session.add(intent)
    session.commit()
    return intent


def _execution(**overrides) -> SweptExecution:
    base = dict(
        execution_id="exec-1",
        account_id="DUN551088",
        ib_order_id="148",
        con_id=756733,
        ticker="UNH",
        exchange="SMART",
        currency="USD",
        side="sell",
        quantity=6.0,
        cumulative_quantity=6.0,
        price=341.22,
        commission=1.0,
        commission_currency="USD",
        executed_at=datetime(2026, 9, 14, 13, 31, tzinfo=UTC),
    )
    base.update(overrides)
    return SweptExecution(**base)


class FakeRedis:
    """Records xadds. Raises for a stream named in ``unreachable``."""

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


# ---------------------------------------------------------------------------
# The adapter — normalizing ib_insync's Fill shape
# ---------------------------------------------------------------------------


def test_a_normalized_ib_fill_becomes_a_swept_execution() -> None:
    """The adapter reads ib_insync's shape without importing it in tests."""
    fill = SimpleNamespace(
        execution=SimpleNamespace(
            execId="0000e1a7.68c1b2d4.01.01",
            acctNumber="DUN551088",
            orderId=148,
            side="SLD",
            shares=6.0,
            cumQty=6.0,
            price=341.22,
            time=datetime(2026, 9, 14, 13, 31, tzinfo=UTC),
        ),
        contract=SimpleNamespace(
            conId=756733, symbol="UNH", exchange="SMART", currency="USD"
        ),
        commissionReport=SimpleNamespace(commission=1.0, currency="USD"),
    )

    [swept] = executions_from_ib_fills([fill])

    assert swept.execution_id == "0000e1a7.68c1b2d4.01.01"
    assert swept.ib_order_id == "148"
    assert swept.side == "sell"  # SLD -> sell, BOT -> buy
    assert swept.quantity == 6.0


def test_an_unrecognized_side_is_skipped_not_raised() -> None:
    """A Fill this codebase cannot classify must not crash the sweep."""
    fill = SimpleNamespace(
        execution=SimpleNamespace(
            execId="x",
            acctNumber="DUN551088",
            orderId=1,
            side="???",
            shares=1.0,
            cumQty=1.0,
            price=1.0,
            time=datetime(2026, 9, 14, 13, 31, tzinfo=UTC),
        ),
        contract=SimpleNamespace(conId=1, symbol="X", exchange="SMART", currency="USD"),
        commissionReport=None,
    )

    assert executions_from_ib_fills([fill]) == []


# ---------------------------------------------------------------------------
# AC5 — the sweep is a recovery path, never a precondition
# ---------------------------------------------------------------------------


def test_ib_unreachable_does_not_change_the_run(monkeypatch, session) -> None:
    """AC5: the sweep is a recovery path, never a precondition."""

    def _boom(**kwargs):
        raise ConnectionError("gateway down")

    monkeypatch.setattr(run_paper, "read_broker_executions", _boom)

    outcome = run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert outcome is None  # reported, not raised


def test_ib_unreachable_prints_a_diagnostic_warning(monkeypatch, session, capsys) -> None:
    def _boom(**kwargs):
        raise ConnectionError("gateway down")

    monkeypatch.setattr(run_paper, "read_broker_executions", _boom)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "gateway down" in out
    assert "could not read executions from IB" in out


def test_the_ib_unreachable_message_redacts_a_dsn(monkeypatch, session, capsys) -> None:
    """FINDING 1: every warning in this function must redact secrets, and
    this fetch-stage one is no exception."""

    def _boom(**kwargs):
        raise ConnectionError(
            "could not connect to postgresql://algo:p@ssw0rd@db:5432/algo_poc"
        )

    monkeypatch.setattr(run_paper, "read_broker_executions", _boom)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    out = capsys.readouterr().out
    assert "p@ssw0rd" not in out
    assert "***@" in out


def test_a_decision_error_does_not_change_the_run_either(
    monkeypatch, session, capsys
) -> None:
    """AC5 says 'or the sweep raises for any reason' — not just IB.

    A failure inside ``plan_sweep`` (or the commit that follows it) must be
    just as harmless as IB being unreachable, but FINDING 2 requires the
    message to say so distinctly: a recurring decision-layer bug must not
    look identical to intermittent IB flakiness in the logs.
    """

    async def _fake_read(**kwargs):
        return [_execution()]

    def _boom(*args, **kwargs):
        raise RuntimeError("ledger blew up")

    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)
    monkeypatch.setattr(run_paper, "plan_sweep", _boom)

    outcome = run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert outcome is None
    out = capsys.readouterr().out
    assert "ledger blew up" in out
    assert "the sweep's own decision logic failed" in out
    # Distinguishable from the fetch-stage message — not the same string.
    assert "could not read executions from IB" not in out


def test_the_decision_error_message_redacts_a_dsn(monkeypatch, session, capsys) -> None:
    """FINDING 1: a SQLAlchemy/psycopg2 failure from session.commit() can
    carry the raw database DSN; it must be redacted like every sibling
    warning in this function, not just the publish-side ones."""

    async def _fake_read(**kwargs):
        return [_execution()]

    def _boom(*args, **kwargs):
        raise RuntimeError(
            "could not connect to server: "
            "postgresql://algo:p@ssw0rd@db:5432/algo_poc"
        )

    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)
    monkeypatch.setattr(run_paper, "plan_sweep", _boom)

    outcome = run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert outcome is None
    out = capsys.readouterr().out
    assert "p@ssw0rd" not in out
    assert "***@" in out


def test_the_sweep_runs_before_reconciliation_and_reuses_the_broker_client_id() -> None:
    """Structural check: no live IB is reachable in CI, so pin the wiring in
    ``main`` the way ``test_the_entry_point_exits_with_mains_return_value``
    pins ``sys.exit(main())`` — by reading the source.

    Covers: insertion point (before ``prepare_daily_run``, i.e. before
    reconciliation), and that the sweep reuses ``args.ib_client_id`` rather
    than inventing a second one (the 2026-08-30 collision).
    """
    source = (REPO_ROOT / "scripts/run_paper.py").read_text()

    snapshot_at = source.index("broker_snapshot = asyncio.run(")
    # Distinguish the call site in main() from the `def` above it: the call
    # is the one immediately followed by its keyword arguments.
    sweep_at = source.index("sweep_executions_best_effort(\n        host=args.ib_host")
    preparation_at = source.index("preparation = prepare_daily_run(")

    assert snapshot_at < sweep_at < preparation_at

    sweep_call = source[sweep_at : source.index(")", source.index(")", sweep_at) + 1)]
    assert "client_id=args.ib_client_id," in sweep_call
    assert "args.ib_client_id + 1" not in sweep_call


# ---------------------------------------------------------------------------
# The best-effort wrapper — fetch, decide, publish
# ---------------------------------------------------------------------------


def test_a_recovered_fill_is_published_to_stream_fills(monkeypatch, session) -> None:
    _submitted_intent(session)

    async def _fake_read(**kwargs):
        return [_execution()]

    fake = FakeRedis()
    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)
    monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kwargs: fake)

    outcome = run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert len(outcome.recovered) == 1
    [(stream, payload)] = fake.published
    assert stream == "stream:fills"
    assert payload["execution_id"] == "exec-1"
    assert payload["recovery_source"] == "ib_execution_sweep"
    assert fake.closed is True


def test_the_sweep_opens_exactly_one_redis_connection_for_many_fills(
    monkeypatch, session
) -> None:
    _submitted_intent(session, recommendation_id="rec-unh", ib_order_id="148")
    _submitted_intent(session, recommendation_id="rec-msft", ib_order_id="149")

    async def _fake_read(**kwargs):
        return [
            _execution(execution_id="exec-1", ib_order_id="148"),
            _execution(
                execution_id="exec-2",
                ib_order_id="149",
                con_id=272093,
                ticker="MSFT",
            ),
        ]

    fake = FakeRedis()
    calls: list[str] = []

    def connect(url, **kwargs):
        calls.append(url)
        return fake

    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)
    monkeypatch.setattr(run_paper, "_redis_from_url", connect)

    outcome = run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert len(outcome.recovered) == 2
    assert len(calls) == 1  # opened once, not once per fill
    assert len(fake.published) == 2


def test_no_redis_connection_when_nothing_was_recovered(monkeypatch, session) -> None:
    """An untracked-only sweep has nothing to publish; don't open Redis for it."""

    async def _fake_read(**kwargs):
        return [_execution(ib_order_id="unknown-order")]

    calls: list[str] = []

    def connect(url, **kwargs):
        calls.append(url)
        return FakeRedis()

    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)
    monkeypatch.setattr(run_paper, "_redis_from_url", connect)

    outcome = run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert outcome.recovered == ()
    assert calls == []


def test_the_sweep_redis_connection_is_bounded(monkeypatch, session) -> None:
    """Same failure class as KAN-16: an unbounded wait here would hold the
    04:15 job open past its window."""
    _submitted_intent(session)

    async def _fake_read(**kwargs):
        return [_execution()]

    seen: list[dict] = []

    def connect(url, **kwargs):
        seen.append(kwargs)
        return FakeRedis()

    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)
    monkeypatch.setattr(run_paper, "_redis_from_url", connect)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert seen[0]["socket_connect_timeout"] > 0
    assert seen[0]["socket_timeout"] > 0


def test_a_publish_failure_for_one_fill_does_not_stop_the_others(
    monkeypatch, session, capsys
) -> None:
    _submitted_intent(session, recommendation_id="rec-unh", ib_order_id="148")
    _submitted_intent(session, recommendation_id="rec-msft", ib_order_id="149")

    async def _fake_read(**kwargs):
        return [
            _execution(execution_id="exec-fails", ib_order_id="148"),
            _execution(
                execution_id="exec-succeeds",
                ib_order_id="149",
                con_id=272093,
                ticker="MSFT",
            ),
        ]

    class FlakyRedis(FakeRedis):
        def xadd(self, stream, payload):
            if payload.get("execution_id") == "exec-fails":
                raise ConnectionError("write timed out")
            return super().xadd(stream, payload)

    fake = FlakyRedis()
    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)
    monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kwargs: fake)

    outcome = run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    # Both were decided recoverable — the decision layer doesn't know about
    # the publish failure.
    assert len(outcome.recovered) == 2
    # But only the one that didn't raise actually made it to the stream.
    assert [p["execution_id"] for _, p in fake.published] == ["exec-succeeds"]
    out = capsys.readouterr().out
    assert "exec-fails" in out
    assert "WARNING" in out


def test_a_broken_redis_connection_does_not_raise(monkeypatch, session, capsys) -> None:
    _submitted_intent(session)

    async def _fake_read(**kwargs):
        return [_execution()]

    def _boom_connect(url, **kwargs):
        raise ConnectionError("Error connecting to redis")

    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)
    monkeypatch.setattr(run_paper, "_redis_from_url", _boom_connect)

    outcome = run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert len(outcome.recovered) == 1  # decided, just not delivered
    out = capsys.readouterr().out
    assert "WARNING" in out
