from __future__ import annotations

from datetime import date

from backtest.runner import BacktestResult
from scripts.run_backtest import (
    PortfolioConfig,
    compute_aggregate_metrics,
)
from services.risk_management.engine import RiskEngine


def _make_risk_engine() -> RiskEngine:
    return RiskEngine(position_entry_limit_pct=12.0)


def _make_result(
    portfolio_values: list[float],
    trades: list[dict] | None = None,
    dates: list | None = None,
) -> BacktestResult:
    if trades is None:
        trades = []
    if dates is None:
        dates = [date(2024, 1, i + 1) for i in range(len(portfolio_values) - 1)]
    from backtest.metrics import BacktestMetrics
    metrics = BacktestMetrics.compute(portfolio_values, trades)
    return BacktestResult(
        trades=trades,
        portfolio_values=portfolio_values,
        dates=dates,
        metrics=metrics,
    )


def test_aggregate_single_portfolio_matches_individual():
    """One portfolio — aggregate metrics should equal individual metrics."""
    values = [100_000, 101_000, 102_000, 100_500]
    trades = [
        {
            "ticker": "AAPL",
            "entry_date": date(2024, 1, 1),
            "exit_date": date(2024, 1, 3),
            "entry_price": 150.0,
            "exit_price": 155.0,
            "quantity": 10,
            "pnl": 50.0,
            "entry_commission": 0.05,
            "exit_commission": 0.05,
            "entry_signals": {},
            "exit_reason": "trailing_stop",
        }
    ]
    result = _make_result(values, trades)
    configs = {"solo": PortfolioConfig("solo", 100_000, lambda t, b: None, _make_risk_engine())}
    results = {"solo": result}

    agg = compute_aggregate_metrics(results, configs)

    assert agg["portfolio_values"] == values
    assert agg["metrics"]["total_return"] == result.metrics["total_return"]
    assert agg["metrics"]["sharpe_ratio"] == result.metrics["sharpe_ratio"]
    assert agg["metrics"]["max_drawdown"] == result.metrics["max_drawdown"]
    assert agg["metrics"]["total_trades"] == 1


def test_aggregate_sums_equity_curves():
    """Two portfolios — combined values are element-wise sums."""
    values_a = [50_000, 51_000, 52_000]
    values_b = [30_000, 30_500, 31_000]
    dates = [date(2024, 1, 1), date(2024, 1, 2)]

    result_a = _make_result(values_a, dates=dates)
    result_b = _make_result(values_b, dates=dates)
    configs = {
        "a": PortfolioConfig("a", 50_000, lambda t, b: None, _make_risk_engine()),
        "b": PortfolioConfig("b", 30_000, lambda t, b: None, _make_risk_engine()),
    }
    results = {"a": result_a, "b": result_b}

    agg = compute_aggregate_metrics(results, configs)

    expected = [80_000, 81_500, 83_000]
    assert agg["portfolio_values"] == expected


def test_aggregate_trades_tagged_with_portfolio():
    """All trades in the aggregate have a 'portfolio' key."""
    trade_a = {
        "ticker": "AAPL",
        "entry_date": date(2024, 1, 1),
        "exit_date": date(2024, 1, 3),
        "entry_price": 150.0,
        "exit_price": 155.0,
        "quantity": 10,
        "pnl": 50.0,
        "entry_commission": 0.05,
        "exit_commission": 0.05,
        "entry_signals": {},
        "exit_reason": "trailing_stop",
    }
    trade_b = {
        "ticker": "MSFT",
        "entry_date": date(2024, 1, 2),
        "exit_date": date(2024, 1, 4),
        "entry_price": 300.0,
        "exit_price": 310.0,
        "quantity": 5,
        "pnl": 50.0,
        "entry_commission": 0.03,
        "exit_commission": 0.03,
        "entry_signals": {},
        "exit_reason": "trailing_stop",
    }
    dates = [date(2024, 1, 1), date(2024, 1, 2)]
    result_a = _make_result([50_000, 50_050, 50_100], [trade_a], dates)
    result_b = _make_result([50_000, 50_050, 50_100], [trade_b], dates)

    configs = {
        "mr": PortfolioConfig("mr", 50_000, lambda t, b: None, _make_risk_engine()),
        "mom": PortfolioConfig("mom", 50_000, lambda t, b: None, _make_risk_engine()),
    }
    results = {"mr": result_a, "mom": result_b}

    agg = compute_aggregate_metrics(results, configs)

    assert len(agg["trades"]) == 2
    assert all("portfolio" in t for t in agg["trades"])
    portfolio_names = {t["portfolio"] for t in agg["trades"]}
    assert portfolio_names == {"mr", "mom"}
    # Verify original trades are not mutated
    assert "portfolio" not in trade_a
    assert "portfolio" not in trade_b


def test_aggregate_empty_results():
    """Empty dict returns empty metrics."""
    agg = compute_aggregate_metrics({}, {})

    assert agg["portfolio_values"] == []
    assert agg["trades"] == []
    assert agg["dates"] == []
    assert agg["metrics"] == {}


def test_universe_registry_has_required_keys():
    """Universe registry defines tickers for each known strategy."""
    from scripts.run_backtest import UNIVERSE_REGISTRY, SP500_TOP50, BEAR_TICKERS

    assert "mean_reversion" in UNIVERSE_REGISTRY
    assert "momentum" in UNIVERSE_REGISTRY
    assert set(UNIVERSE_REGISTRY["mean_reversion"]) == set(SP500_TOP50)
    assert set(SP500_TOP50).issubset(set(UNIVERSE_REGISTRY["momentum"]))
    assert BEAR_TICKERS.issubset(set(UNIVERSE_REGISTRY["momentum"]))


def test_universe_registry_union():
    """get_union_universe returns deduplicated union of all strategy tickers."""
    from scripts.run_backtest import get_union_universe

    universe = get_union_universe(["mean_reversion", "momentum"])
    # Should contain SP500 + BEAR_TICKERS, no duplicates
    assert len(universe) == len(set(universe))
    assert "AAPL" in universe
    assert "SH" in universe


def test_split_portfolios_run_independently():
    """MR and momentum portfolios produce results with independent capital."""
    from datetime import date

    from backtest.runner import BacktestRunner
    from backtest.simulator import SimulatedExecutor
    from scripts.run_backtest import PortfolioConfig, compute_aggregate_metrics
    from services.risk_management.engine import RiskEngine

    bars = {
        "AAPL": [
            {"date": date(2024, 1, d), "open": 150.0 + d, "high": 152.0 + d,
             "low": 149.0 + d, "close": 151.0 + d, "volume": 1000}
            for d in range(1, 6)
        ],
        "MSFT": [
            {"date": date(2024, 1, d), "open": 300.0 + d, "high": 302.0 + d,
             "low": 299.0 + d, "close": 301.0 + d, "volume": 1000}
            for d in range(1, 6)
        ],
    }

    executor = SimulatedExecutor(slippage_bps=0, commission_per_share=0)

    configs = {
        "mr": PortfolioConfig("mr", 60_000, lambda t, b: None, RiskEngine()),
        "mom": PortfolioConfig("mom", 40_000, lambda t, b: None, RiskEngine()),
    }

    results = {}
    for name, pc in configs.items():
        runner = BacktestRunner(executor=executor, initial_capital=pc.capital)
        results[name] = runner.run(bars, pc.signals_fn, pc.risk_engine)

    assert results["mr"].portfolio_values[0] == 60_000
    assert results["mom"].portfolio_values[0] == 40_000

    agg = compute_aggregate_metrics(results, configs)
    assert agg["portfolio_values"][0] == 100_000
