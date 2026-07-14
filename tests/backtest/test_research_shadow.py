from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from backtest.runner import BacktestRunner
from backtest.simulator import SimulatedExecutor


class RecordingObserver:
    def __init__(self, raises: bool = False):
        self.calls: list[dict] = []
        self.raises = raises

    def observe(self, **kwargs):
        if self.raises:
            raise RuntimeError("observer unavailable")
        self.calls.append(kwargs)


def run_with(observer):
    bars = {
        "AAPL": [
            {
                "date": date(2026, 1, 2),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000,
            }
        ]
    }
    risk = MagicMock()
    risk.check_entry.return_value = MagicMock(
        approved=False, adjusted_quantity=0, reason="cap"
    )
    signal_fn = lambda ticker, history: {
        "action": "buy",
        "ticker": ticker,
        "limit_price": 100.0,
        "quantity": 1.0,
        "sector": "Technology",
    }
    runner = BacktestRunner(
        SimulatedExecutor(slippage_bps=0, commission_per_share=0), 10_000
    )
    return runner.run(
        bars,
        signal_fn,
        risk,
        candidate_observer=observer,
        portfolio_name="momentum",
    )


def test_backtest_observes_rejected_raw_buy_candidate():
    observer = RecordingObserver()

    result = run_with(observer)

    assert len(observer.calls) == 1
    assert observer.calls[0]["risk_approved"] is False
    assert observer.calls[0]["risk_reason"] == "cap"
    assert result.trades == []


def test_observer_failure_does_not_change_backtest_result():
    baseline = run_with(None)

    with_failure = run_with(RecordingObserver(raises=True))

    assert with_failure.trades == baseline.trades
    assert with_failure.portfolio_values == baseline.portfolio_values
