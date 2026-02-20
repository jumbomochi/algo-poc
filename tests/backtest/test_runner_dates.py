from __future__ import annotations

from datetime import date

from backtest.runner import BacktestRunner, BacktestResult
from backtest.simulator import SimulatedExecutor


def test_backtest_result_includes_dates():
    """BacktestResult.dates should have one entry per trading day."""
    executor = SimulatedExecutor(slippage_bps=10, commission_per_share=0.005)
    runner = BacktestRunner(executor=executor, initial_capital=100_000)

    bars_by_ticker = {
        "TEST": [
            {"date": date(2024, 1, 2), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000},
            {"date": date(2024, 1, 3), "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
            {"date": date(2024, 1, 4), "open": 101, "high": 103, "low": 100, "close": 102, "volume": 1000},
        ],
    }

    def no_signals(ticker, bars):
        return None

    class NoOpRisk:
        def check_entry(self, *args):
            pass

    result = runner.run(bars_by_ticker, no_signals, NoOpRisk())

    # portfolio_values has initial + one per date = 4 entries
    assert len(result.portfolio_values) == 4
    # dates has one per trading date = 3 entries
    assert len(result.dates) == 3
    assert result.dates[0] == date(2024, 1, 2)
    assert result.dates[-1] == date(2024, 1, 4)
