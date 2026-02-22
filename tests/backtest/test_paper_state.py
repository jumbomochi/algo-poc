from __future__ import annotations

import os
import tempfile
from datetime import date

from scripts.paper_state import PaperTradingState


def test_initial_state_has_correct_structure():
    """New state should have empty portfolios with correct capital."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"momentum": 20_000, "mr": 14_000},
        state_path="/tmp/nonexistent.json",
    )
    assert state.portfolios["momentum"]["capital"] == 20_000
    assert state.portfolios["momentum"]["positions"] == {}
    assert state.portfolios["momentum"]["trades"] == []
    assert state.portfolios["mr"]["capital"] == 14_000


def test_save_and_load_round_trip():
    """State should survive save/load round-trip."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "state.json")

        state = PaperTradingState.create_new(
            portfolio_capitals={"momentum": 20_000},
            state_path=path,
        )
        state.record_fill(
            portfolio="momentum",
            ticker="AAPL",
            action="buy",
            quantity=10,
            price=150.0,
            fill_date=date(2024, 1, 15),
        )
        state.save()

        loaded = PaperTradingState.load(path)
        assert "AAPL" in loaded.portfolios["momentum"]["positions"]
        pos = loaded.portfolios["momentum"]["positions"]["AAPL"]
        assert pos["quantity"] == 10
        assert pos["entry_price"] == 150.0


def test_record_buy_creates_position():
    """Recording a buy fill should create or add to a position."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        state_path="/tmp/test.json",
    )
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 15))

    pos = state.portfolios["mr"]["positions"]["AAPL"]
    assert pos["quantity"] == 10
    assert pos["entry_price"] == 150.0
    assert pos["peak_price"] == 150.0


def test_record_sell_removes_position():
    """Recording a sell fill should remove the position and create a trade record."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        state_path="/tmp/test.json",
    )
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1))
    state.record_fill("mr", "AAPL", "sell", 10, 160.0, date(2024, 1, 15))

    assert "AAPL" not in state.portfolios["mr"]["positions"]
    assert len(state.portfolios["mr"]["trades"]) == 1
    trade = state.portfolios["mr"]["trades"][0]
    assert trade["ticker"] == "AAPL"
    assert trade["pnl"] == 100.0  # (160 - 150) * 10


def test_update_peak_prices():
    """update_peak_prices should update peak for held positions."""
    state = PaperTradingState.create_new(
        portfolio_capitals={"mr": 10_000},
        state_path="/tmp/test.json",
    )
    state.record_fill("mr", "AAPL", "buy", 10, 150.0, date(2024, 1, 1))

    current_prices = {"AAPL": 160.0}
    state.update_peak_prices("mr", current_prices)

    pos = state.portfolios["mr"]["positions"]["AAPL"]
    assert pos["peak_price"] == 160.0
