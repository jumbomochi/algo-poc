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

import asyncio
import sys
import types
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
    updated_at: datetime | None = None,
    status: str = OrderStatus.SUBMITTED.value,
    reason: str | None = None,
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
        status=status,
        reason=reason,
        ib_order_id=ib_order_id,
        created_at=now,
        updated_at=updated_at or now,
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


def _record_existing_fill(
    session: Session, *, execution_id: str, ib_order_id: str = "148"
) -> None:
    """An execution_fills row the projector already applied."""
    from shared.models import ExecutionFill

    session.add(
        ExecutionFill(
            account_id="DUN551088",
            execution_id=execution_id,
            ib_order_id=ib_order_id,
            recommendation_id="rec-unh",
            portfolio="momentum",
            con_id=756733,
            symbol="UNH",
            exchange="SMART",
            currency="USD",
            side="SELL",
            quantity=6.0,
            price=341.22,
            commission=1.0,
            executed_at=datetime(2026, 9, 14, 13, 31, tzinfo=UTC),
            projection_applied=True,
        )
    )
    session.commit()


class _StallingIB:
    """Completes the handshake, then never answers the request."""

    def __init__(self):
        self._connected = False

    async def connectAsync(self, host, port, clientId=None, **kwargs):
        self._connected = True

    async def reqExecutionsAsync(self, _filter):
        await asyncio.sleep(30)

    def accountValues(self):
        return []

    def isConnected(self):
        return self._connected

    def disconnect(self):
        self._connected = False


def _stub_ib_insync(monkeypatch, ib_class) -> None:
    """Swap the module ``read_broker_executions`` imports from.

    A stub rather than monkeypatching the real ``ib_insync.IB``: importing
    ``ib_insync`` inside a test drags in ``eventkit``, which takes hold of an
    event loop at import time and upsets whatever loop the rest of the suite
    is on.
    """
    module = types.ModuleType("ib_insync")
    module.IB = ib_class
    module.ExecutionFilter = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "ib_insync", module)


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
        return run_paper.BrokerExecutions(1, [_execution()])

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
        return run_paper.BrokerExecutions(1, [_execution()])

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
        return run_paper.BrokerExecutions(1, [_execution()])

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
        return run_paper.BrokerExecutions(
            2,
            [
                _execution(execution_id="exec-1", ib_order_id="148"),
                _execution(
                    execution_id="exec-2",
                    ib_order_id="149",
                    con_id=272093,
                    ticker="MSFT",
                ),
            ],
        )

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
    """A sweep with nothing to say opens no connection at all.

    (An *untracked* sweep does open one now — it has an alert to raise. This
    case is the quiet one: an execution the projector already recorded.)
    """
    _submitted_intent(session)
    _record_existing_fill(session, execution_id="exec-1")

    async def _fake_read(**kwargs):
        return run_paper.BrokerExecutions(1, [_execution()])

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
        return run_paper.BrokerExecutions(1, [_execution()])

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

    assert (
        seen[0]["socket_connect_timeout"]
        == run_paper.PUBLISH_CONNECT_TIMEOUT_SECONDS
    )
    assert seen[0]["socket_timeout"] == run_paper.PUBLISH_SOCKET_TIMEOUT_SECONDS


def test_a_publish_failure_for_one_fill_does_not_stop_the_others(
    monkeypatch, session, capsys
) -> None:
    _submitted_intent(session, recommendation_id="rec-unh", ib_order_id="148")
    _submitted_intent(session, recommendation_id="rec-msft", ib_order_id="149")

    async def _fake_read(**kwargs):
        return run_paper.BrokerExecutions(
            2,
            [
                _execution(execution_id="exec-fails", ib_order_id="148"),
                _execution(
                    execution_id="exec-succeeds",
                    ib_order_id="149",
                    con_id=272093,
                    ticker="MSFT",
                ),
            ],
        )

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
        return run_paper.BrokerExecutions(1, [_execution()])

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


# ---------------------------------------------------------------------------
# C3 — "IB returned nothing" and "nothing needed recovery" are opposites
#
# ``reqExecutions`` is clientId-scoped. If this connection's client id is not
# the Gateway's Master API client ID, the sweep sees `[]` every night while
# the execution service places orders under a different one. That is the
# silent failure this branch exists to prevent, so the raw count IB returned
# is reported separately from the four outcome counts. Whether the client id
# is in fact the master one is a live-Gateway question, escalated; all the
# code can do is make the two cases distinguishable.
# ---------------------------------------------------------------------------


def test_the_raw_ib_count_is_reported_when_nothing_needed_recovery(
    monkeypatch, session, capsys
) -> None:
    _submitted_intent(session)
    _record_existing_fill(session, execution_id="exec-1")

    async def _fake_read(**kwargs):
        return run_paper.BrokerExecutions(1, [_execution()])

    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    out = capsys.readouterr().out
    assert "IB returned 1" in out
    assert "0 recovered" in out
    assert "1 already recorded" in out


def test_an_empty_ib_response_is_reported_as_zero_returned(
    monkeypatch, session, capsys
) -> None:
    """The clientId-scoping failure mode: indistinguishable from a healthy
    night until the raw count is printed."""

    async def _fake_read(**kwargs):
        return run_paper.BrokerExecutions(0, [])

    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert "IB returned 0" in capsys.readouterr().out


def test_unreadable_rows_are_visible_as_a_gap_in_the_counts(
    monkeypatch, session, capsys
) -> None:
    """IB returned rows the adapter could not classify — also not a healthy
    night, and also invisible without the raw count."""

    async def _fake_read(**kwargs):
        return run_paper.BrokerExecutions(3, [])

    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    out = capsys.readouterr().out
    assert "IB returned 3" in out
    assert "0 readable" in out


def test_deferred_executions_are_reported(monkeypatch, session, capsys) -> None:
    """A deferral is a recoverable non-event, but it must still be counted:
    a deferral that recurs every night is a stuck fill."""
    _submitted_intent(session)

    async def _fake_read(**kwargs):
        return run_paper.BrokerExecutions(
            1, [_execution(commission_currency="SGD")]
        )

    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)

    outcome = run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert outcome.deferred == ("exec-1",)
    assert "1 deferred" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# I6 — the execution request itself must be bounded
# ---------------------------------------------------------------------------


def test_a_stalled_execution_request_times_out(monkeypatch) -> None:
    """``connectAsync`` was bounded; ``reqExecutionsAsync`` was not. A
    Gateway that completes the handshake and then stalls would hold the run
    open until ALGO_PAPER_TIMEOUT_SECONDS (3h) killed it — the AC5 violation
    the bound exists to prevent. Precedent: ``ib_account.py``'s
    ``asyncio.wait_for(future, ACCOUNT_SUMMARY_TIMEOUT_SECONDS)``.
    """
    _stub_ib_insync(monkeypatch, _StallingIB)
    monkeypatch.setattr(run_paper, "REQ_EXECUTIONS_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(
            run_paper.read_broker_executions(
                host="127.0.0.1", port=7497, client_id=58
            )
        )


def test_a_stalled_execution_request_does_not_gate_the_run(
    monkeypatch, session, capsys
) -> None:
    """And the timeout is caught by the AC5 boundary like any other failure."""
    _stub_ib_insync(monkeypatch, _StallingIB)
    monkeypatch.setattr(run_paper, "REQ_EXECUTIONS_TIMEOUT_SECONDS", 0.05)

    assert (
        run_paper.sweep_executions_best_effort(
            host="127.0.0.1",
            port=7497,
            client_id=58,
            session=session,
            redis_url="redis://localhost:6379/0",
        )
        is None
    )
    assert "could not read executions from IB" in capsys.readouterr().out


def test_the_named_timeout_constant_is_a_sane_bound() -> None:
    assert 0 < run_paper.REQ_EXECUTIONS_TIMEOUT_SECONDS <= 120


# ---------------------------------------------------------------------------
# I7 — a sweep failure must be able to reach the operator
#
# run_paper.sh only sends Telegram on a non-zero exit, and the sweep never
# changes the exit code (correctly, per AC5). Without an alert, every one of
# these stops at a log file nobody reads.
# ---------------------------------------------------------------------------


def test_a_decision_failure_alerts_the_operator(monkeypatch, session) -> None:
    async def _fake_read(**kwargs):
        return run_paper.BrokerExecutions(1, [_execution()])

    def _boom(*args, **kwargs):
        raise RuntimeError("ledger blew up")

    fake = FakeRedis()
    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)
    monkeypatch.setattr(run_paper, "plan_sweep", _boom)
    monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kw: fake)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    [(stream, payload)] = fake.published
    assert stream == "stream:alerts"
    assert payload["event_type"] == "execution_sweep_failed"
    assert payload["priority"] == "high"
    assert "ledger blew up" in payload["message"]


def test_the_decision_failure_alert_redacts_a_dsn(monkeypatch, session) -> None:
    async def _fake_read(**kwargs):
        return run_paper.BrokerExecutions(1, [_execution()])

    def _boom(*args, **kwargs):
        raise RuntimeError("postgresql://algo:p@ssw0rd@db:5432/algo_poc is down")

    fake = FakeRedis()
    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)
    monkeypatch.setattr(run_paper, "plan_sweep", _boom)
    monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kw: fake)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    [(_, payload)] = fake.published
    assert "p@ssw0rd" not in payload["message"]
    assert "***@" in payload["message"]


def test_untracked_executions_alert_the_operator(monkeypatch, session) -> None:
    """IB executed something the book has no intent for. That is the book
    and the broker disagreeing about what was traded — printing it and
    exiting 0 is how it stays unnoticed."""

    async def _fake_read(**kwargs):
        return run_paper.BrokerExecutions(
            1, [_execution(ib_order_id="9999")]
        )

    fake = FakeRedis()
    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)
    monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kw: fake)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    alerts = [p for stream, p in fake.published if stream == "stream:alerts"]
    assert len(alerts) == 1
    assert alerts[0]["event_type"] == "execution_sweep_untracked"
    assert "9999" in alerts[0]["message"]


def test_no_untracked_alert_on_a_clean_sweep(monkeypatch, session) -> None:
    """An alert that fires every night is an alert nobody reads."""
    _submitted_intent(session)

    async def _fake_read(**kwargs):
        return run_paper.BrokerExecutions(1, [_execution()])

    fake = FakeRedis()
    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)
    monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kw: fake)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert [stream for stream, _ in fake.published] == ["stream:fills"]


def test_untracked_alert_does_not_misname_a_terminal_intent(
    monkeypatch, session
) -> None:
    """``untracked`` has two producers: no intent at all for the broker
    order id, and an intent that exists but is already terminal (CANCELLED,
    FILLED, or genuinely EXPIRED) for a reason the sweep must not overturn.
    The alert text must not claim "no order intent in the book" for the
    second case — an operator reading that would go looking for an order
    the system never placed, when the truth is the opposite: IB executed
    against an order the book had already closed."""
    _submitted_intent(session, ib_order_id="148")
    intent = session.query(OrderIntent).filter_by(ib_order_id="148").one()
    intent.status = OrderStatus.CANCELLED.value
    session.commit()

    async def _fake_read(**kwargs):
        return run_paper.BrokerExecutions(1, [_execution(ib_order_id="148")])

    fake = FakeRedis()
    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)
    monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kw: fake)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    alerts = [p for stream, p in fake.published if stream == "stream:alerts"]
    assert len(alerts) == 1
    message = alerts[0]["message"]
    assert "148" in message
    assert "no order intent in the book" not in message


def test_a_failed_alert_does_not_gate_the_run(monkeypatch, session, capsys) -> None:
    """AC5 again: the alert is best-effort. Its usual cause of failure is
    Redis being unreachable, which takes the alert path down with it."""

    async def _fake_read(**kwargs):
        return run_paper.BrokerExecutions(
            1, [_execution(ib_order_id="9999")]
        )

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

    assert outcome.untracked == ("9999",)
    assert "WARNING" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# C1 (edge half) — the FX rate only IB can answer for
# ---------------------------------------------------------------------------


def test_the_exchange_rate_is_read_the_way_the_live_callback_reads_it() -> None:
    """Mirrors ``ib_executor._on_commission_report``: the single USD
    ``ExchangeRate`` account value, finite and positive."""
    ib = SimpleNamespace(
        accountValues=lambda: [
            SimpleNamespace(tag="NetLiquidation", currency="SGD", value="5000"),
            SimpleNamespace(tag="ExchangeRate", currency="USD", value="1.2865"),
        ]
    )

    assert run_paper._exchange_rate_usd(ib) == pytest.approx(1.2865)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [SimpleNamespace(tag="ExchangeRate", currency="USD", value="0")],
        [SimpleNamespace(tag="ExchangeRate", currency="USD", value="nope")],
        [
            SimpleNamespace(tag="ExchangeRate", currency="USD", value="1.2"),
            SimpleNamespace(tag="ExchangeRate", currency="USD", value="1.3"),
        ],
    ],
)
def test_an_unusable_exchange_rate_reads_as_absent(rows) -> None:
    """Absent, not guessed: plan_sweep defers rather than booking a wrong
    commission."""
    assert run_paper._exchange_rate_usd(SimpleNamespace(accountValues=lambda: rows)) is None


def test_an_exchange_rate_read_that_raises_does_not_fail_the_sweep() -> None:
    def _boom():
        raise RuntimeError("no account subscription")

    assert run_paper._exchange_rate_usd(SimpleNamespace(accountValues=_boom)) is None


# ---------------------------------------------------------------------------
# I8 — the comment at the call site must state the guarantee that holds
# ---------------------------------------------------------------------------


def test_the_call_site_does_not_claim_this_run_is_corrected() -> None:
    """``FillProjector`` runs in the portfolio_accounting container and
    ``xadd`` returns immediately, so ``prepare_daily_run`` — which runs
    microseconds later, against a ``broker_snapshot`` read *before* the
    sweep — cannot see the recovered fill. The benefit lands one run later,
    and the comment must not promise otherwise."""
    source = (REPO_ROOT / "scripts/run_paper.py").read_text()
    sweep_at = source.index("sweep_executions_best_effort(\n        host=args.ib_host")
    comment = source[sweep_at - 1200 : sweep_at]

    assert "BEFORE reconciliation reads the book" not in comment
    assert "next run" in comment


# ---------------------------------------------------------------------------
# The blindness self-check — `reqExecutions` is clientId-scoped
# ---------------------------------------------------------------------------
#
# A client sees only its OWN executions unless it holds the Gateway's Master
# API client ID. The sweep connects as 58; the orders are placed by the
# execution service under `ib.client_id`. With the Master API client ID unset
# the sweep returns [] every night — byte-identical to a healthy night on
# which nothing needed recovering. That setting is host state no commit
# carries and a Gateway reinstall silently drops, so the code has to notice.


def _blind_alerts(fake: FakeRedis) -> list[dict]:
    return [
        payload
        for stream, payload in fake.published
        if stream == "stream:alerts"
        and payload["event_type"] == "execution_sweep_blind"
    ]


async def _returned_nothing(**kwargs):
    return run_paper.BrokerExecutions(0, [])


def test_zero_executions_with_a_working_order_alerts_the_operator(
    monkeypatch, session
) -> None:
    """The book thinks IB is holding an order it placed; IB says it executed
    nothing at all, for anybody. That is the signature of a sweep that cannot
    see past its own client id."""
    _submitted_intent(session, updated_at=datetime.now(UTC))

    fake = FakeRedis()
    monkeypatch.setattr(run_paper, "read_broker_executions", _returned_nothing)
    monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kw: fake)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    alerts = _blind_alerts(fake)
    assert len(alerts) == 1
    assert alerts[0]["priority"] == "high"


def test_the_blind_alert_names_the_master_api_client_id(
    monkeypatch, session
) -> None:
    """"0 executions" restates the symptom. The operator needs the cause and
    the setting to go and look at, or the alert is the silence again."""
    _submitted_intent(session, updated_at=datetime.now(UTC))

    fake = FakeRedis()
    monkeypatch.setattr(run_paper, "read_broker_executions", _returned_nothing)
    monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kw: fake)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    message = _blind_alerts(fake)[0]["message"]
    assert "Master API client ID" in message
    assert "Configure > API > Settings" in message
    assert "58" in message
    assert "reqExecutions" in message


def test_an_absent_at_ib_terminalization_is_enough_to_alert(
    monkeypatch, session
) -> None:
    """The phantom this whole feature exists to clear. If it is in the book
    and IB reports nothing at all, the sweep is the suspect."""
    from shared.order_ledger import ABSENT_AT_IB_REASON

    _submitted_intent(
        session,
        updated_at=datetime.now(UTC),
        status=OrderStatus.EXPIRED.value,
        reason=ABSENT_AT_IB_REASON,
    )

    fake = FakeRedis()
    monkeypatch.setattr(run_paper, "read_broker_executions", _returned_nothing)
    monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kw: fake)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert len(_blind_alerts(fake)) == 1


def test_a_quiet_night_does_not_alert(monkeypatch, session) -> None:
    """No resting orders, nothing the book expected IB to report. This is the
    normal case and an alert here would train the operator to ignore it."""
    fake = FakeRedis()
    monkeypatch.setattr(run_paper, "read_broker_executions", _returned_nothing)
    monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kw: fake)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert fake.published == []


def test_stale_ledger_evidence_does_not_alert(monkeypatch, session) -> None:
    """``reqExecutions`` serves the current trading day. A month-old stuck
    intent cannot be what today's request should have returned, and counting
    it would make this fire every night for the rest of the book's life."""
    from datetime import timedelta

    _submitted_intent(
        session, updated_at=datetime.now(UTC) - timedelta(days=30)
    )

    fake = FakeRedis()
    monkeypatch.setattr(run_paper, "read_broker_executions", _returned_nothing)
    monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kw: fake)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert fake.published == []


def test_a_recovering_sweep_never_claims_blindness(monkeypatch, session) -> None:
    """IB answered. Whatever the ledger holds, the client id is not the
    problem."""
    _submitted_intent(session, updated_at=datetime.now(UTC))

    async def _fake_read(**kwargs):
        return run_paper.BrokerExecutions(1, [_execution()])

    fake = FakeRedis()
    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)
    monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kw: fake)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert _blind_alerts(fake) == []


def test_unreadable_rows_are_not_blindness(monkeypatch, session) -> None:
    """IB returned rows the adapter could not classify. That is an adapter
    gap, already visible in the `(M readable)` count — not a client-id
    problem, and naming the wrong cause sends the operator to the wrong
    screen."""
    _submitted_intent(session, updated_at=datetime.now(UTC))

    async def _fake_read(**kwargs):
        return run_paper.BrokerExecutions(3, [])

    fake = FakeRedis()
    monkeypatch.setattr(run_paper, "read_broker_executions", _fake_read)
    monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kw: fake)

    run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert _blind_alerts(fake) == []


def test_a_failed_blind_alert_does_not_gate_the_run(
    monkeypatch, session, capsys
) -> None:
    """AC5. Redis being unreachable is both the usual cause of a failed alert
    and something this run must survive."""
    _submitted_intent(session, updated_at=datetime.now(UTC))

    def _boom_connect(url, **kwargs):
        raise ConnectionError("Error connecting to redis")

    monkeypatch.setattr(run_paper, "read_broker_executions", _returned_nothing)
    monkeypatch.setattr(run_paper, "_redis_from_url", _boom_connect)

    outcome = run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert outcome is not None
    assert outcome.recovered == ()
    assert "WARNING" in capsys.readouterr().out


def test_a_failed_evidence_query_does_not_gate_the_run(
    monkeypatch, session, capsys
) -> None:
    """The self-check reads the book a second time, after the sweep's own
    commit. A failure there is a diagnostic failing, not the run failing."""
    _submitted_intent(session, updated_at=datetime.now(UTC))

    def _boom(self, **kwargs):
        raise RuntimeError("no such column: order_intents.updated_at")

    fake = FakeRedis()
    monkeypatch.setattr(run_paper, "read_broker_executions", _returned_nothing)
    monkeypatch.setattr(run_paper, "_redis_from_url", lambda url, **kw: fake)
    monkeypatch.setattr(
        run_paper.OrderLedger, "broker_activity_evidence_count", _boom
    )

    outcome = run_paper.sweep_executions_best_effort(
        host="127.0.0.1",
        port=7497,
        client_id=58,
        session=session,
        redis_url="redis://localhost:6379/0",
    )

    assert outcome is not None
    assert _blind_alerts(fake) == []
    assert "WARNING" in capsys.readouterr().out


def test_the_blind_check_uses_a_window_reqexecutions_can_still_cover() -> None:
    """A window shorter than a long weekend would miss the Friday order that
    fills on Monday; one much longer re-admits the stale evidence the guard
    is built to exclude."""
    assert 3 <= run_paper.SWEEP_BLINDNESS_EVIDENCE_WINDOW_DAYS <= 7
