# Multi-Strategy Portfolio — Phase 1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Split the existing dual strategy into independent mean-reversion and momentum portfolios, add a universe registry for per-strategy ticker sets, and validate that the split produces correct aggregate results.

**Architecture:** Each strategy gets its own `PortfolioConfig` with independent capital, risk engine, and signal function. A `UNIVERSE_REGISTRY` maps strategy names to ticker lists. Bar data is fetched once for the union of all universes, then each runner receives the full dataset (signal functions already filter to their relevant tickers). Existing `compute_aggregate_metrics()` handles result aggregation.

**Tech Stack:** Python 3.12, pytest, existing backtest infrastructure (BacktestRunner, RiskEngine, SimulatedExecutor)

---

### Task 1: Add Universe Registry

**Files:**
- Modify: `scripts/run_backtest.py:49-59`
- Test: `tests/backtest/test_multi_portfolio.py`

**Step 1: Write the failing test**

Add to `tests/backtest/test_multi_portfolio.py`:

```python
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
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/backtest/test_multi_portfolio.py::test_universe_registry_has_required_keys tests/backtest/test_multi_portfolio.py::test_universe_registry_union -v`
Expected: FAIL with `ImportError: cannot import name 'UNIVERSE_REGISTRY'`

**Step 3: Write minimal implementation**

Add to `scripts/run_backtest.py` after the `BEAR_TICKERS` definition (line 59):

```python
# Per-strategy ticker universes
UNIVERSE_REGISTRY: dict[str, list[str]] = {
    "mean_reversion": SP500_TOP50,
    "momentum": SP500_TOP50 + [t for t in sorted(BEAR_TICKERS) if t not in SP500_TOP50],
}


def get_union_universe(strategy_names: list[str]) -> list[str]:
    """Return deduplicated union of tickers across the given strategies."""
    seen: set[str] = set()
    result: list[str] = []
    for name in strategy_names:
        for ticker in UNIVERSE_REGISTRY[name]:
            if ticker not in seen:
                seen.add(ticker)
                result.append(ticker)
    return result
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/backtest/test_multi_portfolio.py::test_universe_registry_has_required_keys tests/backtest/test_multi_portfolio.py::test_universe_registry_union -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `pytest tests/backtest/ -v`
Expected: All tests pass (no regressions)

**Step 6: Commit**

```bash
git add scripts/run_backtest.py tests/backtest/test_multi_portfolio.py
git commit -m "feat: add universe registry mapping strategies to ticker sets"
```

---

### Task 2: Split Dual Portfolio into Independent MR and Momentum

**Files:**
- Modify: `scripts/run_backtest.py:790-843` (main function)
- Test: `tests/backtest/test_multi_portfolio.py`

**Step 1: Write the failing test**

Add to `tests/backtest/test_multi_portfolio.py`:

```python
def test_split_portfolios_run_independently():
    """MR and momentum portfolios produce results with independent capital."""
    from datetime import date

    from backtest.runner import BacktestRunner
    from backtest.simulator import SimulatedExecutor
    from scripts.run_backtest import PortfolioConfig, compute_aggregate_metrics
    from services.risk_management.engine import RiskEngine

    # Minimal bar data: 2 tickers, 5 days each
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

    # No-op signal functions (no trades) — validates independent capital tracking
    configs = {
        "mr": PortfolioConfig("mr", 60_000, lambda t, b: None, RiskEngine()),
        "mom": PortfolioConfig("mom", 40_000, lambda t, b: None, RiskEngine()),
    }

    results = {}
    for name, pc in configs.items():
        runner = BacktestRunner(executor=executor, initial_capital=pc.capital)
        results[name] = runner.run(bars, pc.signals_fn, pc.risk_engine)

    # Each portfolio tracks its own capital independently
    assert results["mr"].portfolio_values[0] == 60_000
    assert results["mom"].portfolio_values[0] == 40_000

    # Aggregate sums correctly
    agg = compute_aggregate_metrics(results, configs)
    assert agg["portfolio_values"][0] == 100_000
```

**Step 2: Run test to verify it passes**

This test uses existing infrastructure — it should pass immediately.

Run: `pytest tests/backtest/test_multi_portfolio.py::test_split_portfolios_run_independently -v`
Expected: PASS

**Step 3: Modify main() to split the dual portfolio**

In `scripts/run_backtest.py`, replace the portfolios dict (lines 831-843) and the data fetch section (lines 790-792):

Replace the data fetch (line 790-791):
```python
    # 1. Fetch data from IB
    all_tickers = get_union_universe(["mean_reversion", "momentum"])
    print(f"Step 1: Fetching historical data from IB Gateway ({len(all_tickers)} tickers)...")
```

Replace the portfolios dict (lines 831-843):
```python
    portfolios: dict[str, PortfolioConfig] = {
        "mean_reversion": PortfolioConfig(
            name="mean_reversion",
            capital=args.capital * 0.40,
            signals_fn=mr_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=15.0,
                sector_concentration_pct=30.0,
                total_exposure_limit_pct=120.0,
                max_lots_per_ticker=2,
            ),
        ),
        "momentum": PortfolioConfig(
            name="momentum",
            capital=args.capital * 0.60,
            signals_fn=mom_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=12.0,
                sector_concentration_pct=30.0,
                total_exposure_limit_pct=150.0,
                max_lots_per_ticker=1,
            ),
        ),
    }
```

Note: capital split is 40/60 (MR/Momentum) for now — these are the design doc ratios normalized to just these two strategies (12%/18% = 40%/60%). When more strategies are added in later phases, these percentages will decrease.

Also remove the `combined_fn` line (line 829) and the `make_combined_signals_fn` call since MR and Momentum are now independent:
```python
    # Delete this line:
    # combined_fn = make_combined_signals_fn(mr_signals_fn, mom_signals_fn)
```

**Step 4: Run full test suite**

Run: `pytest tests/backtest/ -v`
Expected: All tests pass

**Step 5: Commit**

```bash
git add scripts/run_backtest.py tests/backtest/test_multi_portfolio.py
git commit -m "feat: split dual strategy into independent MR and momentum portfolios"
```

---

### Task 3: Wire main() to Always Use Multi-Portfolio Output

**Files:**
- Modify: `scripts/run_backtest.py:855-892` (print and save sections in main)

**Step 1: Verify current behavior**

The current `main()` has an `if len(portfolios) == 1` branch that uses single-portfolio output. Now that we always have 2 portfolios, the multi-portfolio path will always execute. Verify this is correct.

Run: `pytest tests/backtest/ -v`
Expected: All pass

**Step 2: Simplify main() to always use multi-portfolio output**

Since we now always have 2+ portfolios, remove the single-portfolio branch. Replace lines 855-892:

```python
    # 4. Print results
    aggregate = compute_aggregate_metrics(results, portfolios)
    if len(portfolios) == 1:
        result = next(iter(results.values()))
        print_results(result, elapsed)
    else:
        print_multi_portfolio_results(results, portfolios, aggregate, elapsed)

    # 5. Save results to JSON
    print("\nStep 5: Saving results...")
    base_config = {
        "tickers": all_tickers,
        "years": args.years,
        "initial_capital": args.capital,
        "slippage_bps": args.slippage_bps,
        "commission_per_share": args.commission,
        "portfolios": {name: pc.capital for name, pc in portfolios.items()},
    }
    if len(portfolios) == 1:
        result = next(iter(results.values()))
        save_results(
            config=base_config,
            trades=result.trades,
            portfolio_values=result.portfolio_values,
            dates=result.dates,
            metrics=result.metrics,
            bars=bars_by_ticker,
            output_dir=args.output_dir,
        )
    else:
        save_multi_portfolio_results(
            config=base_config,
            results=results,
            portfolio_configs=portfolios,
            aggregate=aggregate,
            bars=bars_by_ticker,
            output_dir=args.output_dir,
        )
```

Keep the `if len(portfolios) == 1` branches for backward compatibility (someone might reconfigure to a single portfolio). The key change is adding `"portfolios"` to `base_config` and switching `"tickers"` from `tickers` to `all_tickers`.

**Step 3: Run full test suite**

Run: `pytest tests/backtest/ -v`
Expected: All pass

**Step 4: Commit**

```bash
git add scripts/run_backtest.py
git commit -m "refactor: wire main() config to use universe registry and portfolio allocations"
```

---

### Task 4: Add Sector ETF and Thematic ETF Ticker Lists

**Files:**
- Modify: `scripts/run_backtest.py:49-59` (ticker constants area)
- Modify: `scripts/run_backtest.py` (UNIVERSE_REGISTRY)
- Test: `tests/backtest/test_multi_portfolio.py`

**Step 1: Write the failing test**

Add to `tests/backtest/test_multi_portfolio.py`:

```python
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
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/backtest/test_multi_portfolio.py::test_universe_registry_has_future_strategy_keys -v`
Expected: FAIL with `AssertionError: Missing universe for sector_rotation`

**Step 3: Add ticker constants and update registry**

Add after `BEAR_TICKERS` (line 59) in `scripts/run_backtest.py`:

```python
# Inverse/defensive ETFs for tail-risk hedge
DEFENSIVE_TICKERS = ["SH", "PSQ", "SDS", "TLT", "GLD"]

# SPDR sector ETFs
SECTOR_ETFS = [
    "XLK", "XLE", "XLF", "XLV", "XLY", "XLP",
    "XLI", "XLB", "XLU", "XLRE", "XLC",
]

# Thematic ETFs
THEMATIC_ETFS = [
    "ARKK", "TAN", "HACK", "BOTZ", "LIT", "CIBR", "SKYY", "DRIV",
    "FINX", "GAMR", "HERO", "IDRV", "CLOU", "WCLD", "SNSR", "PRNT",
    "IZRL", "GNOM", "ARKG", "ARKQ", "ARKW", "ARKF", "ICLN", "QCLN", "PBW",
]

# S&P 500 extended (top 100 for short-term MR)
SP500_TOP100 = SP500_TOP50 + [
    "CAT", "MS", "NEE", "LOW", "UPS", "SPGI", "RTX", "HON", "ELV",
    "BLK", "SYK", "BKNG", "MDLZ", "ADP", "VRTX", "SCHW", "GILD",
    "AMT", "REGN", "LRCX", "PANW", "BSX", "CB", "MMC", "KLAC",
    "TMUS", "SHW", "SO", "EQIX", "MO", "PGR", "ZTS", "CME",
    "CI", "DUK", "ICE", "SNPS", "CL", "AON", "MCO", "WM",
    "CDNS", "TGT", "BDX", "NOC", "APH", "ITW", "FI", "HUM",
]
```

Update `UNIVERSE_REGISTRY`:

```python
UNIVERSE_REGISTRY: dict[str, list[str]] = {
    "mean_reversion": SP500_TOP50,
    "momentum": SP500_TOP50 + [t for t in sorted(BEAR_TICKERS) if t not in SP500_TOP50],
    "sector_rotation": SECTOR_ETFS,
    "quality_value": SP500_TOP100,
    "earnings_drift": SP500_TOP100,
    "short_term_mr": SP500_TOP100,
    "thematic_momentum": THEMATIC_ETFS,
    "tail_risk_hedge": DEFENSIVE_TICKERS,
}
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/backtest/test_multi_portfolio.py::test_universe_registry_has_future_strategy_keys tests/backtest/test_multi_portfolio.py::test_universe_registry_no_duplicates_within_strategy -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `pytest tests/backtest/ -v`
Expected: All pass

**Step 6: Commit**

```bash
git add scripts/run_backtest.py tests/backtest/test_multi_portfolio.py
git commit -m "feat: add ticker lists and universe registry for all 8 strategies"
```

---

### Task 5: Validate Split Produces Correct Aggregate

**Files:**
- Test: `tests/backtest/test_multi_portfolio.py`

This is a pure validation task — no production code changes. The goal is to ensure the split MR + Momentum portfolios produce reasonable aggregate metrics when run independently.

**Step 1: Write validation test**

Add to `tests/backtest/test_multi_portfolio.py`:

```python
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
```

**Step 2: Run test**

Run: `pytest tests/backtest/test_multi_portfolio.py::test_split_portfolios_aggregate_metrics_are_valid -v`
Expected: PASS

**Step 3: Run full test suite**

Run: `pytest tests/backtest/ -v`
Expected: All pass

**Step 4: Commit**

```bash
git add tests/backtest/test_multi_portfolio.py
git commit -m "test: validate split portfolio aggregate metrics"
```

---

### Task 6: Update Documentation

**Files:**
- Modify: `docs/strategy.md`

**Step 1: Update strategy.md**

In `docs/strategy.md`, update the "Multi-Portfolio Infrastructure" section to reflect the split:

Add after the "Backward Compatibility" subsection:

```markdown
### Current Portfolio Configuration

The backtest runs two independent portfolios by default:

| Portfolio | Capital | Strategy | Risk Limits |
|---|---|---|---|
| `mean_reversion` | 40% of total | Support-level dip buying | 15% entry, 120% exposure, 2 lots |
| `momentum` | 60% of total | 6-month relative strength | 12% entry, 150% exposure, 1 lot |

This replaces the previous combined dual strategy. The two strategies no longer compete for capital — momentum signals are never rejected because mean-reversion filled the portfolio.

### Universe Registry

Each strategy defines its own ticker universe via `UNIVERSE_REGISTRY`. Bar data is fetched once for the union of all universes. Currently defined universes:

| Strategy | Universe | Ticker Count |
|---|---|---|
| `mean_reversion` | S&P 500 top 50 | 50 |
| `momentum` | S&P 500 top 50 + inverse ETFs | 52 |
| `sector_rotation` | SPDR sector ETFs | 11 |
| `quality_value` | S&P 500 top 100 | 100 |
| `earnings_drift` | S&P 500 top 100 | 100 |
| `short_term_mr` | S&P 500 top 100 | 100 |
| `thematic_momentum` | Thematic ETFs | 25 |
| `tail_risk_hedge` | Inverse + defensive ETFs | 5 |
```

**Step 2: Commit**

```bash
git add docs/strategy.md
git commit -m "docs: update strategy.md with split portfolio config and universe registry"
```

---

## Summary

| Task | What | Files | Tests |
|---|---|---|---|
| 1 | Universe registry | `run_backtest.py`, `test_multi_portfolio.py` | 2 new |
| 2 | Split dual into MR + Momentum | `run_backtest.py`, `test_multi_portfolio.py` | 1 new |
| 3 | Wire main() output | `run_backtest.py` | existing |
| 4 | Add all ticker lists | `run_backtest.py`, `test_multi_portfolio.py` | 2 new |
| 5 | Validate aggregate metrics | `test_multi_portfolio.py` | 1 new |
| 6 | Update docs | `docs/strategy.md` | — |

Total: 6 tasks, 6 commits, 6 new tests, 0 modifications to BacktestRunner/RiskEngine/metrics.
