from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import DataError, OperationalError
from sqlalchemy.orm import Session

from services.portfolio_accounting.projector import (
    FillConflictError,
    FillProjectionError,
    FillProjector,
    InvalidFillError,
    UnattributedFillError,
)
from shared.models import (
    Base,
    ExecutionFill,
    OrderIntent,
    OrderStatus,
    PortfolioConfig,
    Position,
)
from shared.order_ledger import OrderLedger
from shared.schemas.messages import FillMessage


NOW = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


@pytest.fixture
def projector(session: Session) -> FillProjector:
    return FillProjector(session)


def seed_intent(
    session: Session,
    *,
    recommendation_id: str = "rec-1",
    action: str = "BUY",
    quantity: float = 10,
    status: OrderStatus = OrderStatus.SUBMITTED,
    filled_quantity: float = 0,
    reason: str | None = None,
) -> OrderIntent:
    session.add(
        PortfolioConfig(
            portfolio="momentum",
            capital=10_000,
            cash=10_000,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    intent = OrderIntent(
        recommendation_id=recommendation_id,
        account_id="DU12345",
        mode="paper",
        portfolio="momentum",
        con_id=265598,
        symbol="AAPL",
        exchange="SMART",
        currency="USD",
        action=action,
        requested_quantity=quantity,
        limit_price=100,
        order_type="LMT",
        reserved_notional=quantity * 100 if action == "BUY" else 0,
        filled_quantity=filled_quantity,
        status=status.value,
        reason=reason,
        ib_order_id="42",
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(intent)
    session.commit()
    return intent


def make_fill(
    execution_id: str = "e-1",
    *,
    recommendation_id: str = "rec-1",
    side: str = "buy",
    quantity: float = 10,
    cumulative: float | None = 10,
    price: float = 100,
    commission: float = 1,
    commission_currency: str | None = "USD",
    commission_trading: float | None = None,
    commission_fx_base_per_trading: float | None = None,
    order_id: str = "42",
    account_id: str = "DU12345",
    ticker: str = "AAPL",
    exchange: str = "SMART",
    timestamp: datetime = NOW,
    order_done: bool = False,
) -> FillMessage:
    if commission_trading is None and commission_currency == "USD":
        commission_trading = commission
    return FillMessage(
        ticker=ticker,
        timestamp=timestamp,
        side=side,
        quantity=quantity,
        fill_price=price,
        commission=commission,
        commission_currency=commission_currency,
        commission_trading=commission_trading,
        commission_fx_base_per_trading=commission_fx_base_per_trading,
        recommendation_id=recommendation_id,
        order_id=order_id,
        execution_id=execution_id,
        account_id=account_id,
        cumulative_quantity=cumulative,
        portfolio="momentum",
        con_id=265598,
        exchange=exchange,
        currency="USD",
        order_done=order_done,
    )


def get_position(session: Session) -> Position | None:
    return session.scalar(
        select(Position).where(
            Position.portfolio == "momentum",
            Position.ticker == "AAPL",
            Position.status == "open",
        )
    )


def get_cash(session: Session) -> float:
    return float(session.scalar(select(PortfolioConfig.cash)))


def fill_count(session: Session) -> int:
    return int(session.scalar(select(func.count(ExecutionFill.id))) or 0)


def test_smart_routed_fill_accepts_actual_execution_venue(projector, session):
    """A SMART-routed order fills on a specific venue (NASDAQ/ARCA/IBKRATS/NYSE),
    never 'SMART'. The projector must not treat the intent(SMART) vs fill(venue)
    difference as an attribution conflict — otherwise every real fill is DLQ'd
    and execution_fills never populates.
    """
    seed_intent(session)  # intent.exchange == "SMART"

    assert projector.apply(make_fill(exchange="NASDAQ")) is True

    assert get_position(session).quantity == 10
    assert get_position(session).account_id == "DU12345"


def test_new_position_gets_sector_from_universe_map(projector, session):
    """A projected buy fill must write the ticker's real sector. NULL-sector
    rows collapse into one 'Unknown' bucket in the risk service, which trips
    the sector concentration limit and freezes all new entries."""
    seed_intent(session)

    assert projector.apply(make_fill()) is True

    assert get_position(session).sector == "Technology"  # AAPL


def test_replayed_buy_fill_changes_cash_once(projector, session):
    seed_intent(session)
    fill = make_fill()

    assert projector.apply(fill) is True
    assert projector.apply(fill) is False

    assert get_position(session).quantity == 10
    assert get_position(session).account_id == "DU12345"
    assert get_cash(session) == pytest.approx(8_999)
    assert fill_count(session) == 1
    assert session.scalar(select(ExecutionFill)).projection_applied is True


@pytest.mark.parametrize(
    "terminal_status",
    [OrderStatus.CANCELLED, OrderStatus.EXPIRED],
)
def test_late_fill_applies_without_reopening_terminal_intent(
    projector, session, terminal_status
):
    seed_intent(
        session,
        status=terminal_status,
        reason="broker terminal remainder",
    )

    assert projector.apply(make_fill(
        quantity=2,
        cumulative=2,
        commission=1,
    )) is True

    intent = session.scalar(select(OrderIntent))
    assert intent.status == terminal_status.value
    assert intent.reason == "broker terminal remainder"
    assert intent.filled_quantity == pytest.approx(2)
    assert get_position(session).quantity == pytest.approx(2)
    assert get_cash(session) == pytest.approx(9_799)


def test_late_partial_remainder_fill_preserves_cancellation_and_is_idempotent(
    projector, session
):
    seed_intent(session)
    assert projector.apply(make_fill(
        "e-1", quantity=4, cumulative=4, commission=0
    )) is True
    OrderLedger(session).transition(
        "rec-1",
        OrderStatus.CANCELLED,
        reason="partial remainder cancelled",
    )
    session.commit()
    late = make_fill("e-2", quantity=2, cumulative=6, commission=1)

    assert projector.apply(late) is True
    assert projector.apply(late) is False

    intent = session.scalar(select(OrderIntent))
    assert intent.status == OrderStatus.CANCELLED.value
    assert intent.reason == "partial remainder cancelled"
    assert intent.filled_quantity == pytest.approx(6)
    assert get_position(session).quantity == pytest.approx(6)
    assert get_cash(session) == pytest.approx(9_399)


@pytest.mark.parametrize(
    "impossible_status",
    [OrderStatus.RISK_REJECTED, OrderStatus.SUBMISSION_FAILED],
)
def test_fill_still_rejects_impossible_pre_submission_terminal_status(
    projector, session, impossible_status
):
    seed_intent(session, status=impossible_status)

    with pytest.raises(InvalidFillError, match="cannot accept"):
        projector.apply(make_fill())

    assert get_position(session) is None
    assert get_cash(session) == pytest.approx(10_000)
    assert session.scalar(select(ExecutionFill)).projection_applied is False


def test_usd_commission_is_preserved_and_applied_in_trading_currency(
    projector, session
):
    seed_intent(session)

    assert projector.apply(make_fill(
        commission=1.25,
        commission_currency="USD",
        commission_trading=1.25,
    )) is True

    stored = session.scalar(select(ExecutionFill))
    assert stored.commission == pytest.approx(1.25)
    assert stored.commission_currency == "USD"
    assert stored.commission_trading == pytest.approx(1.25)
    assert stored.commission_fx_base_per_trading is None
    assert get_cash(session) == pytest.approx(8_998.75)


def test_sgd_commission_preserves_original_and_applies_translated_usd(
    projector, session
):
    seed_intent(session)

    assert projector.apply(make_fill(
        commission=1.25,
        commission_currency="SGD",
        commission_trading=1.0,
        commission_fx_base_per_trading=1.25,
    )) is True

    stored = session.scalar(select(ExecutionFill))
    assert stored.commission == pytest.approx(1.25)
    assert stored.commission_currency == "SGD"
    assert stored.commission_trading == pytest.approx(1.0)
    assert stored.commission_fx_base_per_trading == pytest.approx(1.25)
    assert get_cash(session) == pytest.approx(8_999.0)


@pytest.mark.parametrize(
    ("commission_currency", "commission_trading"),
    [("SGD", None), ("EUR", None)],
)
def test_untranslated_commission_is_audited_without_sleeve_mutation(
    projector, session, commission_currency, commission_trading
):
    seed_intent(session)

    with pytest.raises(InvalidFillError, match="commission"):
        projector.apply(make_fill(
            commission=1.25,
            commission_currency=commission_currency,
            commission_trading=commission_trading,
        ))

    stored = session.scalar(select(ExecutionFill))
    assert stored.commission_currency == commission_currency
    assert stored.commission_trading is None
    assert stored.projection_applied is False
    assert get_cash(session) == pytest.approx(10_000)


def test_replayed_execution_rejects_changed_commission_translation(
    projector, session
):
    seed_intent(session)
    fill = make_fill(
        commission=1.25,
        commission_currency="SGD",
        commission_trading=1.0,
        commission_fx_base_per_trading=1.25,
    )
    assert projector.apply(fill) is True

    with pytest.raises(FillConflictError, match="commission_trading"):
        projector.apply(make_fill(
            commission=1.25,
            commission_currency="SGD",
            commission_trading=0.99,
            commission_fx_base_per_trading=1.25,
        ))


def test_fill_does_not_mutate_unowned_legacy_position(projector, session):
    seed_intent(session)
    session.add(Position(
        account_id=None,
        ticker="AAPL", portfolio="momentum", con_id=265598,
        exchange="SMART", currency="USD", quantity=1,
        avg_entry_price=90, current_price=90, peak_price=90,
        highest_price_since_entry=90, opened_at=NOW, status="open",
    ))
    session.commit()

    with pytest.raises(InvalidFillError, match="account ownership"):
        projector.apply(make_fill())

    positions = list(session.scalars(select(Position)))
    assert len(positions) == 1
    assert positions[0].account_id is None
    assert positions[0].quantity == 1


@pytest.mark.parametrize(
    ("legacy_portfolio", "legacy_ticker"),
    [
        ("other_sleeve", "AAPL"),
        ("momentum", "AAPL.A"),
    ],
)
def test_fill_rejects_unowned_contract_across_sleeves_and_ticker_aliases(
    projector, session, legacy_portfolio, legacy_ticker
):
    seed_intent(session)
    session.add(Position(
        account_id=None,
        ticker=legacy_ticker,
        portfolio=legacy_portfolio,
        con_id=265598,
        exchange="SMART",
        currency="USD",
        quantity=1,
        avg_entry_price=90,
        current_price=90,
        peak_price=90,
        highest_price_since_entry=90,
        opened_at=NOW,
        status="open",
    ))
    session.commit()

    with pytest.raises(InvalidFillError, match="account ownership"):
        projector.apply(make_fill())

    positions = list(session.scalars(select(Position)))
    assert len(positions) == 1
    assert positions[0].portfolio == legacy_portfolio
    assert positions[0].ticker == legacy_ticker
    assert positions[0].quantity == 1


def test_replayed_fill_accepts_equivalent_timestamp_offset(projector, session):
    seed_intent(session)
    fill = make_fill()

    assert projector.apply(fill) is True
    assert projector.apply(make_fill(
        timestamp=NOW.astimezone(timezone(timedelta(hours=8)))
    )) is False

    assert get_position(session).quantity == 10
    assert get_cash(session) == pytest.approx(8_999)


@pytest.mark.parametrize(
    "change",
    [
        {"price": 101},
        {"timestamp": datetime(2026, 7, 19, 8, 1, tzinfo=timezone.utc)},
    ],
)
def test_conflicting_reuse_of_execution_identity_is_rejected(
    projector, session, change
):
    seed_intent(session)
    assert projector.apply(make_fill()) is True

    with pytest.raises(FillConflictError):
        projector.apply(make_fill(**change))

    assert get_position(session).quantity == 10
    assert get_cash(session) == pytest.approx(8_999)


def test_partial_fills_weight_average_and_complete_intent(projector, session):
    seed_intent(session)

    projector.apply(make_fill("e-1", quantity=4, cumulative=4, price=100, commission=0))
    session.expire_all()
    intent = session.scalar(select(OrderIntent))
    assert intent.status == OrderStatus.PARTIALLY_FILLED.value
    assert intent.filled_quantity == pytest.approx(4)

    projector.apply(make_fill("e-2", quantity=6, cumulative=10, price=110, commission=0))
    session.expire_all()
    intent = session.scalar(select(OrderIntent))
    assert intent.status == OrderStatus.FILLED.value
    assert intent.filled_quantity == pytest.approx(10)
    assert get_position(session).avg_entry_price == pytest.approx(106)


def test_delayed_fills_project_after_completed_history_marked_filled(projector, session):
    seed_intent(
        session,
        status=OrderStatus.FILLED,
        filled_quantity=10,
    )

    assert projector.apply(make_fill("e-1", quantity=4, cumulative=4)) is True
    assert projector.apply(make_fill("e-2", quantity=6, cumulative=10)) is True

    session.expire_all()
    intent = session.scalar(select(OrderIntent))
    assert intent.status == OrderStatus.FILLED.value
    assert intent.filled_quantity == pytest.approx(10)
    assert get_position(session).quantity == pytest.approx(10)


def test_rejected_fill_is_not_reconstructed_as_applied_for_delayed_history(
    projector, session
):
    seed_intent(
        session,
        status=OrderStatus.FILLED,
        filled_quantity=10,
    )
    config = session.scalar(select(PortfolioConfig))
    config.cash = 0
    session.commit()

    with pytest.raises(InvalidFillError, match="cash"):
        projector.apply(make_fill("e-1", quantity=4, cumulative=4, commission=0))

    config = session.scalar(select(PortfolioConfig))
    config.cash = 10_000
    session.commit()
    with pytest.raises(InvalidFillError, match="cumulative"):
        projector.apply(make_fill("e-2", quantity=6, cumulative=10, commission=0))

    assert get_position(session) is None
    assert get_cash(session) == pytest.approx(10_000)
    fills = session.scalars(select(ExecutionFill).order_by(ExecutionFill.id)).all()
    assert [fill.projection_applied for fill in fills] == [False, False]


def test_unknown_fill_is_audited_without_position_or_cash_mutation(projector, session):
    seed_intent(session)
    before_cash = get_cash(session)

    with pytest.raises(UnattributedFillError):
        projector.apply(make_fill("e-unknown", recommendation_id="unknown"))

    assert get_position(session) is None
    assert get_cash(session) == before_cash
    assert fill_count(session) == 1


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"account_id": "DU99999"}, "account"),
        ({"order_id": "99"}, "order"),
        ({"ticker": "MSFT"}, "symbol"),
        ({"side": "sell"}, "side"),
    ],
)
def test_mismatched_fill_is_audited_without_mutation(
    projector, session, change, match
):
    seed_intent(session)

    with pytest.raises(UnattributedFillError, match=match):
        projector.apply(make_fill(**change))

    assert get_position(session) is None
    assert get_cash(session) == 10_000
    assert fill_count(session) == 1


@pytest.mark.parametrize(
    "fill",
    [
        make_fill(quantity=11, cumulative=11),
        make_fill(quantity=4, cumulative=3),
        make_fill(quantity=-1, cumulative=-1),
        make_fill(price=0),
        make_fill(commission=-1),
    ],
)
def test_invalid_fill_economics_are_audited_without_mutation(
    projector, session, fill
):
    seed_intent(session)

    with pytest.raises(InvalidFillError):
        projector.apply(fill)

    assert get_position(session) is None
    assert get_cash(session) == 10_000
    assert fill_count(session) == 1


@pytest.mark.parametrize(
    "fill",
    [make_fill(price=float("nan")), make_fill(commission=float("inf"))],
)
def test_non_finite_fill_is_rejected_before_database_insert(projector, session, fill):
    seed_intent(session)

    with pytest.raises(InvalidFillError, match="finite"):
        projector.apply(fill)

    assert get_position(session) is None
    assert get_cash(session) == 10_000
    assert fill_count(session) == 0


def test_non_monotonic_cumulative_fill_is_audited_without_second_mutation(
    projector, session
):
    seed_intent(session)
    projector.apply(make_fill("e-1", quantity=6, cumulative=6, commission=0))

    with pytest.raises(InvalidFillError, match="cumulative"):
        projector.apply(make_fill("e-2", quantity=2, cumulative=5, commission=0))

    assert get_position(session).quantity == pytest.approx(6)
    assert get_cash(session) == pytest.approx(9_400)
    assert fill_count(session) == 2


def test_rejected_audit_does_not_poison_later_valid_cumulative(projector, session):
    seed_intent(session)
    with pytest.raises(InvalidFillError):
        projector.apply(make_fill("bad", quantity=11, cumulative=11))

    assert projector.apply(make_fill("good", quantity=10, cumulative=10)) is True
    assert get_position(session).quantity == pytest.approx(10)
    assert get_cash(session) == pytest.approx(8_999)


def test_partial_sell_and_full_close_apply_commissions(projector, session):
    seed_intent(session)
    projector.apply(make_fill(commission=1))

    buy_intent = session.scalar(select(OrderIntent))
    buy_intent.recommendation_id = "rec-buy"
    session.add(
        OrderIntent(
            recommendation_id="rec-sell",
            account_id="DU12345",
            mode="paper",
            portfolio="momentum",
            con_id=265598,
            symbol="AAPL",
            exchange="SMART",
            currency="USD",
            action="SELL",
            requested_quantity=10,
            limit_price=None,
            order_type="MKT",
            reserved_notional=0,
            filled_quantity=0,
            status=OrderStatus.SUBMITTED.value,
            ib_order_id="43",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    session.commit()

    projector.apply(make_fill(
        "s-1", recommendation_id="rec-sell", order_id="43", side="sell",
        quantity=4, cumulative=4, price=110, commission=2,
    ))
    assert get_position(session).quantity == pytest.approx(6)
    assert get_position(session).avg_entry_price == pytest.approx(100)
    assert get_cash(session) == pytest.approx(8_999 + 438)

    projector.apply(make_fill(
        "s-2", recommendation_id="rec-sell", order_id="43", side="sell",
        quantity=6, cumulative=10, price=120, commission=3,
    ))
    assert get_position(session) is None
    assert get_cash(session) == pytest.approx(8_999 + 438 + 717)


def test_sell_cannot_exceed_position_and_buy_cannot_overdraw_cash(projector, session):
    seed_intent(session, action="SELL")
    with pytest.raises(InvalidFillError, match="position"):
        projector.apply(make_fill(side="sell"))
    assert get_cash(session) == 10_000

    # A separate sleeve proves insufficient cash cannot create a negative book.
    intent = session.scalar(select(OrderIntent))
    intent.recommendation_id = "rec-sell-bad"
    session.add(OrderIntent(
        recommendation_id="rec-buy-bad",
        account_id="DU12345",
        mode="paper",
        portfolio="momentum",
        con_id=265598,
        symbol="AAPL",
        exchange="SMART",
        currency="USD",
        action="BUY",
        requested_quantity=200,
        limit_price=100,
        order_type="LMT",
        reserved_notional=20_000,
        filled_quantity=0,
        status=OrderStatus.SUBMITTED.value,
        ib_order_id="44",
        created_at=NOW,
        updated_at=NOW,
    ))
    session.commit()
    with pytest.raises(InvalidFillError, match="cash"):
        projector.apply(make_fill(
            "e-buy", recommendation_id="rec-buy-bad", order_id="44",
            quantity=200, cumulative=200,
        ))
    assert get_position(session) is None
    assert get_cash(session) == 10_000


def test_rounded_full_fill_terminalizes_and_releases_reservation(
    session, projector
):
    """A whole-share-rounded order (requested 8.3243, placed 8) that fully fills
    its placed quantity and is marked done by IB must terminalize FILLED — not
    stick at PARTIALLY_FILLED and leak the (requested-filled) reservation."""
    seed_intent(session, quantity=8.3243, status=OrderStatus.SUBMITTED)
    ledger = OrderLedger(session)

    assert projector.apply(make_fill(quantity=8, cumulative=8, order_done=True)) is True

    intent = ledger.get("rec-1")
    session.rollback()
    assert intent.status == OrderStatus.FILLED.value
    # FILLED is terminal, so no reservation remains to block future buys.
    assert ledger.active_reservations("momentum") == 0
    session.rollback()


def test_partial_without_order_done_stays_partial(session, projector):
    """A genuine partial (order still working, not done) is unchanged."""
    seed_intent(session, quantity=10, status=OrderStatus.SUBMITTED)
    ledger = OrderLedger(session)

    assert projector.apply(make_fill(quantity=6, cumulative=6, order_done=False)) is True

    intent = ledger.get("rec-1")
    session.rollback()
    assert intent.status == OrderStatus.PARTIALLY_FILLED.value


def test_material_partial_then_done_stays_partial_not_filled(session, projector):
    """order_done alone must NOT mark FILLED: a genuinely partial order that
    the broker marks done (e.g. cancelled after 40/100) must stay
    PARTIALLY_FILLED so the status path can terminalize it CANCELLED — only a
    sub-one-share (whole-share-rounding) shortfall may terminalize on done."""
    seed_intent(session, quantity=100, status=OrderStatus.SUBMITTED)
    ledger = OrderLedger(session)

    assert projector.apply(
        make_fill(quantity=40, cumulative=40, order_done=True)
    ) is True

    intent = ledger.get("rec-1")
    session.rollback()
    assert intent.status == OrderStatus.PARTIALLY_FILLED.value
    session.rollback()


def test_column_overflow_is_audited_and_raised_not_crashed(projector, session):
    """A DataError from sleeve accounting must behave like any unprojectable fill.

    KAN-61: ``trades.recommendation_id`` was varchar(50) while ids reached 60
    characters, so ``_apply_fill_accounting`` raised
    ``StringDataRightTruncation``. ``DataError`` was in neither the projector's
    nor the runner's except clause, so it escaped ``apply()`` and killed the
    process: an empty ``trades`` table and an empty DLQ at the same time, because
    the service died before it could quarantine anything.

    The contract this restores is the one the class docstring already promises:
    the immutable execution row survives as audit, and the error is raised only
    after that audit transaction commits.
    """
    seed_intent(session)
    fill = make_fill()

    def overflow(*args, **kwargs):
        # Write first, then fail. A real overflow raises from flush() with
        # accounting changes already pending in the savepoint, so the property
        # under test is that those get rolled back while the audit row outside
        # the savepoint still commits. Raising on entry would prove only that
        # the except clause names DataError.
        session.add(
            Position(
                account_id="DU12345",
                ticker="AAPL",
                portfolio="momentum",
                quantity=10,
                avg_entry_price=100,
                current_price=100,
                peak_price=100,
                highest_price_since_entry=100,
                opened_at=NOW,
                status="open",
            )
        )
        session.flush()
        raise DataError(
            "INSERT INTO trades (...) VALUES (...)",
            {},
            Exception("value too long for type character varying(50)"),
        )

    projector._paper_state._apply_fill_accounting = overflow

    with pytest.raises(FillProjectionError):
        projector.apply(fill)

    # The audit row is the whole point: it must outlive the failure.
    assert fill_count(session) == 1
    execution = session.scalar(select(ExecutionFill))
    assert execution.projection_applied is not True
    # And no sleeve state moved.
    assert get_position(session) is None
    assert get_cash(session) == 10_000


def test_an_infrastructure_error_still_escapes_rather_than_being_audited(
    projector, session
):
    """The narrow catch matters: OperationalError must NOT be treated as bad data.

    A DataError means this message can never be stored. An OperationalError
    means the database is unreachable, and quarantining a perfectly good fill
    because Postgres blinked would be silent data loss. It must propagate so the
    message stays pending and the container restarts.
    """
    seed_intent(session)
    fill = make_fill()

    def unreachable(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("server closed connection"))

    projector._paper_state._apply_fill_accounting = unreachable

    with pytest.raises(OperationalError):
        projector.apply(fill)
