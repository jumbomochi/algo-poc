"""Tests for shared/liquidation.py — identity-scoped liquidation targets.

First coverage of ``load_liquidation_targets``. The behavior under test is the
one the six-sleeve portfolio makes routine: two sleeves holding the same ticker
must produce two rows, each carrying its own broker identity, so one sleeve's
exit is never booked against another's.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shared.liquidation import (
    exit_intent_id,
    liquidation_exit_id,
    load_liquidation_targets,
)
from shared.models.base import Base
from shared.models.portfolio import Position

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def add_position(
    session,
    ticker="AAPL",
    portfolio="momentum",
    account_id="DUN551088",
    con_id=265598,
    qty=10.0,
    exchange="SMART",
    currency="USD",
    status="open",
):
    session.add(Position(
        ticker=ticker, portfolio=portfolio, account_id=account_id,
        con_id=con_id, exchange=exchange, currency=currency, quantity=qty,
        avg_entry_price=100.0, current_price=110.0, peak_price=115.0,
        highest_price_since_entry=115.0, sector="Tech",
        opened_at=NOW, status=status,
    ))
    session.flush()


def by_scope(rows):
    return {(r["account_id"], r["portfolio"], r["con_id"]): r for r in rows}


class TestLoadLiquidationTargets:
    def test_two_portfolios_one_ticker_produce_two_truthful_rows(self, session):
        """AC1: not one summed row carrying the first sleeve's identity."""
        add_position(session, portfolio="momentum", qty=10.0)
        add_position(session, portfolio="quality_value", qty=30.0)

        rows = by_scope(load_liquidation_targets(session))

        assert len(rows) == 2
        assert rows[("DUN551088", "momentum", 265598)]["quantity"] == 10.0
        assert rows[("DUN551088", "quality_value", 265598)]["quantity"] == 30.0
        assert all(r["ticker"] == "AAPL" for r in rows.values())

    def test_two_accounts_one_ticker_produce_two_rows(self, session):
        """AC2."""
        add_position(session, account_id="DUN551088", qty=10.0)
        add_position(session, account_id="U1234567", qty=7.0)

        rows = by_scope(load_liquidation_targets(session))

        assert len(rows) == 2
        assert rows[("DUN551088", "momentum", 265598)]["quantity"] == 10.0
        assert rows[("U1234567", "momentum", 265598)]["quantity"] == 7.0

    def test_lots_within_one_scope_still_aggregate(self, session):
        """AC3: intra-scope aggregation is the intended behavior, preserved."""
        add_position(session, qty=10.0)
        add_position(session, qty=5.0)
        add_position(session, qty=2.5)

        rows = load_liquidation_targets(session)

        assert len(rows) == 1
        assert rows[0]["quantity"] == 17.5
        assert rows[0]["portfolio"] == "momentum"

    def test_distinct_con_ids_in_one_sleeve_are_separate_rows(self, session):
        add_position(session, ticker="AAPL", con_id=265598, qty=10.0)
        add_position(session, ticker="MSFT", con_id=272093, qty=4.0)

        rows = by_scope(load_liquidation_targets(session))

        assert len(rows) == 2
        assert rows[("DUN551088", "momentum", 265598)]["ticker"] == "AAPL"
        assert rows[("DUN551088", "momentum", 272093)]["ticker"] == "MSFT"

    def test_null_con_id_position_is_returned_not_dropped(self, session):
        """AC4: the emitter's missing-con_id guard is what must alert on it —
        this loader must not silently swallow an unroutable position."""
        add_position(session, con_id=None, qty=3.0)

        rows = load_liquidation_targets(session)

        assert len(rows) == 1
        assert rows[0]["con_id"] is None
        assert rows[0]["quantity"] == 3.0

    def test_null_con_id_does_not_collapse_with_a_routable_scope(self, session):
        add_position(session, portfolio="momentum", con_id=265598, qty=10.0)
        add_position(session, portfolio="momentum", con_id=None, qty=3.0)

        rows = by_scope(load_liquidation_targets(session))

        assert len(rows) == 2
        assert rows[("DUN551088", "momentum", None)]["quantity"] == 3.0
        assert rows[("DUN551088", "momentum", 265598)]["quantity"] == 10.0

    def test_two_null_con_id_tickers_in_one_sleeve_stay_separate(self, session):
        """Two unroutable positions must not collapse into one row — the
        emitter would then alert on one ticker and lose the other entirely."""
        add_position(session, ticker="AAPL", con_id=None, qty=3.0)
        add_position(session, ticker="MSFT", con_id=None, qty=8.0)

        rows = {r["ticker"]: r for r in load_liquidation_targets(session)}

        assert len(rows) == 2
        assert rows["AAPL"]["quantity"] == 3.0
        assert rows["MSFT"]["quantity"] == 8.0

    def test_account_scoping_still_filters(self, session):
        """AC5."""
        add_position(session, account_id="DUN551088", qty=10.0)
        add_position(session, account_id="U1234567", qty=7.0)

        rows = load_liquidation_targets(session, account_id="U1234567")

        assert len(rows) == 1
        assert rows[0]["account_id"] == "U1234567"
        assert rows[0]["quantity"] == 7.0

    def test_closed_positions_excluded(self, session):
        add_position(session, qty=10.0, status="closed")

        assert load_liquidation_targets(session) == []

    def test_row_shape_is_unchanged(self, session):
        """Callers depend on these seven keys; only the row count changes."""
        add_position(session)

        assert set(load_liquidation_targets(session)[0]) == {
            "ticker", "quantity", "con_id", "account_id",
            "exchange", "currency", "portfolio",
        }


class TestExitIntentId:
    def test_format(self):
        """AC6."""
        assert exit_intent_id(
            "stop-loss", "DUN551088", "momentum", 265598, date(2026, 8, 15), 0
        ) == "stop-loss-DUN551088-momentum-265598-2026-08-15-0"

    def test_two_portfolios_same_ticker_and_date_yield_distinct_ids(self):
        """AC6: the collision that made one sleeve suppress the other."""
        args = (265598, date(2026, 8, 15), 0)
        assert exit_intent_id("stop-loss", "DUN551088", "momentum", *args) != \
            exit_intent_id("stop-loss", "DUN551088", "quality_value", *args)

    def test_seq_distinguishes_repeat_exits_on_the_same_day(self):
        base = ("stop-loss", "DUN551088", "momentum", 265598, date(2026, 8, 15))
        assert exit_intent_id(*base, 0) != exit_intent_id(*base, 1)

    def test_kind_is_part_of_the_id(self):
        base = ("DUN551088", "momentum", 265598, date(2026, 8, 15), 0)
        assert exit_intent_id("stop-loss", *base) != \
            exit_intent_id("passive-trim", *base)

    def test_is_deterministic(self):
        args = ("stop-loss", "DUN551088", "momentum", 265598, date(2026, 8, 15), 0)
        assert exit_intent_id(*args) == exit_intent_id(*args)


class TestLiquidationExitId:
    def test_unchanged_epoch_scheme(self):
        """AC7: pinned so a future refactor cannot quietly merge the two
        schemes. A kill is one event across the book and converges on one id
        per ticker; a stop-loss recurs and needs a sequence instead."""
        assert liquidation_exit_id("paper", "AAPL", 1755216000) == \
            "liq-paper-AAPL-1755216000"

    def test_distinct_from_exit_intent_id(self):
        assert not liquidation_exit_id("paper", "AAPL", 1755216000).startswith(
            "stop-loss"
        )
