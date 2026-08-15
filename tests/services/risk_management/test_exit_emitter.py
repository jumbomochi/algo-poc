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
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.risk_management import runner as runner_module
from services.risk_management.runner import RiskServiceRunner
from shared.config import AppConfig, CurrencyConfig, RiskConfig
from shared.models import Base, OrderStatus, Position
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "run_stop_loss_check and _trim_position_to_target still publish their "
        "own unbacked orders; KAN-7 routes them through the emitter and must "
        "then delete this marker"
    ),
)
def test_emit_ledgered_exit_is_the_only_exit_publish_site():
    """Exactly one exit publish site, and it is the emitter.

    Strict xfail is the sequencing enforcement: this goes green the moment
    KAN-7 lands, and a strict xfail that unexpectedly passes fails the suite —
    so KAN-7 cannot forget to flip it.
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
