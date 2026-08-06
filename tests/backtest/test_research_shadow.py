from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from unittest.mock import MagicMock

from backtest.runner import BacktestResult, BacktestRunner
from backtest.costs import CostModel
from backtest.simulator import SimulatedExecutor


class RecordingObserver:
    def __init__(self, raises: bool = False):
        self.calls: list[dict] = []
        self.raises = raises

    def observe(self, **kwargs):
        if self.raises:
            raise RuntimeError("observer unavailable")
        self.calls.append(kwargs)


class ExportFailingObserver:
    def observe(self, **kwargs):
        pass

    @property
    def records(self):
        raise RuntimeError("export unavailable")


class MutatingObserver:
    def observe(self, **kwargs):
        kwargs["signal"]["signals"]["momentum"]["score"] = -1.0


class Uncopyable:
    def __deepcopy__(self, memo):
        raise RuntimeError("snapshot unavailable")


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
    def signal_fn(ticker, history):
        return {
            "action": "buy",
            "ticker": ticker,
            "limit_price": 100.0,
            "quantity": 1.0,
            "sector": "Technology",
        }
    runner = BacktestRunner(
        SimulatedExecutor(CostModel(slippage_bps=0, commission_per_share=0, commission_minimum=0.0)), 10_000
    )
    return runner.run(
        bars,
        signal_fn,
        risk,
        candidate_observer=observer,
        portfolio_name="momentum",
    )


def run_approved_with(observer):
    bars = {
        "AAPL": [
            {
                "date": date(2026, 1, 2),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
            {
                "date": date(2026, 1, 3),
                "open": 102,
                "high": 103,
                "low": 99,
                "close": 102,
                "volume": 1000,
            },
            {
                "date": date(2026, 1, 4),
                "open": 104,
                "high": 105,
                "low": 103,
                "close": 104,
                "volume": 1000,
            },
        ]
    }
    risk = MagicMock()
    risk.check_entry.return_value = MagicMock(
        approved=True, adjusted_quantity=1.0, reason="approved"
    )

    # Entry decided on 1/2 fills at 1/3's low-touched limit; the exit decided
    # on 1/3 fills at 1/4's open.
    def signal_fn(ticker, history):
        if len(history) == 1:
            return {
                "action": "buy",
                "ticker": ticker,
                "limit_price": 100.0,
                "quantity": 1.0,
                "sector": "Technology",
                "signals": {"momentum": {"score": 0.8}},
            }
        return {"action": "sell", "ticker": ticker, "exit_reason": "test"}

    runner = BacktestRunner(
        SimulatedExecutor(CostModel(slippage_bps=0, commission_per_share=0, commission_minimum=0.0)), 10_000
    )
    return runner.run(
        bars,
        signal_fn,
        risk,
        candidate_observer=observer,
        portfolio_name="momentum",
    )


def established_result(result):
    """Return every established backtest output, excluding shadow evidence."""
    established = asdict(result)
    established.pop("shadow_candidates")
    return established


@dataclass
class ExtendedBacktestResult(BacktestResult):
    future_established_field: str = ""


def test_established_result_parity_includes_future_dataclass_fields():
    baseline = ExtendedBacktestResult(future_established_field="baseline")
    changed = ExtendedBacktestResult(future_established_field="changed")

    assert established_result(baseline) != established_result(changed)


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

    assert established_result(with_failure) == established_result(baseline)


def test_observer_export_failure_does_not_change_backtest_result():
    baseline = run_with(None)

    with_failure = run_with(ExportFailingObserver())

    assert established_result(with_failure) == established_result(baseline)
    assert with_failure.shadow_candidates == []


def test_mutating_observer_cannot_change_established_backtest_result_fields():
    baseline = run_approved_with(None)

    with_observer = run_approved_with(MutatingObserver())

    assert established_result(with_observer) == established_result(baseline)
    assert with_observer.trades[0]["entry_signals"] == {
        "momentum": {"score": 0.8}
    }


def test_signal_snapshot_failure_does_not_change_backtest_result():
    observer = RecordingObserver()
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

    def signal_fn(ticker, history):
        return {
            "action": "buy",
            "ticker": ticker,
            "limit_price": 100.0,
            "quantity": 1.0,
            "sector": "Technology",
            "signals": {"uncopyable": Uncopyable()},
        }

    runner = BacktestRunner(
        SimulatedExecutor(CostModel(slippage_bps=0, commission_per_share=0, commission_minimum=0.0)), 10_000
    )
    result = runner.run(
        bars,
        signal_fn,
        risk,
        candidate_observer=observer,
        portfolio_name="momentum",
    )

    assert result.trades == []
    assert result.portfolio_values == [10_000, 10_000]
    assert observer.calls == []
