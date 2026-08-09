from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from backtest.runner import BacktestResult, BacktestRunner
from backtest.costs import CostModel
from backtest.simulator import SimulatedExecutor


def _make_bars(ticker: str, data: list[tuple]) -> list[dict]:
    """Helper to build bar dicts from (date, open, high, low, close) tuples."""
    return [
        {"date": d, "open": o, "high": h, "low": l, "close": c}
        for d, o, h, l, c in data
    ]


class TestBacktestResultDataclass:
    def test_backtest_result_fields(self):
        result = BacktestResult(
            trades=[{"pnl": 100}],
            portfolio_values=[100_000, 100_100],
            metrics={"total_return": 0.001},
        )
        assert len(result.trades) == 1
        assert len(result.portfolio_values) == 2
        assert result.metrics["total_return"] == 0.001


class TestSimpleBuySell:
    def test_simple_buy_sell_end_to_end(self):
        """Buy decided on day 1, hold, exit decided on day 3.

        Both orders fill on the session after the decision (next-open fills),
        so the round trip spans day 2 (entry) to day 4 (exit).
        """
        bars = _make_bars("AAPL", [
            (date(2025, 1, 6), 150.0, 155.0, 148.0, 153.0),
            (date(2025, 1, 7), 153.0, 158.0, 151.0, 156.0),
            (date(2025, 1, 8), 156.0, 160.0, 154.0, 159.0),
            (date(2025, 1, 9), 159.0, 162.0, 157.0, 161.0),
        ])
        bars_by_ticker = {"AAPL": bars}

        call_count = {"n": 0}

        def signals_fn(ticker: str, bars_so_far: list[dict]) -> dict | None:
            call_count["n"] += 1
            current_bar = bars_so_far[-1]
            if current_bar["date"] == date(2025, 1, 6):
                return {
                    "action": "buy",
                    "ticker": ticker,
                    "limit_price": 153.0,
                    "quantity": 100,
                    "sector": "Technology",
                }
            if current_bar["date"] == date(2025, 1, 8):
                return {
                    "action": "sell",
                    "ticker": ticker,
                }
            return None

        risk_engine = MagicMock()
        risk_engine.check_entry.return_value = MagicMock(
            approved=True, adjusted_quantity=100
        )

        executor = SimulatedExecutor(CostModel(slippage_bps=0, commission_per_share=0.0, commission_minimum=0.0))
        runner = BacktestRunner(
            executor=executor,
            initial_capital=100_000.0,
        )
        result = runner.run(
            bars_by_ticker=bars_by_ticker,
            signals_fn=signals_fn,
            risk_engine=risk_engine,
        )

        assert isinstance(result, BacktestResult)
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade["ticker"] == "AAPL"
        assert trade["entry_date"] == date(2025, 1, 7)
        assert trade["exit_date"] == date(2025, 1, 9)
        # Bought at day 2's open of 153, sold at day 4's open of 159.
        assert trade["pnl"] > 0


class TestNoSignals:
    def test_no_signals_produces_no_trades(self):
        bars = _make_bars("AAPL", [
            (date(2025, 1, 6), 150.0, 155.0, 148.0, 153.0),
            (date(2025, 1, 7), 153.0, 158.0, 151.0, 156.0),
        ])
        bars_by_ticker = {"AAPL": bars}

        def signals_fn(ticker: str, bars_so_far: list[dict]) -> dict | None:
            return None

        risk_engine = MagicMock()
        executor = SimulatedExecutor(CostModel(slippage_bps=0, commission_per_share=0.0, commission_minimum=0.0))
        runner = BacktestRunner(executor=executor, initial_capital=100_000.0)
        result = runner.run(
            bars_by_ticker=bars_by_ticker,
            signals_fn=signals_fn,
            risk_engine=risk_engine,
        )

        assert len(result.trades) == 0
        assert result.metrics["total_trades"] == 0
        # Portfolio should remain at initial capital
        assert result.portfolio_values[-1] == pytest.approx(100_000.0)


class TestRiskRejection:
    def test_risk_rejection_produces_no_trade(self):
        bars = _make_bars("AAPL", [
            (date(2025, 1, 6), 150.0, 155.0, 148.0, 153.0),
            (date(2025, 1, 7), 153.0, 158.0, 151.0, 156.0),
        ])
        bars_by_ticker = {"AAPL": bars}

        def signals_fn(ticker: str, bars_so_far: list[dict]) -> dict | None:
            return {
                "action": "buy",
                "ticker": ticker,
                "limit_price": 149.0,
                "quantity": 100,
                "sector": "Technology",
            }

        risk_engine = MagicMock()
        risk_engine.check_entry.return_value = MagicMock(
            approved=False, adjusted_quantity=0
        )

        executor = SimulatedExecutor(CostModel(slippage_bps=0, commission_per_share=0.0, commission_minimum=0.0))
        runner = BacktestRunner(executor=executor, initial_capital=100_000.0)
        result = runner.run(
            bars_by_ticker=bars_by_ticker,
            signals_fn=signals_fn,
            risk_engine=risk_engine,
        )

        assert len(result.trades) == 0
        assert result.portfolio_values[-1] == pytest.approx(100_000.0)


class TestMultipleTickers:
    def test_multiple_tickers_traded(self):
        bars_aapl = _make_bars("AAPL", [
            (date(2025, 1, 6), 150.0, 155.0, 148.0, 153.0),
            (date(2025, 1, 7), 153.0, 158.0, 151.0, 156.0),
            (date(2025, 1, 8), 156.0, 160.0, 154.0, 159.0),
            (date(2025, 1, 9), 159.0, 162.0, 157.0, 161.0),
        ])
        bars_msft = _make_bars("MSFT", [
            (date(2025, 1, 6), 400.0, 410.0, 395.0, 405.0),
            (date(2025, 1, 7), 405.0, 415.0, 400.0, 412.0),
            (date(2025, 1, 8), 412.0, 420.0, 408.0, 418.0),
            (date(2025, 1, 9), 418.0, 425.0, 414.0, 422.0),
        ])
        bars_by_ticker = {"AAPL": bars_aapl, "MSFT": bars_msft}

        def signals_fn(ticker: str, bars_so_far: list[dict]) -> dict | None:
            current_bar = bars_so_far[-1]
            if current_bar["date"] == date(2025, 1, 6):
                # Limits reachable during the *next* session's bar.
                limit = 152.0 if ticker == "AAPL" else 401.0
                return {
                    "action": "buy",
                    "ticker": ticker,
                    "limit_price": limit,
                    "quantity": 10,
                    "sector": "Technology",
                }
            if current_bar["date"] == date(2025, 1, 8):
                return {"action": "sell", "ticker": ticker}
            return None

        risk_engine = MagicMock()
        risk_engine.check_entry.return_value = MagicMock(
            approved=True, adjusted_quantity=10
        )

        executor = SimulatedExecutor(CostModel(slippage_bps=0, commission_per_share=0.0, commission_minimum=0.0))
        runner = BacktestRunner(executor=executor, initial_capital=100_000.0)
        result = runner.run(
            bars_by_ticker=bars_by_ticker,
            signals_fn=signals_fn,
            risk_engine=risk_engine,
        )

        assert len(result.trades) == 2
        tickers_traded = {t["ticker"] for t in result.trades}
        assert tickers_traded == {"AAPL", "MSFT"}
