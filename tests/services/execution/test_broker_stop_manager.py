"""KAN-19 — sizing and placing the protective stop, and ledgering it.

The ledger row is not bookkeeping. The spike proved on the live pipeline that
an *unledgered* resting stop makes reconciliation report ``major`` and disables
entries: the 08-15 04:15 paper run went from "ok / enabled" to
"major / disabled" with the spike's stop as the only change.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.execution.broker_stops import BrokerStopManager, ips_stop_price
from shared.models import Base, OrderStatus, Position
from shared.order_ledger import OrderLedger

OPENED_AT = datetime(2026, 8, 16, 13, 30, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def make_position(
    *,
    ticker: str = "AAPL",
    con_id: int = 265598,
    quantity: float = 21,
    highest: float = 220.65,
    account_id: str | None = "DUN551088",
    portfolio: str = "momentum",
    status: str = "open",
) -> Position:
    return Position(
        account_id=account_id,
        ticker=ticker,
        portfolio=portfolio,
        con_id=con_id,
        exchange="SMART",
        currency="USD",
        quantity=quantity,
        avg_entry_price=200.0,
        current_price=highest,
        peak_price=highest,
        highest_price_since_entry=highest,
        opened_at=OPENED_AT,
        status=status,
    )


def make_manager(session, *, order_manager=None, enabled=True, **kwargs):
    order_manager = order_manager or AsyncMock()
    order_manager.submit_stop = AsyncMock(return_value="4242")
    defaults = dict(
        order_manager=order_manager,
        order_ledger=OrderLedger(session),
        mode="paper",
        account_id="DUN551088",
        trailing_pct=15.0,
        enabled=enabled,
    )
    defaults.update(kwargs)
    return BrokerStopManager(**defaults), order_manager


class TestIpsStopPrice:
    def test_is_the_trailing_percentage_below_the_high(self):
        """The same rule RiskEngine.check_stop_loss fires on."""
        assert ips_stop_price(220.0, 15.0) == pytest.approx(187.0)

    def test_is_rounded_to_a_tradeable_tick(self):
        """IB rejects a stop price with sub-penny precision."""
        assert ips_stop_price(220.65, 15.0) == pytest.approx(187.55)

    def test_rejects_a_reference_price_that_cannot_produce_a_stop(self):
        with pytest.raises(ValueError):
            ips_stop_price(0.0, 15.0)


class TestPlacementOnOpen:
    async def test_places_a_stop_at_the_ips_level_for_the_whole_position(
        self, session
    ):
        manager, order_manager = make_manager(session)

        order_id = await manager.ensure_coverage(
            account_id="DUN551088",
            portfolio="momentum",
            con_id=265598,
            symbol="AAPL",
            exchange="SMART",
            currency="USD",
            quantity=21,
            reference_price=220.65,
        )

        assert order_id == "4242"
        kwargs = order_manager.submit_stop.await_args.kwargs
        assert kwargs["ticker"] == "AAPL"
        assert kwargs["quantity"] == pytest.approx(21)
        assert kwargs["stop_price"] == pytest.approx(187.55)
        assert kwargs["tif"] == "GTC"

    async def test_the_stop_gets_a_submitted_ledger_intent(self, session):
        """AC6 — and the thing that keeps reconciliation from disabling entries."""
        manager, _ = make_manager(session)

        await manager.ensure_coverage(
            account_id="DUN551088", portfolio="momentum", con_id=265598,
            symbol="AAPL", exchange="SMART", currency="USD",
            quantity=21, reference_price=220.65,
        )

        intent = OrderLedger(session).get_by_ib_order_id("4242")
        assert intent is not None
        assert intent.order_type == "stop"
        assert intent.action == "SELL"
        assert intent.status == OrderStatus.SUBMITTED.value
        assert intent.requested_quantity == pytest.approx(21)
        assert intent.con_id == 265598
        assert intent.account_id == "DUN551088"

    async def test_a_stop_reserves_no_buying_power(self, session):
        """A protective sell is not a claim on cash."""
        manager, _ = make_manager(session)

        await manager.ensure_coverage(
            account_id="DUN551088", portfolio="momentum", con_id=265598,
            symbol="AAPL", exchange="SMART", currency="USD",
            quantity=21, reference_price=220.65,
        )

        intent = OrderLedger(session).get_by_ib_order_id("4242")
        assert intent.reserved_notional == pytest.approx(0.0)

    async def test_an_already_covered_position_gets_no_second_stop(self, session):
        """AC3 — coverage must equal the position, never exceed it."""
        manager, order_manager = make_manager(session)
        kwargs = dict(
            account_id="DUN551088", portfolio="momentum", con_id=265598,
            symbol="AAPL", exchange="SMART", currency="USD",
            quantity=21, reference_price=220.65,
        )
        await manager.ensure_coverage(**kwargs)

        second = await manager.ensure_coverage(**kwargs)

        assert second is None
        assert order_manager.submit_stop.await_count == 1

    async def test_only_the_uncovered_shortfall_is_placed(self, session):
        """A position topped up mid-life gets a stop for the new shares only."""
        manager, order_manager = make_manager(session)
        await manager.ensure_coverage(
            account_id="DUN551088", portfolio="momentum", con_id=265598,
            symbol="AAPL", exchange="SMART", currency="USD",
            quantity=21, reference_price=220.65,
        )
        order_manager.submit_stop = AsyncMock(return_value="4243")

        await manager.ensure_coverage(
            account_id="DUN551088", portfolio="momentum", con_id=265598,
            symbol="AAPL", exchange="SMART", currency="USD",
            quantity=30, reference_price=220.65,
        )

        assert order_manager.submit_stop.await_args.kwargs["quantity"] == pytest.approx(9)
        ledger = OrderLedger(session)
        assert ledger.open_stop_quantity("DUN551088", "momentum", 265598) == pytest.approx(30)

    async def test_each_placement_gets_its_own_recommendation_id(self, session):
        manager, order_manager = make_manager(session)
        await manager.ensure_coverage(
            account_id="DUN551088", portfolio="momentum", con_id=265598,
            symbol="AAPL", exchange="SMART", currency="USD",
            quantity=21, reference_price=220.65,
        )
        first = order_manager.submit_stop.await_args.kwargs["recommendation_id"]
        order_manager.submit_stop = AsyncMock(return_value="4243")

        await manager.ensure_coverage(
            account_id="DUN551088", portfolio="momentum", con_id=265598,
            symbol="AAPL", exchange="SMART", currency="USD",
            quantity=30, reference_price=220.65,
        )
        second = order_manager.submit_stop.await_args.kwargs["recommendation_id"]

        assert first != second
        assert first.startswith("stop-DUN551088-momentum-265598-")

    async def test_a_broker_refusal_is_recorded_not_swallowed(self, session):
        """An unplaced stop must never leave a SUBMITTED row claiming cover."""
        manager, order_manager = make_manager(session)
        order_manager.submit_stop = AsyncMock(side_effect=RuntimeError("IB said no"))

        order_id = await manager.ensure_coverage(
            account_id="DUN551088", portfolio="momentum", con_id=265598,
            symbol="AAPL", exchange="SMART", currency="USD",
            quantity=21, reference_price=220.65,
        )

        assert order_id is None
        ledger = OrderLedger(session)
        assert ledger.open_stop_quantity("DUN551088", "momentum", 265598) == 0.0
        assert ledger.get(
            "stop-DUN551088-momentum-265598-0"
        ).status == OrderStatus.SUBMISSION_FAILED.value

    async def test_a_failed_placement_can_be_retried(self, session):
        """The next attempt must not collide with the failed row's id."""
        manager, order_manager = make_manager(session)
        order_manager.submit_stop = AsyncMock(side_effect=RuntimeError("IB said no"))
        kwargs = dict(
            account_id="DUN551088", portfolio="momentum", con_id=265598,
            symbol="AAPL", exchange="SMART", currency="USD",
            quantity=21, reference_price=220.65,
        )
        await manager.ensure_coverage(**kwargs)
        order_manager.submit_stop = AsyncMock(return_value="4243")

        assert await manager.ensure_coverage(**kwargs) == "4243"


class TestFlagOff:
    """AC4 — with the flag off, behaviour is byte-identical to today."""

    async def test_places_nothing(self, session):
        manager, order_manager = make_manager(session, enabled=False)

        order_id = await manager.ensure_coverage(
            account_id="DUN551088", portfolio="momentum", con_id=265598,
            symbol="AAPL", exchange="SMART", currency="USD",
            quantity=21, reference_price=220.65,
        )

        assert order_id is None
        order_manager.submit_stop.assert_not_awaited()

    async def test_writes_no_ledger_row(self, session):
        manager, _ = make_manager(session, enabled=False)

        await manager.ensure_coverage(
            account_id="DUN551088", portfolio="momentum", con_id=265598,
            symbol="AAPL", exchange="SMART", currency="USD",
            quantity=21, reference_price=220.65,
        )

        assert OrderLedger(session).open_stop_quantity(
            "DUN551088", "momentum", 265598
        ) == 0.0

    async def test_backfill_does_nothing(self, session):
        session.add(make_position())
        session.commit()
        manager, order_manager = make_manager(session, enabled=False)

        assert await manager.backfill_open_positions() == []
        order_manager.submit_stop.assert_not_awaited()


class TestStartupBackfill:
    """AC2 — two covered positions and one uncovered."""

    async def _covered(self, session, manager, position, order_id):
        manager._order_manager.submit_stop = AsyncMock(return_value=order_id)
        await manager.ensure_coverage(
            account_id=position.account_id,
            portfolio=position.portfolio,
            con_id=position.con_id,
            symbol=position.ticker,
            exchange="SMART",
            currency="USD",
            quantity=position.quantity,
            reference_price=position.highest_price_since_entry,
        )

    async def test_only_the_uncovered_position_gets_a_stop(self, session):
        covered_a = make_position(ticker="AAPL", con_id=265598, quantity=21)
        covered_b = make_position(ticker="MSFT", con_id=272093, quantity=8)
        uncovered = make_position(
            ticker="NVDA", con_id=4815747, quantity=13, highest=120.0
        )
        session.add_all([covered_a, covered_b, uncovered])
        session.commit()
        manager, order_manager = make_manager(session)
        await self._covered(session, manager, covered_a, "1")
        await self._covered(session, manager, covered_b, "2")
        order_manager.submit_stop = AsyncMock(return_value="3")

        placed = await manager.backfill_open_positions()

        assert placed == ["3"]
        kwargs = order_manager.submit_stop.await_args.kwargs
        assert kwargs["ticker"] == "NVDA"
        assert kwargs["quantity"] == pytest.approx(13)
        assert kwargs["stop_price"] == pytest.approx(102.0)

    async def test_the_stop_sits_below_the_high_not_below_todays_price(self, session):
        """The IPS rule trails the high, so backfill must read it, not the last."""
        position = make_position(quantity=21, highest=220.65)
        position.current_price = 150.0
        session.add(position)
        session.commit()
        manager, order_manager = make_manager(session)

        await manager.backfill_open_positions()

        assert order_manager.submit_stop.await_args.kwargs[
            "stop_price"
        ] == pytest.approx(187.55)

    async def test_closed_and_empty_positions_are_left_alone(self, session):
        session.add_all([
            make_position(ticker="AAPL", con_id=1, status="closed"),
            make_position(ticker="MSFT", con_id=2, quantity=0),
        ])
        session.commit()
        manager, order_manager = make_manager(session)

        assert await manager.backfill_open_positions() == []
        order_manager.submit_stop.assert_not_awaited()

    async def test_another_accounts_position_is_not_this_services_to_protect(
        self, session
    ):
        session.add(make_position(account_id="DU999999"))
        session.commit()
        manager, order_manager = make_manager(session)

        assert await manager.backfill_open_positions() == []
        order_manager.submit_stop.assert_not_awaited()

    async def test_a_position_with_no_contract_id_is_reported_not_guessed(
        self, session
    ):
        """Coverage is tracked per con_id; without one it cannot be asserted."""
        session.add(make_position(con_id=None))
        session.commit()
        manager, order_manager = make_manager(session)

        assert await manager.backfill_open_positions() == []
        order_manager.submit_stop.assert_not_awaited()

    async def test_one_failure_does_not_abandon_the_remaining_positions(
        self, session
    ):
        session.add_all([
            make_position(ticker="AAPL", con_id=265598, quantity=21),
            make_position(ticker="MSFT", con_id=272093, quantity=8),
        ])
        session.commit()
        manager, order_manager = make_manager(session)
        order_manager.submit_stop = AsyncMock(
            side_effect=[RuntimeError("IB said no"), "4243"]
        )

        placed = await manager.backfill_open_positions()

        assert placed == ["4243"]


class TestRunnerWiring:
    """AC1/AC2/AC4 — where the placement is actually triggered from."""

    def _runner(self, session, *, enabled=True, account_id="DUN551088"):
        from shared.config import AppConfig, ExecutionConfig, IBConfig
        from unittest.mock import MagicMock

        from services.execution.runner import ExecutionServiceRunner

        config = MagicMock(spec=AppConfig)
        config.execution = ExecutionConfig(
            broker_stops_enabled=enabled, broker_stops_account_id=account_id
        )
        config.ib = IBConfig()
        config.mode = "paper"
        config.risk = MagicMock()
        config.risk.stop_loss_trailing_pct = 15.0
        config.risk.min_viable_fill_pct = 40.0

        order_manager = MagicMock()
        order_manager.submit_stop = AsyncMock(return_value="4242")
        order_manager.open_orders = {}

        runner = ExecutionServiceRunner(
            config=config,
            redis_client=AsyncMock(),
            order_manager=order_manager,
            order_ledger=OrderLedger(session),
        )
        return runner, order_manager

    def _buy_fill(self, **overrides):
        payload = {
            "order_id": "order-1",
            "account_id": "DUN551088",
            "execution_id": "exec-1",
            "con_id": 265598,
            "ticker": "AAPL",
            "exchange": "SMART",
            "currency": "USD",
            "side": "buy",
            "quantity": 21,
            "cumulative_quantity": 21,
            "fill_price": 220.65,
            "timestamp": OPENED_AT,
            "order_done": True,
        }
        payload.update(overrides)
        return payload

    async def _seed_attribution(self, runner, session, portfolio="momentum"):
        """The buy intent the fill belongs to, as the real pipeline would."""
        from types import SimpleNamespace

        ledger = OrderLedger(session)
        ledger.create_intent(
            SimpleNamespace(
                recommendation_id="rec-1",
                account_id="DUN551088",
                mode="paper",
                portfolio=portfolio,
                con_id=265598,
                symbol="AAPL",
                exchange="SMART",
                currency="USD",
                action="BUY",
                quantity=21,
                limit_price=220.65,
                order_type="limit",
            )
        )
        ledger.transition("rec-1", OrderStatus.APPROVED)
        ledger.record_submission("rec-1", "order-1")
        session.commit()

    async def test_opening_a_position_places_its_stop(self, session):
        runner, order_manager = self._runner(session)
        await self._seed_attribution(runner, session)

        await runner.handle_ib_fill(self._buy_fill())

        kwargs = order_manager.submit_stop.await_args.kwargs
        assert kwargs["ticker"] == "AAPL"
        assert kwargs["quantity"] == pytest.approx(21)
        assert kwargs["stop_price"] == pytest.approx(187.55)

    async def test_a_sell_fill_places_no_stop(self, session):
        """Nothing is left to protect."""
        runner, order_manager = self._runner(session)
        await self._seed_attribution(runner, session)

        await runner.handle_ib_fill(self._buy_fill(side="sell"))

        order_manager.submit_stop.assert_not_awaited()

    async def test_the_stop_covers_the_whole_position_not_just_the_last_fill(
        self, session
    ):
        """AC3 — a second buy must leave total coverage at the held quantity."""
        runner, order_manager = self._runner(session)
        await self._seed_attribution(runner, session)
        await runner.handle_ib_fill(self._buy_fill())
        order_manager.submit_stop = AsyncMock(return_value="4243")

        await runner.handle_ib_fill(
            self._buy_fill(execution_id="exec-2", quantity=9)
        )

        assert order_manager.submit_stop.await_args.kwargs[
            "quantity"
        ] == pytest.approx(9)
        assert OrderLedger(session).open_stop_quantity(
            "DUN551088", "momentum", 265598
        ) == pytest.approx(30)

    async def test_the_flag_off_leaves_the_fill_path_untouched(self, session):
        runner, order_manager = self._runner(session, enabled=False)
        await self._seed_attribution(runner, session)

        await runner.handle_ib_fill(self._buy_fill())

        order_manager.submit_stop.assert_not_awaited()
        assert runner._redis.publish.await_count == 1

    async def test_a_failed_stop_does_not_break_the_fill(self, session):
        """The fill is real whatever the broker says about the stop."""
        runner, order_manager = self._runner(session)
        order_manager.submit_stop = AsyncMock(side_effect=RuntimeError("no"))
        await self._seed_attribution(runner, session)

        await runner.handle_ib_fill(self._buy_fill())

        assert runner._redis.publish.await_count >= 1

    async def test_an_unplaced_stop_raises_an_alert(self, session):
        """A position nobody is protecting must not fail silently."""
        runner, order_manager = self._runner(session)
        order_manager.submit_stop = AsyncMock(side_effect=RuntimeError("no"))
        await self._seed_attribution(runner, session)

        await runner.handle_ib_fill(self._buy_fill())

        published = [
            call.args[1] for call in runner._redis.publish.await_args_list
        ]
        assert any(
            row.get("event_type") == "broker_stop_not_placed" for row in published
        )

    async def test_a_stop_fill_is_attributed_to_its_intent(self, session):
        """AC6 — the fill of a resting stop is not an orphan."""
        runner, order_manager = self._runner(session)
        await self._seed_attribution(runner, session)
        await runner.handle_ib_fill(self._buy_fill())
        runner._redis.publish.reset_mock()

        await runner.handle_ib_fill(
            self._buy_fill(
                order_id="4242",
                execution_id="exec-stop",
                side="sell",
                quantity=21,
            )
        )

        fill = runner._redis.publish.await_args_list[0].args[1]
        assert fill["recommendation_id"] == "stop-DUN551088-momentum-265598-0"
        assert fill["portfolio"] == "momentum"

    async def test_startup_backfills_uncovered_positions(self, session):
        runner, order_manager = self._runner(session)
        session.add(make_position())
        session.commit()

        await runner.backfill_broker_stops()

        assert order_manager.submit_stop.await_args.kwargs[
            "stop_price"
        ] == pytest.approx(187.55)

    async def test_startup_backfill_is_inert_with_the_flag_off(self, session):
        runner, order_manager = self._runner(session, enabled=False)
        session.add(make_position())
        session.commit()

        await runner.backfill_broker_stops()

        order_manager.submit_stop.assert_not_awaited()

    async def test_setup_covers_positions_left_uncovered_by_a_prior_session(
        self, session
    ):
        """AC2 — a crash between fill and placement is invisible until startup."""
        runner, order_manager = self._runner(session)
        session.add(make_position())
        session.commit()
        runner._redis.drain_pending = AsyncMock(return_value=[])

        await runner.setup()

        assert order_manager.submit_stop.await_count == 1

    async def test_setup_is_unchanged_with_the_flag_off(self, session):
        runner, order_manager = self._runner(session, enabled=False)
        session.add(make_position())
        session.commit()
        runner._redis.drain_pending = AsyncMock(return_value=[])

        await runner.setup()

        order_manager.submit_stop.assert_not_awaited()

    async def test_a_stop_restored_after_a_restart_is_still_exempt_from_the_sweep(
        self, session
    ):
        """Otherwise the first close after a restart cancels every stop.

        ``restore_pending_orders`` rebuilds ``open_orders`` from the ledger,
        and an entry with no ``order_type`` is swept like an unfilled limit —
        so the protection would survive the Gateway restart the spike proved
        it survives, then be cancelled by our own housekeeping.
        """
        from services.execution.order_manager import OrderManager

        runner, _ = self._runner(session)
        manager, _ = make_manager(session)
        manager._order_manager = order_manager = AsyncMock()
        order_manager.submit_stop = AsyncMock(return_value="4242")
        await manager.ensure_coverage(
            account_id="DUN551088", portfolio="momentum", con_id=265598,
            symbol="AAPL", exchange="SMART", currency="USD",
            quantity=21, reference_price=220.65,
        )

        real_manager = OrderManager(
            executor=AsyncMock(), redis_client=AsyncMock(), db_session=session
        )
        runner._order_manager = real_manager
        runner.restore_pending_orders()

        assert real_manager.open_orders["4242"]["order_type"] == "stop"


class TestKillAndExitInteraction:
    """AC7 — how a resting stop meets the paths that sell the position."""

    def _runner_with_real_manager(self, session):
        from unittest.mock import MagicMock

        from services.execution.order_manager import OrderManager
        from services.execution.runner import ExecutionServiceRunner
        from shared.config import AppConfig, ExecutionConfig, IBConfig

        config = MagicMock(spec=AppConfig)
        config.execution = ExecutionConfig(
            broker_stops_enabled=True, broker_stops_account_id="DUN551088"
        )
        config.ib = IBConfig()
        config.mode = "paper"
        config.risk = MagicMock()
        config.risk.stop_loss_trailing_pct = 15.0

        executor = AsyncMock()
        executor.find_order_by_ref = AsyncMock(return_value=None)
        executor.submit_stop_order = AsyncMock(return_value="4242")
        executor.cancel_order = AsyncMock(return_value=True)
        order_manager = OrderManager(
            executor=executor, redis_client=AsyncMock(), db_session=session
        )
        runner = ExecutionServiceRunner(
            config=config,
            redis_client=AsyncMock(),
            order_manager=order_manager,
            order_ledger=OrderLedger(session),
        )
        return runner, order_manager, executor

    async def _rest_a_stop(self, runner):
        return await runner._broker_stops.ensure_coverage(
            account_id="DUN551088", portfolio="momentum", con_id=265598,
            symbol="AAPL", exchange="SMART", currency="USD",
            quantity=21, reference_price=220.65,
        )

    async def test_the_kill_cancels_the_stop_before_it_liquidates(self, session):
        """Spike Q4, the failure this design must not reproduce.

        A stop the cancel-all cannot see survives the kill and then rests
        against a position the liquidation already flattened — so it sells
        short when it triggers. Placing stops on the execution client's own
        connection, tracked in ``open_orders``, is what puts them inside the
        cancel-all that runs before liquidation.
        """
        runner, order_manager, executor = self._runner_with_real_manager(session)
        await self._rest_a_stop(runner)
        assert "4242" in order_manager.open_orders

        cancelled = await order_manager.cancel_all_orders()

        assert "4242" in cancelled
        executor.cancel_order.assert_awaited_with("4242")
        assert order_manager.open_orders == {}

    async def test_a_resting_stop_makes_an_exit_refuse_rather_than_oversell(
        self, session
    ):
        """The documented KAN-19/KAN-20 seam, pinned so it cannot drift.

        The oversell guard subtracts sells already working from the broker
        position, and a full-coverage stop *is* a working sell — so a
        flattening exit sizes to zero and is refused with an alert. Refusing
        is the safe half: two live sells against 21 shares is how a long-only
        account ends up short (KAN-10).

        Cancelling the stop first, so the exit can proceed, is adjustment —
        KAN-20's work. Until it lands, turning the flag on blocks exits on any
        stopped position, which is why it stays off.
        """
        runner, order_manager, executor = self._runner_with_real_manager(session)
        await self._rest_a_stop(runner)
        ledger = OrderLedger(session)

        working = ledger.outstanding_sell_quantity(
            account_id="DUN551088", con_id=265598
        )

        assert working == pytest.approx(21.0)
