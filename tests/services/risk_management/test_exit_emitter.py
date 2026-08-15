"""The single publish site for risk-side exits (KAN-5).

Every risk-side sell must be published by :meth:`_emit_ledgered_exit` and by
nothing else. An order published without a backing ledger intent is rejected by
execution and dead-letters silently — the failure mode that left stop-loss sells
unexecuted for weeks. These tests pin the emitter's contract so the callers
added in KAN-7 inherit a method already proven identical to what shipped.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from services.risk_management import runner as runner_module
from services.risk_management.runner import RiskServiceRunner
from shared.config import AppConfig, CurrencyConfig, RiskConfig
from shared.models import Base, OrderIntent, OrderStatus, Position
from shared.order_ledger import OrderLedger

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


def test_emit_ledgered_exit_is_the_only_exit_publish_site():
    """Exactly one exit publish site, and it is the emitter.

    Written as a strict xfail in KAN-5 and flipped here (AC 8): stop-loss and
    passive-trim now route through the emitter, so the assertion holds for the
    first time. It stays as a live test because the regression it catches —
    someone adding a second, unbacked publish — is the original P0.
    """
    exit_sites = {
        method: count
        for method, count in _approved_order_publish_sites().items()
        if method != ENTRY_PATH_PUBLISHER
    }
    assert exit_sites == {"_emit_ledgered_exit": 1}


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
