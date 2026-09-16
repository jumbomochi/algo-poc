"""Restoring an exit that was written off instead of recorded — KAN-85 slice 1.

On 2026-08-28 and 2026-09-14 two positions that IB had really SOLD were
repaired with ``set_position_quantity -> 0``. That action writes no trade, no
realised P&L and no cash movement (``reconcile_paper.py::_apply_action``), so
the book lost **-5,719.51** of value and two completed round trips vanished
from ``trades``:

    PANW  con_id 110619459  sector_rotation  written off 2026-08-28  -3,476.99
    LLY   con_id 9160       sector_rotation  written off 2026-09-14  -2,242.52

This module tests the tool that puts them back — and, more importantly, the
much larger set of cases where it must REFUSE. Reconstructing a trade from a
statement weeks later produces evidence that looks observed but is not, so
every refusal here exists to stop the tool inventing a number.

THE RESIDUE GUARD. If the sale does not account for the whole holding, the
remainder is left OPEN and reconciliation flags it as ``missing_in_ib`` on the
next run, re-blocking every buy in all six sleeves. Neither real case needs it
— PANW and LLY both bought whole shares (9 and 2) and their sell intents match
exactly — but a fractional holding is reachable through other paths, so the
guard refuses rather than absorbing, and names the shortfall.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from scripts.ops.restore_missed_exit import (
    RestoreRefusedError,
    apply_restore,
    plan_restore,
)
from shared.models import Base
from shared.models.order_ledger import (
    ExecutionFill,
    OrderIntent,
    OrderStatus,
)
from shared.models.portfolio import Position, Trade
from shared.models.portfolio_config import PortfolioConfig

ACCOUNT = "DUN551088"
CON_ID = 110619459
PORTFOLIO = "sector_rotation"
TICKER = "PANW"
HELD = 9.0          # PANW's real entry fill: 9 whole shares
ENTRY = 314.15
EXIT_PRICE = 382.85
OPENED = datetime(2026, 7, 30, 13, 31, tzinfo=timezone.utc)
SOLD_AT = datetime(2026, 8, 20, 13, 30, tzinfo=timezone.utc)
WRITTEN_OFF = datetime(2026, 8, 28, 5, 52, tzinfo=timezone.utc)


@pytest.fixture()
def session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'restore.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _portfolio(session, cash=5000.0):
    """The sleeve's cash book. _apply_fill_accounting refuses an unknown
    portfolio, and the restored proceeds land here."""
    now = datetime.now(timezone.utc)
    session.add(PortfolioConfig(
        portfolio=PORTFOLIO, capital=20000.0, cash=cash, currency="USD",
        created_at=now, updated_at=now,
    ))
    session.commit()


def _written_off_position(session, *, quantity=0.0, status="closed", held=HELD):
    """The row as ``set_position_quantity -> 0`` left it."""
    _portfolio(session)
    pos = Position(
        ticker=TICKER, portfolio=PORTFOLIO, quantity=quantity,
        avg_entry_price=ENTRY, current_price=EXIT_PRICE,
        peak_price=EXIT_PRICE, highest_price_since_entry=EXIT_PRICE,
        opened_at=OPENED, closed_at=WRITTEN_OFF if status == "closed" else None,
        status=status, con_id=CON_ID, exchange="SMART", currency="USD",
        account_id=ACCOUNT,
    )
    session.add(pos)
    session.commit()
    return pos


def _buy_fill(session):
    """The entry fill. Its presence is what proves this was a real position
    rather than a phantom — the distinction KAN-85 is named for."""
    session.add(ExecutionFill(
        account_id=ACCOUNT, execution_id="0000dc8f.6b2d21b5.01.01",
        ib_order_id="20", con_id=CON_ID, symbol=TICKER, exchange="SMART",
        currency="USD", side="BUY", quantity=HELD, price=ENTRY,
        commission=1.0, executed_at=OPENED, projection_applied=True,
    ))
    session.commit()


def _sell_intent(session, *, status=OrderStatus.EXPIRED.value, quantity=9.0):
    intent = OrderIntent(
        recommendation_id="sleeve-2026-08-19-DUN551088-paper-sector_rotation-PANW-sell",
        account_id=ACCOUNT, mode="paper", portfolio=PORTFOLIO, con_id=CON_ID,
        symbol=TICKER, exchange="SMART", currency="USD", action="SELL",
        requested_quantity=quantity, order_type="MKT", status=status,
        ib_order_id="130", created_at=SOLD_AT - timedelta(days=1),
        updated_at=WRITTEN_OFF,
    )
    session.add(intent)
    session.commit()
    return intent


def _plan(session, **over):
    kwargs = dict(
        con_id=CON_ID, portfolio=PORTFOLIO, held_quantity=HELD,
        sold_quantity=HELD, price=EXIT_PRICE, executed_at=SOLD_AT,
        execution_id="statement-panw-20260820", commission=1.05,
    )
    kwargs.update(over)
    return plan_restore(session, **kwargs)


# ---------------------------------------------------------------------------
# The residue trap — the refusal that matters most
# ---------------------------------------------------------------------------


def test_a_sell_smaller_than_the_holding_is_refused(session):
    """A partial sale leaves the remainder OPEN, and reconciliation flags that
    as missing_in_ib on the next run — re-blocking every buy in all six
    sleeves. Refuse rather than absorb."""
    _written_off_position(session)
    _buy_fill(session)
    _sell_intent(session)

    with pytest.raises(RestoreRefusedError, match="residue|remainder"):
        _plan(session, sold_quantity=8.0)


def test_the_residue_refusal_names_the_amount_and_what_is_needed(session):
    """A refusal the operator cannot act on just moves the problem."""
    _written_off_position(session)
    _buy_fill(session)
    _sell_intent(session)

    with pytest.raises(RestoreRefusedError) as exc:
        _plan(session, sold_quantity=8.0)
    message = str(exc.value)
    assert "1.0000" in message
    assert "statement" in message.lower()


def test_a_sell_larger_than_the_holding_is_refused(session):
    _written_off_position(session)
    _buy_fill(session)
    _sell_intent(session)

    with pytest.raises(RestoreRefusedError):
        _plan(session, sold_quantity=HELD + 1.0)


# ---------------------------------------------------------------------------
# Missed exit versus phantom — the distinction KAN-85 exists for
# ---------------------------------------------------------------------------


def test_a_position_with_no_entry_fill_is_refused_as_a_phantom(session):
    """No fill history means the position may never have existed. Writing a
    sell for it would invent a round trip, which is worse than the hole."""
    _written_off_position(session)
    _sell_intent(session)

    with pytest.raises(RestoreRefusedError, match="phantom|no entry fill"):
        _plan(session)


def test_a_real_position_with_an_entry_fill_is_planned(session):
    _written_off_position(session)
    _buy_fill(session)
    _sell_intent(session)

    plan = _plan(session)
    assert plan.ticker == TICKER
    assert plan.held_quantity == pytest.approx(HELD)
    assert plan.sold_quantity == pytest.approx(HELD)
    assert plan.estimated_pnl == pytest.approx((EXIT_PRICE - ENTRY) * HELD)


# ---------------------------------------------------------------------------
# Never guess a price
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("price", [0.0, -1.0])
def test_a_missing_or_negative_price_is_refused(session, price):
    """A wrong price is a wrong P&L forever. The statement supplies it or the
    repair does not happen."""
    _written_off_position(session)
    _buy_fill(session)
    _sell_intent(session)

    with pytest.raises(RestoreRefusedError, match="price"):
        _plan(session, price=price)


def test_the_entry_price_is_never_used_as_a_default_exit(session):
    """Defaulting to entry would book a zero P&L and look plausible."""
    _written_off_position(session)
    _buy_fill(session)
    _sell_intent(session)

    plan = _plan(session, price=EXIT_PRICE)
    assert plan.price != ENTRY


# ---------------------------------------------------------------------------
# Target selection and account safety
# ---------------------------------------------------------------------------


def test_an_open_position_is_refused(session):
    """An open position is reconciliation's job, not this tool's. Applying
    here would double-sell."""
    _written_off_position(session, quantity=HELD, status="open")
    _buy_fill(session)

    with pytest.raises(RestoreRefusedError, match="closed|written off"):
        _plan(session)


def test_a_live_account_is_refused(session):
    """Same DU guard the repair tool carries. A live account's ledger is never
    reconstructed by a script."""
    pos = _written_off_position(session)
    pos.account_id = "U1234567"
    session.commit()
    _buy_fill(session)

    with pytest.raises(RestoreRefusedError, match="paper|DU"):
        _plan(session)


def test_an_unknown_position_is_refused(session):
    with pytest.raises(RestoreRefusedError, match="exactly one|no position"):
        _plan(session, con_id=999999)


# ---------------------------------------------------------------------------
# Idempotence — never record the same exit twice
# ---------------------------------------------------------------------------


def test_an_already_recorded_execution_is_refused(session):
    _written_off_position(session)
    _buy_fill(session)
    _sell_intent(session)
    session.add(ExecutionFill(
        account_id=ACCOUNT, execution_id="statement-panw-20260820",
        ib_order_id="130", con_id=CON_ID, symbol=TICKER, exchange="SMART",
        currency="USD", side="SELL", quantity=HELD, price=EXIT_PRICE,
        commission=1.05, executed_at=SOLD_AT, projection_applied=True,
    ))
    session.commit()

    with pytest.raises(RestoreRefusedError, match="already"):
        _plan(session)


def test_an_existing_sell_trade_is_refused(session):
    """Belt and braces: a trade with no matching fill still means the exit is
    on the record, and a second one would double-count the P&L."""
    _written_off_position(session)
    _buy_fill(session)
    _sell_intent(session)
    session.add(Trade(
        ticker=TICKER, portfolio=PORTFOLIO, side="sell", quantity=HELD,
        price=EXIT_PRICE, entry_price=ENTRY, entry_date=OPENED.date(),
        pnl=1.0, executed_at=SOLD_AT,
    ))
    session.commit()

    with pytest.raises(RestoreRefusedError, match="already|trade"):
        _plan(session)


# ---------------------------------------------------------------------------
# Applying it: the end state must be exactly right, or nothing
# ---------------------------------------------------------------------------


def test_apply_requires_the_exact_confirmation(session):
    _written_off_position(session)
    _buy_fill(session)
    _sell_intent(session)
    plan = _plan(session)

    with pytest.raises(RestoreRefusedError, match="confirm"):
        apply_restore(session, plan, confirm="yes")


def test_apply_writes_the_trade_the_write_off_deleted(session):
    _written_off_position(session)
    _buy_fill(session)
    _sell_intent(session)
    plan = _plan(session)

    apply_restore(session, plan, confirm="RESTORE MISSED EXIT")

    trade = session.scalar(select(Trade).where(Trade.ticker == TICKER))
    assert trade is not None, "the whole point: trades must carry the exit"
    assert trade.side == "sell"
    assert trade.quantity == pytest.approx(HELD)
    assert trade.price == pytest.approx(EXIT_PRICE)
    assert trade.entry_price == pytest.approx(ENTRY)
    assert trade.pnl == pytest.approx((EXIT_PRICE - ENTRY) * HELD)


def test_apply_removes_the_position_from_the_open_book(session):
    """paper_state.py:296 DELETES a fully-sold position rather than closing it,
    so gone is the expected end state. What must never happen is it being left
    OPEN — reconciliation would read that as a new phantom and fail-close."""
    _written_off_position(session)
    _buy_fill(session)
    _sell_intent(session)
    plan = _plan(session)

    apply_restore(session, plan, confirm="RESTORE MISSED EXIT")

    pos = session.scalar(select(Position).where(Position.con_id == CON_ID))
    assert pos is None or pos.status != "open"


def test_apply_records_the_exit_as_a_reconstruction(session):
    """Evidence that was reconstructed must never be indistinguishable from
    evidence that was observed (KAN-85 AC4)."""
    _written_off_position(session)
    _buy_fill(session)
    _sell_intent(session)
    plan = _plan(session)

    apply_restore(session, plan, confirm="RESTORE MISSED EXIT")

    trade = session.scalar(select(Trade).where(Trade.ticker == TICKER))
    assert trade.exit_reason and "repair" in trade.exit_reason.lower()


def test_apply_corrects_an_expired_intent(session):
    """The projector will NOT do this: _advance_intent returns early on a
    terminal status, so an EXPIRED order stays EXPIRED even once its fill is
    recorded. UNH only advanced because its intent was still SUBMITTED."""
    _written_off_position(session)
    _buy_fill(session)
    intent = _sell_intent(session, status=OrderStatus.EXPIRED.value)
    plan = _plan(session)

    apply_restore(session, plan, confirm="RESTORE MISSED EXIT")

    session.refresh(intent)
    assert intent.status == OrderStatus.FILLED.value
    assert intent.filled_quantity == pytest.approx(HELD)


def test_apply_writes_the_execution_fill(session):
    _written_off_position(session)
    _buy_fill(session)
    _sell_intent(session)
    plan = _plan(session)

    apply_restore(session, plan, confirm="RESTORE MISSED EXIT")

    fill = session.scalar(
        select(ExecutionFill).where(
            ExecutionFill.execution_id == "statement-panw-20260820"
        )
    )
    assert fill is not None
    assert fill.side.lower() == "sell"
    assert fill.projection_applied is True


def test_a_failed_apply_leaves_the_position_written_off(session):
    """Compensation. The reopen and the projection are separate transactions
    because FillProjector owns its own; a failure between them must not leave
    a reopened position, which reconciliation would flag as a NEW phantom."""
    _written_off_position(session)
    _buy_fill(session)
    _sell_intent(session)
    plan = _plan(session)

    # A price the accounting path will reject at projection time.
    broken = plan.__class__(**{**plan.__dict__, "price": float("nan")})
    with pytest.raises(Exception):
        apply_restore(session, broken, confirm="RESTORE MISSED EXIT")

    pos = session.scalar(select(Position).where(Position.con_id == CON_ID))
    assert pos is not None, "a failed restore must not delete the position"
    assert pos.status == "closed", "a failed restore must not leave it open"
    assert pos.quantity == pytest.approx(0.0)
