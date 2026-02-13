from __future__ import annotations

from datetime import date

import pytest

from backtest.metrics import BacktestMetrics


class TestTotalReturn:
    def test_total_return_positive(self):
        values = [100_000, 105_000, 110_000, 115_000, 120_000]
        result = BacktestMetrics.compute(portfolio_values=values, trades=[])
        assert result["total_return"] == pytest.approx(0.20)  # 20%

    def test_total_return_negative(self):
        values = [100_000, 95_000, 90_000]
        result = BacktestMetrics.compute(portfolio_values=values, trades=[])
        assert result["total_return"] == pytest.approx(-0.10)

    def test_total_return_flat(self):
        values = [100_000, 100_000, 100_000]
        result = BacktestMetrics.compute(portfolio_values=values, trades=[])
        assert result["total_return"] == pytest.approx(0.0)


class TestSharpeRatio:
    def test_sharpe_positive_for_winning(self):
        # Steadily increasing values -> positive sharpe
        values = [100_000 + i * 1_000 for i in range(252)]
        result = BacktestMetrics.compute(portfolio_values=values, trades=[])
        assert result["sharpe_ratio"] > 0

    def test_sharpe_near_zero_for_flat(self):
        values = [100_000] * 252
        result = BacktestMetrics.compute(portfolio_values=values, trades=[])
        assert result["sharpe_ratio"] == pytest.approx(0.0)


class TestMaxDrawdown:
    def test_max_drawdown_calculation(self):
        # Goes up to 120k, drops to 90k (25% drawdown), recovers
        values = [100_000, 110_000, 120_000, 100_000, 90_000, 95_000, 100_000]
        result = BacktestMetrics.compute(portfolio_values=values, trades=[])
        assert result["max_drawdown"] == pytest.approx(0.25)  # 30k/120k

    def test_max_drawdown_no_drawdown(self):
        values = [100_000, 110_000, 120_000, 130_000]
        result = BacktestMetrics.compute(portfolio_values=values, trades=[])
        assert result["max_drawdown"] == pytest.approx(0.0)


class TestWinRate:
    def test_win_rate_all_wins(self):
        trades = [
            {"pnl": 100.0, "entry_date": date(2025, 1, 1), "exit_date": date(2025, 1, 5)},
            {"pnl": 200.0, "entry_date": date(2025, 1, 6), "exit_date": date(2025, 1, 10)},
            {"pnl": 50.0, "entry_date": date(2025, 1, 11), "exit_date": date(2025, 1, 15)},
        ]
        result = BacktestMetrics.compute(portfolio_values=[100_000, 100_350], trades=trades)
        assert result["win_rate"] == pytest.approx(1.0)

    def test_win_rate_all_losses(self):
        trades = [
            {"pnl": -100.0, "entry_date": date(2025, 1, 1), "exit_date": date(2025, 1, 5)},
            {"pnl": -200.0, "entry_date": date(2025, 1, 6), "exit_date": date(2025, 1, 10)},
        ]
        result = BacktestMetrics.compute(portfolio_values=[100_000, 99_700], trades=trades)
        assert result["win_rate"] == pytest.approx(0.0)

    def test_win_rate_mixed(self):
        trades = [
            {"pnl": 100.0, "entry_date": date(2025, 1, 1), "exit_date": date(2025, 1, 5)},
            {"pnl": -50.0, "entry_date": date(2025, 1, 6), "exit_date": date(2025, 1, 10)},
        ]
        result = BacktestMetrics.compute(portfolio_values=[100_000, 100_050], trades=trades)
        assert result["win_rate"] == pytest.approx(0.5)


class TestAvgHoldingPeriod:
    def test_avg_holding_period(self):
        trades = [
            {"pnl": 100.0, "entry_date": date(2025, 1, 1), "exit_date": date(2025, 1, 6)},   # 5 days
            {"pnl": 200.0, "entry_date": date(2025, 1, 10), "exit_date": date(2025, 1, 20)},  # 10 days
        ]
        result = BacktestMetrics.compute(portfolio_values=[100_000, 100_300], trades=trades)
        assert result["avg_holding_period_days"] == pytest.approx(7.5)


class TestTotalTrades:
    def test_total_trades_count(self):
        trades = [
            {"pnl": 100.0, "entry_date": date(2025, 1, 1), "exit_date": date(2025, 1, 5)},
            {"pnl": -50.0, "entry_date": date(2025, 1, 6), "exit_date": date(2025, 1, 10)},
            {"pnl": 200.0, "entry_date": date(2025, 1, 11), "exit_date": date(2025, 1, 15)},
        ]
        result = BacktestMetrics.compute(portfolio_values=[100_000, 100_250], trades=trades)
        assert result["total_trades"] == 3


class TestEmptyTrades:
    def test_empty_trades_sensible_defaults(self):
        result = BacktestMetrics.compute(portfolio_values=[100_000, 100_000], trades=[])
        assert result["total_trades"] == 0
        assert result["win_rate"] == 0.0
        assert result["avg_holding_period_days"] == 0.0
        assert result["total_return"] == pytest.approx(0.0)


class TestBenchmarkReturn:
    def test_benchmark_values_optional(self):
        result = BacktestMetrics.compute(
            portfolio_values=[100_000, 110_000],
            trades=[],
            benchmark_values=None,
        )
        assert "benchmark_return" not in result or result["benchmark_return"] is None

    def test_benchmark_return_computed(self):
        result = BacktestMetrics.compute(
            portfolio_values=[100_000, 110_000],
            trades=[],
            benchmark_values=[100.0, 115.0],
        )
        assert result["benchmark_return"] == pytest.approx(0.15)
