from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from backtest.runner import BacktestRunner, BacktestResult
from backtest.simulator import SimulatedExecutor


def test_runner_supports_multiple_lots_per_ticker():
    """Runner should accept multiple buy signals for the same ticker."""
    executor = SimulatedExecutor(slippage_bps=0, commission_per_share=0)
    runner = BacktestRunner(executor=executor, initial_capital=100_000)

    # 10 days of data, price goes 100 -> 102 -> 105 -> 103 -> 108 -> 105 -> 110 -> 112 -> 115 -> 120
    bars = {
        "TEST": [
            {"date": date(2024, 1, d), "open": p, "high": p + 1, "low": p - 1, "close": p, "volume": 1000}
            for d, p in [
                (2, 100), (3, 102), (4, 105), (5, 103), (6, 108),
                (7, 105), (8, 110), (9, 112), (10, 115), (11, 120),
            ]
        ],
    }

    call_count = {"buy": 0}

    def signals_fn(ticker, bars_so_far):
        if len(bars_so_far) < 2:
            return None
        price = bars_so_far[-1]["close"]
        # Buy on day 2 (price 102) and day 6 (price 105)
        if len(bars_so_far) == 2:
            call_count["buy"] += 1
            return {"action": "buy", "ticker": ticker, "limit_price": price + 1,
                    "quantity": 10, "sector": "Test", "signals": {}}
        if len(bars_so_far) == 6:
            call_count["buy"] += 1
            return {"action": "buy", "ticker": ticker, "limit_price": price + 1,
                    "quantity": 10, "sector": "Test", "signals": {}}
        # Sell all on day 10 (price 120)
        if len(bars_so_far) == 10:
            return {"action": "sell", "ticker": ticker, "limit_price": price,
                    "quantity": 0, "sector": "Test", "exit_reason": "trailing_stop",
                    "lot_index": "all"}
        return None

    @dataclass
    class AlwaysApprove:
        approved: bool = True
        adjusted_quantity: int = 0
        reason: str = "ok"

    class MockRisk:
        def check_entry(self, ticker, quantity, price, sector, portfolio, existing_lots=0):
            return AlwaysApprove(adjusted_quantity=quantity)

    result = runner.run(bars, signals_fn, MockRisk())

    assert call_count["buy"] == 2
    # Both lots should produce individual trade records on exit
    assert len(result.trades) == 2, (
        f"Expected 2 trade records (one per lot), got {len(result.trades)}"
    )
    assert result.metrics["total_trades"] == 2
