"""The single publish site for risk-side exits (KAN-5).

Every risk-side sell must go out through the emitter's own publish site
(:meth:`_publish_approved_exit` since KAN-8) and through nothing else. An order
published without a backing ledger intent is rejected by
execution and dead-letters silently — the failure mode that left stop-loss sells
unexecuted for weeks. These tests pin the emitter's contract so the callers
added in KAN-7 inherit a method already proven identical to what shipped.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from services.execution.runner import ExecutionServiceRunner
from services.risk_management import runner as runner_module
from services.risk_management.runner import RiskServiceRunner
from shared.config import (
    AppConfig,
    CurrencyConfig,
    ExecutionConfig,
    IBConfig,
    RiskConfig,
)
from shared.models import Base, OrderIntent, OrderStatus, Position
from shared.order_ledger import OrderLedger
from shared.schemas.messages import ApprovedOrderMessage

# The entry path publishes approved *buys* from an intent risk itself approved
# a few lines earlier (_persist_risk_approval). It is not an exit and is out of
# this emitter's scope; naming it here keeps the assertion below honest about
# what it does and does not cover.
ENTRY_PATH_PUBLISHER = "process_recommendation"

# Captured from the pre-change code (KAN-5 parent commit) by running a kill
# against the fixture book below and dumping the published stream dict. The
# extraction is only structural if it still produces exactly this.
PRE_CHANGE_KILL_PAYLOAD = {
    "action": "sell",
    "order_type": "market",
    "quantity": "10.0",
    "recommendation_id": "liq-paper-AAPL-1786104000",
    "risk_adjustments": '{"kill_switch": true, "reason": "emergency"}',
    "schema_version": "1",
    "ticker": "AAPL",
}


def _approved_order_publish_sites() -> dict[str, int]:
    """Map method name -> how many times it publishes to APPROVED_ORDERS_STREAM.

    Parsed from the AST rather than grepped, so reformatting a call across
    lines cannot silently hide a publish site from this check.
    """
    tree = ast.parse(inspect.getsource(runner_module))
    cls = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "RiskServiceRunner"
    )
    sites: dict[str, int] = {}
    for method in cls.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(method):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "publish"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "APPROVED_ORDERS_STREAM"
            ):
                sites[method.name] = sites.get(method.name, 0) + 1
    return sites


def _target(
    ticker: str = "AAPL",
    *,
    quantity: float | None = 10.0,
    con_id: int | None = 111,
) -> dict:
    """One `load_liquidation_targets` row."""
    return {
        "ticker": ticker,
        "quantity": quantity,
        "con_id": con_id,
        "account_id": "DUTEST",
        "exchange": "SMART",
        "currency": "USD",
        "portfolio": "momentum",
    }


def _published_orders(mock_redis) -> list[dict]:
    return [
        call.args[1]
        for call in mock_redis.publish.call_args_list
        if call.args[0] == "stream:approved_orders"
    ]


def _alerts(mock_redis) -> list[str]:
    return [
        str(call.args[1])
        for call in mock_redis.publish.call_args_list
        if call.args[0] == "stream:alerts"
    ]


@pytest.fixture()
def mock_config():
    config = MagicMock(spec=AppConfig)
    config.mode = "paper"
    config.risk = RiskConfig()
    config.currency = CurrencyConfig()
    return config


@pytest.fixture()
def mock_redis():
    redis = AsyncMock()
    redis.publish = AsyncMock(return_value="msg-id-123")
    redis.stream_length = AsyncMock(return_value=0)
    return redis


@pytest.fixture()
def db_runner(mock_config, mock_redis):
    """A runner holding 10 AAPL, with a real ledger on in-memory sqlite."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(
        Position(
            account_id="DUTEST",
            ticker="AAPL",
            portfolio="momentum",
            con_id=111,
            exchange="SMART",
            currency="USD",
            quantity=10.0,
            avg_entry_price=100,
            current_price=100,
            peak_price=100,
            highest_price_since_entry=100,
            opened_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            status="open",
        )
    )
    session.commit()
    ledger = OrderLedger(session)
    runner = RiskServiceRunner(
        config=mock_config,
        redis_client=mock_redis,
        db_session=session,
        order_ledger=ledger,
    )
    runner._portfolio.positions = {}
    return runner, ledger, session


@pytest.fixture()
def execution_runner(db_runner, mock_redis):
    """A real execution service sharing the risk runner's ledger.

    The redelivery guard being asserted lives in
    :meth:`ExecutionServiceRunner.process_approved_order` and reads the ledger,
    so a mock would prove nothing — the two services have to share a session.
    """
    _runner, ledger, _session = db_runner
    config = MagicMock(spec=AppConfig)
    config.mode = "paper"
    config.execution = ExecutionConfig()
    config.ib = IBConfig()
    config.risk = RiskConfig()
    order_manager = AsyncMock()
    order_manager.submit_entry = AsyncMock(return_value="order-001")
    order_manager.submit_exit = AsyncMock(return_value="order-002")
    order_manager.open_orders = {}
    return (
        ExecutionServiceRunner(
            config=config,
            redis_client=mock_redis,
            order_manager=order_manager,
            order_ledger=ledger,
        ),
        order_manager,
    )


def test_emit_ledgered_exit_is_the_only_exit_publish_site():
    """Exactly one exit publish site, and it is the emitter.

    Written as a strict xfail in KAN-5 and flipped here (AC 8): stop-loss and
    passive-trim now route through the emitter, so the assertion holds for the
    first time. It stays as a live test because the regression it catches —
    someone adding a second, unbacked publish — is the original P0.

    KAN-8 pushed the raw ``redis.publish`` one level down into
    ``_publish_approved_exit`` so the emitter and the re-publish sweep share the
    publish-then-mark ordering. The invariant is unchanged and now stricter:
    one place in the class puts a sell on stream:approved_orders.
    """
    exit_sites = {
        method: count
        for method, count in _approved_order_publish_sites().items()
        if method != ENTRY_PATH_PUBLISHER
    }
    assert exit_sites == {"_publish_approved_exit": 1}


class TestKillPathIsUnchanged:
    """The extraction must be provably structural."""

    @pytest.mark.asyncio
    async def test_published_payload_is_field_for_field_identical(
        self, db_runner, mock_redis
    ):
        runner, _ledger, session = db_runner

        published = await runner._emit_ledgered_exit(
            "liq",
            _target(),
            exit_id="liq-paper-AAPL-1786104000",
            reason="emergency",
        )

        assert published is True
        orders = _published_orders(mock_redis)
        assert len(orders) == 1
        # timestamp is wall-clock and excluded; every other field must match
        # the payload captured before the extraction.
        assert {
            key: value for key, value in orders[0].items() if key != "timestamp"
        } == PRE_CHANGE_KILL_PAYLOAD
        session.rollback()

    @pytest.mark.asyncio
    async def test_exit_intent_is_created_approved(self, db_runner, mock_redis):
        """KAN-4 semantics survive the move: risk approves its own exits, so
        execution's record_submission is a legal APPROVED->SUBMITTED."""
        runner, ledger, session = db_runner

        await runner._emit_ledgered_exit(
            "liq", _target(), exit_id="liq-paper-AAPL-1", reason="emergency"
        )

        intent = ledger.get("liq-paper-AAPL-1")
        assert intent.status == OrderStatus.APPROVED.value
        assert intent.approved_at is not None
        assert intent.action == "SELL"
        assert intent.con_id == 111
        session.rollback()


class TestGuardsMovedIntact:
    @pytest.mark.asyncio
    async def test_missing_con_id_is_flagged_with_kind_and_not_published(
        self, db_runner, mock_redis
    ):
        """No backing intent is possible without a con_id, so execution would
        reject the exit. Alert for manual action instead of publishing a doomed
        order — and say which mechanism could not route (KAN-5 adds `kind`)."""
        runner, _ledger, session = db_runner

        published = await runner._emit_ledgered_exit(
            "stop-loss",
            _target(con_id=None),
            exit_id="stop-loss-AAPL-1",
            reason="trailing stop hit",
        )

        assert published is False
        assert _published_orders(mock_redis) == []
        alerts = _alerts(mock_redis)
        assert any("AAPL" in a and "manual" in a.lower() for a in alerts)
        assert any("stop-loss" in a for a in alerts)  # kind is in the context
        session.rollback()

    @pytest.mark.asyncio
    async def test_conflicting_intent_is_flagged_not_published(
        self, db_runner, mock_redis
    ):
        """A row for this exit id that disagrees on economics is left exactly as
        found and nothing is published: execution submits the published quantity
        verbatim, so publishing anyway would leave a broker order the ledger
        contradicts (open_order_mismatch, which blocks buys).

        NOTE: KAN-5's AC #5 says this branch "adopts the existing intent". That
        wording predates KAN-4, which deliberately replaced adopt-and-publish
        with flag-and-refuse (see test_conflicting_exit_intent_is_flagged_not_
        published in test_runner.py). This test pins the behavior that actually
        shipped, which is what "zero behavior change" means here.
        """
        runner, ledger, session = db_runner
        exit_id = "liq-paper-AAPL-1786104000"
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id=exit_id,
                account_id="DUTEST",
                mode="paper",
                portfolio="momentum",
                con_id=111,
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
                action="SELL",
                quantity=4.0,  # the position holds 10 — economics conflict
                limit_price=None,
                order_type="MKT",
            )
        )
        session.commit()

        published = await runner._emit_ledgered_exit(
            "liq", _target(), exit_id=exit_id, reason="emergency"
        )

        assert published is False
        assert _published_orders(mock_redis) == []
        assert any("manual" in a.lower() for a in _alerts(mock_redis))
        # The conflicting row is untouched, and the session survives so the
        # rest of the book still flattens.
        intent = ledger.get(exit_id)
        assert intent.status == OrderStatus.PROPOSED.value
        assert intent.requested_quantity == pytest.approx(4.0)
        session.rollback()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("quantity", [None, 0.0, -5.0])
    async def test_non_positive_quantity_publishes_nothing(
        self, db_runner, mock_redis, quantity
    ):
        runner, _ledger, session = db_runner

        published = await runner._emit_ledgered_exit(
            "liq",
            _target(quantity=quantity),
            exit_id="liq-paper-AAPL-1",
            reason="emergency",
        )

        assert published is False
        assert _published_orders(mock_redis) == []
        session.rollback()


class TestEmitterGeneralisesForKan7:
    """The two parameters that exist so stop-loss and trim can be callers."""

    @pytest.mark.asyncio
    async def test_caller_supplied_quantity_sells_a_portion(
        self, db_runner, mock_redis
    ):
        """A passive trim sells part of the position, not all of it — the
        published order and the backing intent must agree on that partial
        quantity or execution's submission diverges from the ledger."""
        runner, ledger, session = db_runner

        await runner._emit_ledgered_exit(
            "passive-trim",
            _target(),
            exit_id="passive-trim-AAPL-1",
            reason="hard ceiling breach",
            quantity=3.0,
        )

        order = _published_orders(mock_redis)[0]
        assert float(order["quantity"]) == pytest.approx(3.0)
        assert ledger.get("passive-trim-AAPL-1").requested_quantity == pytest.approx(
            3.0
        )
        session.rollback()

    @pytest.mark.asyncio
    async def test_caller_supplied_risk_adjustments_replace_the_kill_default(
        self, db_runner, mock_redis
    ):
        """Only the kill path is a kill: a stop-loss exit must not arrive at
        execution labelled `kill_switch`."""
        runner, _ledger, session = db_runner

        await runner._emit_ledgered_exit(
            "stop-loss",
            _target(),
            exit_id="stop-loss-AAPL-1",
            reason="trailing stop hit",
            risk_adjustments={"stop_loss": True, "reason": "trailing stop hit"},
        )

        adjustments = _published_orders(mock_redis)[0]["risk_adjustments"]
        assert "stop_loss" in adjustments
        assert "kill_switch" not in adjustments
        session.rollback()


def _position(
    ticker: str = "AAPL",
    *,
    portfolio: str = "momentum",
    con_id: int | None = 111,
    quantity: float = 10.0,
) -> Position:
    return Position(
        account_id="DUTEST",
        ticker=ticker,
        portfolio=portfolio,
        con_id=con_id,
        exchange="SMART",
        currency="USD",
        quantity=quantity,
        avg_entry_price=100,
        current_price=100,
        peak_price=100,
        highest_price_since_entry=100,
        opened_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        status="open",
    )


def _breaching(runner, *, ticker: str = "AAPL", quantity: float = 10.0):
    """Put the in-memory book in a state the trailing stop must fire on."""
    runner._portfolio.nav = 100_000
    runner._portfolio.positions = {
        ticker: {
            "quantity": quantity,
            "sector": "Technology",
            "highest_price_since_entry": 100.0,
        }
    }
    runner._current_prices = {ticker: 84.0}  # 16% off the high, stop is 15%


def _intents(session) -> list[OrderIntent]:
    session.rollback()
    return list(session.scalars(select(OrderIntent).order_by(OrderIntent.id)))


class TestStopLossReachesTheBroker:
    """KAN-7: the P0 itself — stop-loss sells that execution can actually route."""

    @pytest.mark.asyncio
    async def test_breach_creates_an_approved_intent_and_publishes_it(
        self, db_runner, mock_redis
    ):
        """AC 1 (risk half): the published id is the intent's id, so execution's
        opening ledger lookup finds it instead of dead-lettering."""
        runner, ledger, session = db_runner
        _breaching(runner)

        await runner.run_stop_loss_check()

        (order,) = _published_orders(mock_redis)
        (intent,) = _intents(session)
        assert order["recommendation_id"] == intent.recommendation_id
        assert intent.status == OrderStatus.APPROVED.value
        assert intent.con_id == 111
        assert intent.portfolio == "momentum"
        assert float(order["quantity"]) == pytest.approx(10.0)
        assert "stop_loss" in order["risk_adjustments"]

    @pytest.mark.asyncio
    async def test_identity_comes_from_the_db_not_the_in_memory_book(
        self, db_runner, mock_redis
    ):
        """The in-memory position carries no con_id/account — resolving through
        load_liquidation_targets is what makes the intent creatable at all."""
        runner, _ledger, session = db_runner
        _breaching(runner, quantity=999.0)  # in-memory quantity is not authority

        await runner.run_stop_loss_check()

        (order,) = _published_orders(mock_redis)
        assert float(order["quantity"]) == pytest.approx(10.0)
        session.rollback()

    @pytest.mark.asyncio
    async def test_a_persistent_breach_yields_one_intent_at_a_time(
        self, db_runner, mock_redis
    ):
        """AC 2 / design test #4: the scan runs every few minutes and the breach
        does not clear on its own. Three scans, one working sell."""
        runner, _ledger, session = db_runner
        _breaching(runner)

        for _ in range(3):
            await runner.run_stop_loss_check()

        assert len(_published_orders(mock_redis)) == 1
        assert len(_intents(session)) == 1

    @pytest.mark.asyncio
    async def test_a_plain_non_exit_sell_suppresses_the_stop_loss(
        self, db_runner, mock_redis
    ):
        """AC 4 / design test #8: suppression is about the position, not about
        exits. A sleeve-rebalance sell already working means the shares are
        spoken for; a second sell oversells the moment the first fills."""
        runner, ledger, session = db_runner
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id="sleeve-rebalance-AAPL",
                account_id="DUTEST",
                mode="paper",
                portfolio="momentum",
                con_id=111,
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
                action="SELL",
                quantity=4.0,
                limit_price=None,
                order_type="MKT",
            )
        )
        session.commit()
        _breaching(runner)

        await runner.run_stop_loss_check()

        assert _published_orders(mock_redis) == []
        assert [i.recommendation_id for i in _intents(session)] == [
            "sleeve-rebalance-AAPL"
        ]

    @pytest.mark.asyncio
    async def test_a_terminal_earlier_exit_does_not_suppress_a_new_breach(
        self, db_runner, mock_redis
    ):
        """The flip side of AC 2: once the exit has filled, a fresh breach the
        same day must be able to exit again — under its own id, not the filled
        one's (seq + 1)."""
        runner, ledger, session = db_runner
        _breaching(runner)
        await runner.run_stop_loss_check()
        first = _intents(session)[0].recommendation_id
        ledger.transition(first, OrderStatus.SUBMITTED)
        ledger.transition(first, OrderStatus.FILLED)
        session.commit()

        await runner.run_stop_loss_check()

        ids = [i.recommendation_id for i in _intents(session)]
        assert len(ids) == 2
        assert ids[0].endswith("-0") and ids[1].endswith("-1")
        assert len(_published_orders(mock_redis)) == 2

    @pytest.mark.asyncio
    async def test_missing_con_id_alerts_with_kind_and_publishes_nothing(
        self, db_runner, mock_redis
    ):
        """AC 3 / design test #7: an unroutable stop-loss is escalated the way an
        unroutable kill is, and names the control that could not route."""
        runner, _ledger, session = db_runner
        position = session.scalars(select(Position)).one()
        position.con_id = None
        session.commit()
        _breaching(runner)

        await runner.run_stop_loss_check()

        assert _published_orders(mock_redis) == []
        assert _intents(session) == []
        unroutable = [a for a in _alerts(mock_redis) if "liquidation_unroutable" in a]
        assert len(unroutable) == 1
        assert "stop-loss" in unroutable[0]
        assert not [a for a in _alerts(mock_redis) if "stop_loss_triggered" in a]

    @pytest.mark.asyncio
    async def test_an_identical_re_entry_adopts_the_existing_intent(
        self, db_runner, mock_redis
    ):
        """AC 5: a replay of the same exit — same id, same economics — adopts the
        row that is already there rather than raising ConflictingOrderIntent or
        minting a duplicate."""
        runner, _ledger, session = db_runner

        for _ in range(2):
            published = await runner._emit_ledgered_exit(
                "stop-loss",
                _target(),
                exit_id="stop-loss-DUTEST-momentum-111-2026-08-15-0",
                reason="trailing stop hit",
            )
            assert published is True

        assert len(_intents(session)) == 1


@pytest.fixture()
def two_sleeve_runner(mock_config, mock_redis):
    """AAPL held by two sleeves: 10 shares in momentum, 30 in quality.

    The six-sleeve book does this routinely, and it is the case a ticker-keyed
    exit gets wrong — one sleeve's identity carrying the other sleeve's shares.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(_position(portfolio="momentum", quantity=10.0))
    session.add(_position(portfolio="quality", quantity=30.0))
    session.commit()
    ledger = OrderLedger(session)
    runner = RiskServiceRunner(
        config=mock_config,
        redis_client=mock_redis,
        db_session=session,
        order_ledger=ledger,
    )
    runner._portfolio.positions = {}
    return runner, ledger, session


class TestExitsFanOutPerSleeve:
    """AC 6 / 7: one breach, one exit per identity scope."""

    @pytest.mark.asyncio
    async def test_stop_loss_exits_each_sleeve_under_its_own_id(
        self, two_sleeve_runner, mock_redis
    ):
        runner, _ledger, session = two_sleeve_runner
        _breaching(runner, quantity=40.0)

        await runner.run_stop_loss_check()

        orders = _published_orders(mock_redis)
        assert len(orders) == 2
        assert len({o["recommendation_id"] for o in orders}) == 2
        by_portfolio = {i.portfolio: i for i in _intents(session)}
        assert by_portfolio["momentum"].requested_quantity == pytest.approx(10.0)
        assert by_portfolio["quality"].requested_quantity == pytest.approx(30.0)

    @pytest.mark.asyncio
    async def test_trim_sells_each_sleeve_its_own_share_not_the_ticker_total(
        self, two_sleeve_runner, mock_redis
    ):
        """The ceiling is breached by the ticker (40 shares at $100 = 40% of a
        $10k NAV, ceiling 10%), but 30 of those shares are quality's. Selling
        the whole 30-share overage out of momentum would flatten momentum and
        leave quality untouched."""
        runner, _ledger, session = two_sleeve_runner
        runner._portfolio.nav = 10_000
        runner._current_prices = {"AAPL": 100.0}

        await runner._trim_position_to_target(
            SimpleNamespace(
                ticker="AAPL",
                target_pct=10.0,
                current_pct=40.0,
                message="hard ceiling breach",
            )
        )

        by_portfolio = {i.portfolio: i for i in _intents(session)}
        # overage = 40 - 10 = 30 shares, split 10:30 across the sleeves.
        assert by_portfolio["momentum"].requested_quantity == pytest.approx(7.5)
        assert by_portfolio["quality"].requested_quantity == pytest.approx(22.5)
        assert sum(
            float(o["quantity"]) for o in _published_orders(mock_redis)
        ) == pytest.approx(30.0)

    @pytest.mark.asyncio
    async def test_a_working_sell_in_one_sleeve_does_not_block_the_other(
        self, two_sleeve_runner, mock_redis
    ):
        """Suppression is scoped to {account, portfolio, con_id} — scoping it to
        the ticker would let one sleeve's working order strand the other's."""
        runner, ledger, session = two_sleeve_runner
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id="working-momentum-sell",
                account_id="DUTEST",
                mode="paper",
                portfolio="momentum",
                con_id=111,
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
                action="SELL",
                quantity=10.0,
                limit_price=None,
                order_type="MKT",
            )
        )
        session.commit()
        _breaching(runner, quantity=40.0)

        await runner.run_stop_loss_check()

        (order,) = _published_orders(mock_redis)
        assert float(order["quantity"]) == pytest.approx(30.0)
        assert {i.portfolio for i in _intents(session)} == {"momentum", "quality"}


class _PublishCrash(RuntimeError):
    """Stands in for the process dying between the commit and the publish."""


def _crash_on_exit_publish(mock_redis):
    """Make the next ``stream:approved_orders`` publish blow up.

    The real failure is a SIGKILL between ``session.commit()`` and the
    ``await self._redis.publish(...)`` one line below it. A raising publish
    leaves the process in the same *durable* state — the intent is committed,
    the message never reached the stream — which is the only state the next
    scan can observe.
    """
    original = mock_redis.publish

    async def crashing(stream, payload):
        if stream == "stream:approved_orders":
            raise _PublishCrash("process died before the publish landed")
        return await original(stream, payload)

    mock_redis.publish = AsyncMock(side_effect=crashing)
    return original


async def _orphan_a_stop_loss(runner, mock_redis) -> str:
    """Drive one stop-loss breach through a crashing publish.

    Returns the recommendation id of the intent left committed-but-unpublished.
    """
    _breaching(runner)
    original = _crash_on_exit_publish(mock_redis)
    with pytest.raises(_PublishCrash):
        await runner.run_stop_loss_check()
    mock_redis.publish = original
    session = runner._order_ledger.session
    session.rollback()
    (intent,) = _intents(session)
    return intent.recommendation_id


class TestOrphanProofPublication:
    """KAN-8: a crash between persist and publish must not mute a ticker.

    Before this, the emitter committed an APPROVED exit and *then* published.
    A crash in that window left a nonterminal intent nobody would ever act on,
    and KAN-7's suppression rule read it as "an exit is already in flight" — so
    the ticker's stop-loss stayed muted until a human noticed.
    """

    @pytest.mark.asyncio
    async def test_a_crash_between_persist_and_publish_leaves_an_orphan(
        self, db_runner, mock_redis
    ):
        """The precondition every other test here builds on (AC 4, second half):
        the intent is committed and APPROVED, and published_at stays NULL."""
        runner, ledger, session = db_runner

        exit_id = await _orphan_a_stop_loss(runner, mock_redis)

        intent = ledger.get(exit_id)
        assert intent.status == OrderStatus.APPROVED.value
        assert intent.published_at is None
        assert _published_orders(mock_redis) == []
        session.rollback()

    @pytest.mark.asyncio
    async def test_the_next_scan_republishes_the_orphan_under_the_same_id(
        self, db_runner, mock_redis
    ):
        """AC 1 / design test #3: the periodic scan recovers the orphan itself.

        Same id, so a downstream replay is a no-op — and no second intent, which
        is what a "re-emit" (rather than a re-publish) would have produced."""
        runner, ledger, session = db_runner
        exit_id = await _orphan_a_stop_loss(runner, mock_redis)

        await runner.run_periodic_risk_checks()

        (order,) = _published_orders(mock_redis)
        assert order["recommendation_id"] == exit_id
        assert [i.recommendation_id for i in _intents(session)] == [exit_id]
        assert ledger.get(exit_id).published_at is not None
        session.rollback()

    @pytest.mark.asyncio
    async def test_a_downstream_replay_of_the_republished_exit_is_a_no_op(
        self, db_runner, mock_redis, execution_runner
    ):
        """AC 1 (downstream half): if the original publish *did* land and only
        the mark_published was lost, execution sees the same order twice. Its
        opening ledger lookup must swallow the second one — one broker order."""
        runner, ledger, session = db_runner
        exit_id = await _orphan_a_stop_loss(runner, mock_redis)
        execution, order_manager = execution_runner

        await runner.run_periodic_risk_checks()
        (payload,) = _published_orders(mock_redis)
        order = ApprovedOrderMessage.from_stream_dict(payload)

        await execution.process_approved_order(order)
        await execution.process_approved_order(order)

        assert order_manager.submit_exit.await_count == 1
        session.rollback()
        assert ledger.get(exit_id).status == OrderStatus.SUBMITTED.value
        session.rollback()

    @pytest.mark.asyncio
    async def test_an_unpublished_exit_does_not_suppress(self, db_runner, mock_redis):
        """AC 2, the crux. An unpublished intent means "publish me", not "an
        exit is already working" — getting this backwards reinstates the mute."""
        runner, _ledger, session = db_runner
        await _orphan_a_stop_loss(runner, mock_redis)

        assert runner._has_pending_sell(_target()) is False
        session.rollback()

    @pytest.mark.asyncio
    async def test_a_published_exit_still_suppresses(self, db_runner, mock_redis):
        """AC 3: KAN-7's rule is preserved for the published case — that intent
        really is in flight, and a second sell would oversell on the first fill."""
        runner, _ledger, session = db_runner
        _breaching(runner)
        await runner.run_stop_loss_check()

        assert runner._has_pending_sell(_target()) is True
        session.rollback()

    @pytest.mark.asyncio
    async def test_an_unpublished_proposed_sell_still_suppresses(
        self, db_runner, mock_redis
    ):
        """The narrowing is scoped to *risk's own* APPROVED exits. A PROPOSED
        sell with published_at NULL belongs to run_paper's recommendation
        outbox, which will publish it to stream:recommendations — treating it as
        an orphan here would publish an unapproved order to execution."""
        runner, ledger, session = db_runner
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id="sleeve-2026-08-16-momentum-AAPL-sell",
                account_id="DUTEST",
                mode="paper",
                portfolio="momentum",
                con_id=111,
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
                action="SELL",
                quantity=10.0,
                limit_price=None,
                order_type="MKT",
            )
        )
        session.commit()

        assert runner._has_pending_sell(_target()) is True
        await runner._republish_unpublished_exits()

        assert _published_orders(mock_redis) == []
        session.rollback()

    @pytest.mark.asyncio
    async def test_a_terminal_intent_is_never_republished(self, db_runner, mock_redis):
        """AC 6: published_at is not the test — the lifecycle is. An orphan that
        somebody flattened by hand (or that execution filled before the mark
        landed) must not be sold a second time."""
        runner, ledger, session = db_runner
        exit_id = await _orphan_a_stop_loss(runner, mock_redis)
        ledger.transition(exit_id, OrderStatus.SUBMITTED)
        ledger.transition(exit_id, OrderStatus.FILLED)
        session.commit()
        assert ledger.get(exit_id).published_at is None
        session.rollback()

        await runner._republish_unpublished_exits()

        assert _published_orders(mock_redis) == []
        session.rollback()

    @pytest.mark.asyncio
    async def test_published_at_is_set_on_a_successful_emit(
        self, db_runner, mock_redis
    ):
        """AC 4, first half: the emitter marks the intent published *after* the
        publish returns, so the column means what the sweep assumes it means."""
        runner, ledger, session = db_runner

        await runner._emit_ledgered_exit(
            "liq", _target(), exit_id="liq-paper-AAPL-1", reason="emergency"
        )

        assert ledger.get("liq-paper-AAPL-1").published_at is not None
        session.rollback()

    @pytest.mark.asyncio
    async def test_a_second_consecutive_republish_alerts_and_the_first_does_not(
        self, db_runner, mock_redis
    ):
        """AC 5: one re-publish is a recovered crash and needs no operator. The
        same intent needing it again next scan means publishes are not sticking
        — that is a broken pipe, and it pages."""
        runner, ledger, session = db_runner
        exit_id = await _orphan_a_stop_loss(runner, mock_redis)

        await runner._republish_unpublished_exits()
        assert [a for a in _alerts(mock_redis) if "exit_republish_repeated" in a] == []

        # published_at did not stick — the failure mode the alert exists for.
        ledger.get(exit_id).published_at = None
        session.commit()
        await runner._republish_unpublished_exits()

        repeated = [a for a in _alerts(mock_redis) if "exit_republish_repeated" in a]
        assert len(repeated) == 1
        assert exit_id in repeated[0]
        assert "high" in repeated[0]
        session.rollback()

    @pytest.mark.asyncio
    async def test_the_repeat_alert_fires_when_the_publish_itself_keeps_failing(
        self, db_runner, mock_redis
    ):
        """AC 5, the case the alert actually exists for.

        "Publishes are not sticking" usually means the publish is *raising*, and
        the sweep is fail-fast — so an alert emitted after the publish loop
        would never be reached and a permanently dead sweep would page nobody.
        """
        runner, _ledger, session = db_runner
        exit_id = await _orphan_a_stop_loss(runner, mock_redis)
        _crash_on_exit_publish(mock_redis)

        for _ in range(2):
            with pytest.raises(_PublishCrash):
                await runner._republish_unpublished_exits()

        repeated = [a for a in _alerts(mock_redis) if "exit_republish_repeated" in a]
        assert len(repeated) == 1
        assert exit_id in repeated[0]
        session.rollback()

    @pytest.mark.asyncio
    async def test_a_submitted_exit_is_neither_republished_nor_unsuppressed(
        self, db_runner, mock_redis
    ):
        """The other half of the crash window: the publish landed and only the
        mark was lost, so execution already has the order. Re-publishing is
        pointless and — far worse — letting it stop suppressing would put a
        second sell against shares the broker is already selling."""
        runner, ledger, session = db_runner
        exit_id = await _orphan_a_stop_loss(runner, mock_redis)
        ledger.transition(exit_id, OrderStatus.SUBMITTED)
        session.commit()
        assert ledger.get(exit_id).published_at is None
        session.rollback()

        await runner._republish_unpublished_exits()

        assert _published_orders(mock_redis) == []
        assert runner._has_pending_sell(_target()) is True
        session.rollback()

    @pytest.mark.asyncio
    async def test_an_exit_approved_before_today_is_escalated_not_republished(
        self, db_runner, mock_redis
    ):
        """An exit is a decision about a price that has since moved on.

        Every exit intent predating KAN-8 has published_at NULL — nothing ever
        wrote that column on the risk side — so an unbounded sweep would fire
        days-old stop-losses at today's market on the first scan after deploy,
        against positions that may have been flattened by hand since.
        """
        runner, ledger, session = db_runner
        exit_id = await _orphan_a_stop_loss(runner, mock_redis)
        ledger.get(exit_id).approved_at = datetime.now(timezone.utc) - timedelta(
            days=3
        )
        session.commit()

        await runner._republish_unpublished_exits()

        assert _published_orders(mock_redis) == []
        stale = [a for a in _alerts(mock_redis) if "exit_orphan_stale" in a]
        assert len(stale) == 1
        assert exit_id in stale[0]
        assert "critical" in stale[0]
        session.rollback()

    @pytest.mark.asyncio
    async def test_a_stale_orphan_alerts_once_not_every_scan(
        self, db_runner, mock_redis
    ):
        """Nobody can clear it inside a scan interval, and an alert that repeats
        every 30 minutes is an alert that gets muted."""
        runner, ledger, session = db_runner
        exit_id = await _orphan_a_stop_loss(runner, mock_redis)
        ledger.get(exit_id).approved_at = datetime.now(timezone.utc) - timedelta(
            days=3
        )
        session.commit()

        for _ in range(3):
            await runner._republish_unpublished_exits()

        assert len([a for a in _alerts(mock_redis) if "exit_orphan_stale" in a]) == 1
        session.rollback()

    @pytest.mark.asyncio
    async def test_a_stale_orphan_still_does_not_suppress_a_fresh_breach(
        self, db_runner, mock_redis
    ):
        """Leaving the stale row inert must not re-create the mute this story
        exists to remove: today's breach gets its own correctly-sized exit."""
        runner, ledger, session = db_runner
        stale_id = await _orphan_a_stop_loss(runner, mock_redis)
        ledger.get(stale_id).approved_at = datetime.now(timezone.utc) - timedelta(
            days=3
        )
        session.commit()
        _breaching(runner)

        await runner._republish_unpublished_exits()
        await runner.run_stop_loss_check()

        (order,) = _published_orders(mock_redis)
        assert order["recommendation_id"] != stale_id
        assert float(order["quantity"]) == pytest.approx(10.0)
        session.rollback()
