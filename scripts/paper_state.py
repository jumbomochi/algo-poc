#!/usr/bin/env python3
"""Paper trading state persistence.

Manages position tracking, trade history, and equity across
multiple portfolios. State is persisted to a JSON file between
daily runs.
"""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Any


class PaperTradingState:
    """Manages paper trading state for multiple portfolios.

    Each portfolio tracks:
    - capital: initial capital allocation
    - cash: current cash available
    - positions: {ticker: {quantity, entry_price, entry_date, peak_price}}
    - trades: list of completed trades with P&L
    - equity_history: list of {date, equity} snapshots
    """

    def __init__(self, data: dict[str, Any], state_path: str) -> None:
        self._data = data
        self._state_path = state_path

    @property
    def portfolios(self) -> dict[str, Any]:
        return self._data["portfolios"]

    @classmethod
    def create_new(
        cls,
        portfolio_capitals: dict[str, float],
        state_path: str,
    ) -> PaperTradingState:
        """Create a fresh state with initial capital per portfolio."""
        data: dict[str, Any] = {"portfolios": {}}
        for name, capital in portfolio_capitals.items():
            data["portfolios"][name] = {
                "capital": capital,
                "cash": capital,
                "positions": {},
                "trades": [],
                "equity_history": [],
            }
        return cls(data, state_path)

    @classmethod
    def load(cls, state_path: str) -> PaperTradingState:
        """Load state from JSON file."""
        with open(state_path) as f:
            data = json.load(f)
        return cls(data, state_path)

    def save(self) -> None:
        """Save state to JSON file."""
        os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
        with open(self._state_path, "w") as f:
            json.dump(self._data, f, indent=2, default=str)

    def record_fill(
        self,
        portfolio: str,
        ticker: str,
        action: str,
        quantity: int,
        price: float,
        fill_date: date,
    ) -> None:
        """Record a fill (buy or sell) for a portfolio."""
        pf = self.portfolios[portfolio]

        if action == "buy":
            if ticker in pf["positions"]:
                # Average into existing position
                pos = pf["positions"][ticker]
                old_qty = pos["quantity"]
                old_price = pos["entry_price"]
                new_qty = old_qty + quantity
                pos["entry_price"] = (old_price * old_qty + price * quantity) / new_qty
                pos["quantity"] = new_qty
                pos["peak_price"] = max(pos["peak_price"], price)
            else:
                pf["positions"][ticker] = {
                    "quantity": quantity,
                    "entry_price": price,
                    "entry_date": str(fill_date),
                    "peak_price": price,
                }
            pf["cash"] -= price * quantity

        elif action == "sell":
            pos = pf["positions"].get(ticker)
            if pos:
                pnl = (price - pos["entry_price"]) * quantity
                pf["trades"].append({
                    "ticker": ticker,
                    "entry_price": pos["entry_price"],
                    "exit_price": price,
                    "quantity": quantity,
                    "entry_date": pos.get("entry_date", ""),
                    "exit_date": str(fill_date),
                    "pnl": pnl,
                    "portfolio": portfolio,
                })
                pf["cash"] += price * quantity
                del pf["positions"][ticker]

    def update_peak_prices(
        self, portfolio: str, current_prices: dict[str, float]
    ) -> None:
        """Update peak prices for all held positions."""
        for ticker, pos in self.portfolios[portfolio]["positions"].items():
            if ticker in current_prices:
                pos["peak_price"] = max(pos["peak_price"], current_prices[ticker])

    def compute_equity(
        self, portfolio: str, current_prices: dict[str, float]
    ) -> float:
        """Compute current equity (cash + market value of positions)."""
        pf = self.portfolios[portfolio]
        market_value = sum(
            pos["quantity"] * current_prices.get(ticker, pos["entry_price"])
            for ticker, pos in pf["positions"].items()
        )
        return pf["cash"] + market_value
