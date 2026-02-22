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


def test_universe_registry_has_future_strategy_keys():
    """Universe registry defines tickers for all planned strategies."""
    from scripts.run_backtest import UNIVERSE_REGISTRY

    expected_strategies = [
        "mean_reversion", "momentum", "sector_rotation",
        "quality_value", "earnings_drift", "short_term_mr",
        "thematic_momentum", "tail_risk_hedge",
    ]
    for strategy in expected_strategies:
        assert strategy in UNIVERSE_REGISTRY, f"Missing universe for {strategy}"
        assert len(UNIVERSE_REGISTRY[strategy]) > 0, f"Empty universe for {strategy}"


def test_universe_registry_no_duplicates_within_strategy():
    """Each strategy's universe has no duplicate tickers."""
    from scripts.run_backtest import UNIVERSE_REGISTRY

    for name, tickers in UNIVERSE_REGISTRY.items():
        assert len(tickers) == len(set(tickers)), f"Duplicates in {name}"


def test_split_portfolios_aggregate_metrics_are_valid():
    """Split MR + Momentum portfolios produce valid aggregate metrics."""
    from datetime import date

    from backtest.runner import BacktestRunner
    from backtest.simulator import SimulatedExecutor
    from scripts.run_backtest import PortfolioConfig, compute_aggregate_metrics
    from services.risk_management.engine import RiskEngine

    # Simple signal: buy AAPL on day 2, sell on day 5
    call_count = {"n": 0}

    def simple_buy_sell(ticker, bars):
        if ticker != "AAPL" or len(bars) < 2:
            return None
        call_count["n"] += 1
        if len(bars) == 2:
            return {
                "action": "buy", "ticker": ticker,
                "limit_price": bars[-1]["close"],
                "quantity": 5, "sector": "Tech",
            }
        if len(bars) == 5:
            return {
                "action": "sell", "ticker": ticker,
                "limit_price": bars[-1]["close"],
                "quantity": 0, "sector": "Tech",
            }
        return None

    bars = {
        "AAPL": [
            {"date": date(2024, 1, d), "open": 150.0, "high": 155.0,
             "low": 148.0, "close": 150.0 + d, "volume": 50000}
            for d in range(1, 8)
        ],
    }

    executor = SimulatedExecutor(slippage_bps=0, commission_per_share=0)

    configs = {
        "strat_a": PortfolioConfig("strat_a", 60_000, simple_buy_sell, RiskEngine()),
        "strat_b": PortfolioConfig("strat_b", 40_000, simple_buy_sell, RiskEngine()),
    }

    results = {}
    for name, pc in configs.items():
        runner = BacktestRunner(executor=executor, initial_capital=pc.capital)
        results[name] = runner.run(bars, pc.signals_fn, pc.risk_engine)

    agg = compute_aggregate_metrics(results, configs)

    # Aggregate should have valid metrics
    assert agg["metrics"]["total_trades"] >= 0
    assert -1.0 <= agg["metrics"]["total_return"] <= 10.0
    assert 0.0 <= agg["metrics"]["max_drawdown"] <= 1.0
    # Aggregate portfolio values should start at combined capital
    assert agg["portfolio_values"][0] == 100_000
    # All trades should be tagged
    assert all("portfolio" in t for t in agg["trades"])


def test_five_portfolios_aggregate_correctly():
    """Five portfolios with no-op signals produce correct aggregate starting capital."""
    from datetime import date

    from backtest.runner import BacktestRunner
    from backtest.simulator import SimulatedExecutor
    from scripts.run_backtest import PortfolioConfig, compute_aggregate_metrics
    from services.risk_management.engine import RiskEngine

    bars = {
        "AAPL": [
            {"date": date(2024, 1, d), "open": 150.0, "high": 152.0,
             "low": 149.0, "close": 151.0, "volume": 1000}
            for d in range(1, 6)
        ],
    }
    executor = SimulatedExecutor(slippage_bps=0, commission_per_share=0)

    # 5 portfolios with different allocations summing to 100k
    allocations = {"mr": 16_000, "mom": 24_000, "sector": 16_000,
                   "st_mr": 20_000, "thematic": 24_000}
    configs = {
        name: PortfolioConfig(name, capital, lambda t, b: None, RiskEngine())
        for name, capital in allocations.items()
    }

    results = {}
    for name, pc in configs.items():
        runner = BacktestRunner(executor=executor, initial_capital=pc.capital)
        results[name] = runner.run(bars, pc.signals_fn, pc.risk_engine)

    agg = compute_aggregate_metrics(results, configs)
    assert agg["portfolio_values"][0] == 100_000
    assert len(results) == 5


def test_seven_portfolios_aggregate_correctly():
    """Seven portfolios with no-op signals produce correct aggregate starting capital."""
    from datetime import date

    from backtest.runner import BacktestRunner
    from backtest.simulator import SimulatedExecutor
    from scripts.run_backtest import PortfolioConfig, compute_aggregate_metrics
    from services.risk_management.engine import RiskEngine

    bars = {
        "AAPL": [
            {"date": date(2024, 1, d), "open": 150.0, "high": 152.0,
             "low": 149.0, "close": 151.0, "volume": 1000}
            for d in range(1, 6)
        ],
    }
    executor = SimulatedExecutor(slippage_bps=0, commission_per_share=0)

    allocations = {
        "mr": 14_000, "mom": 20_000, "sector": 14_000,
        "quality": 14_000, "earnings": 17_000,
        "st_mr": 11_000, "thematic": 10_000,
    }
    configs = {
        name: PortfolioConfig(name, capital, lambda t, b: None, RiskEngine())
        for name, capital in allocations.items()
    }

    results = {}
    for name, pc in configs.items():
        runner = BacktestRunner(executor=executor, initial_capital=pc.capital)
        results[name] = runner.run(bars, pc.signals_fn, pc.risk_engine)

    agg = compute_aggregate_metrics(results, configs)
    assert agg["portfolio_values"][0] == 100_000
    assert len(results) == 7
