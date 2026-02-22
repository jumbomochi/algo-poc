# Phase 4: Tail-Risk Hedge + Performance-Adaptive Rebalancer

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add the 8th and final strategy (tail-risk hedge with regime-based rotation) and a performance-adaptive rebalancer that post-processes equity curves to simulate monthly capital reallocation.

**Architecture:** The tail-risk hedge signal function rotates between inverse ETFs and defensive assets based on the market regime (bull/neutral/bear) computed by the existing `compute_regime_by_date()`. The rebalancer is a pure post-processing step: it takes independently-run per-strategy equity curves, computes trailing 6-month Sharpe per strategy monthly, and shifts weights toward higher-performing strategies. No changes to BacktestRunner, RiskEngine, or metrics.

**Tech Stack:** Python, numpy (already imported), pytest

---

## Context

### Existing infrastructure used by this phase

- `compute_regime_by_date(bars_by_ticker) -> dict[date, str]` — returns "bull", "neutral", or "bear" per date
- `DEFENSIVE_TICKERS = ["SH", "PSQ", "SDS", "TLT", "GLD"]` — already defined in `scripts/run_backtest.py`
- `UNIVERSE_REGISTRY["tail_risk_hedge"]` — already maps to `DEFENSIVE_TICKERS`
- `PortfolioConfig` dataclass — name, capital, signals_fn, risk_engine
- `compute_aggregate_metrics()` — sums equity curves, pools trades
- `BacktestRunner.run()` returns `BacktestResult` with `portfolio_values`, `trades`, `dates`, `metrics`

### Signal function contract

All signal functions match: `(ticker: str, bars: list[dict]) -> dict | None`

Return dict must have: `action` ("buy"/"sell"), `ticker`, `limit_price`, `quantity`, `sector`, and optionally `signals` (buy) or `exit_reason` (sell).

### Tail-Risk Hedge spec (from design doc)

| Regime | Allocation |
|---|---|
| Bull | 50% GLD + 50% TLT |
| Neutral | 40% GLD + 40% TLT + 20% SH |
| Bear | 40% SH + 30% PSQ + 20% SDS + 10% GLD |

Exit: regime change triggers full rotation (sell everything, re-buy per new allocation).

### Rebalancer spec (from design doc)

- Monthly evaluation, rebalance only if drift > 3%
- Metric: trailing 6-month Sharpe per strategy
- Above-median Sharpe strategies gain from below-median
- Shift cap: 5% max per strategy per rebalance
- Floor: 5% per strategy (tail-risk: 8%)
- Ceiling: 25% per strategy
- Bear regime: tail-risk allocation increases to 15%

---

## Task 1: Tail-Risk Hedge Signal Function

**Files:**
- Modify: `scripts/run_backtest.py` (add `make_tail_risk_hedge_signals_fn()` after `make_earnings_drift_signals_fn()` around line 1107)
- Create: `tests/backtest/test_tail_risk_hedge_signals.py`

### Step 1: Write the failing tests

Create `tests/backtest/test_tail_risk_hedge_signals.py`:

```python
from __future__ import annotations

from datetime import date, timedelta

from scripts.run_backtest import make_tail_risk_hedge_signals_fn


def _make_bars(n: int, start_price: float = 100.0, start_date: date = date(2024, 1, 1)):
    """Generate n daily bars."""
    bars = []
    price = start_price
    for i in range(n):
        d = start_date + timedelta(days=i)
        bars.append({
            "date": d,
            "open": price,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "volume": 1_000_000,
        })
        price *= 1.001
    return bars


def test_tail_risk_hedge_buys_bull_allocation():
    """In bull regime, buys GLD and TLT only."""
    regime_by_date = {}
    start = date(2024, 1, 1)
    for i in range(60):
        regime_by_date[start + timedelta(days=i)] = "bull"

    signals_fn = make_tail_risk_hedge_signals_fn(
        regime_by_date=regime_by_date,
        position_size_pct=0.25,
        initial_capital=10_000,
    )

    bars = _make_bars(10, start_date=start)

    # GLD and TLT should get buy signals in bull regime
    gld_signal = signals_fn("GLD", bars)
    tlt_signal = signals_fn("TLT", bars)
    assert gld_signal is not None
    assert gld_signal["action"] == "buy"
    assert tlt_signal is not None
    assert tlt_signal["action"] == "buy"

    # SH, PSQ, SDS should NOT get buy signals in bull regime
    sh_signal = signals_fn("SH", bars)
    assert sh_signal is None


def test_tail_risk_hedge_buys_bear_allocation():
    """In bear regime, buys SH, PSQ, SDS, GLD (not TLT)."""
    regime_by_date = {}
    start = date(2024, 1, 1)
    for i in range(60):
        regime_by_date[start + timedelta(days=i)] = "bear"

    signals_fn = make_tail_risk_hedge_signals_fn(
        regime_by_date=regime_by_date,
        position_size_pct=0.25,
        initial_capital=10_000,
    )

    bars = _make_bars(10, start_date=start)

    # SH, PSQ, SDS, GLD should get buy signals in bear regime
    sh_signal = signals_fn("SH", bars)
    psq_signal = signals_fn("PSQ", bars)
    sds_signal = signals_fn("SDS", bars)
    gld_signal = signals_fn("GLD", bars)
    assert sh_signal is not None and sh_signal["action"] == "buy"
    assert psq_signal is not None and psq_signal["action"] == "buy"
    assert sds_signal is not None and sds_signal["action"] == "buy"
    assert gld_signal is not None and gld_signal["action"] == "buy"

    # TLT should NOT get buy signal in bear
    tlt_signal = signals_fn("TLT", bars)
    assert tlt_signal is None


def test_tail_risk_hedge_regime_change_sells():
    """Regime change from bull to bear sells existing positions."""
    regime_by_date = {}
    start = date(2024, 1, 1)
    # First 10 days: bull, then switch to bear
    for i in range(10):
        regime_by_date[start + timedelta(days=i)] = "bull"
    for i in range(10, 30):
        regime_by_date[start + timedelta(days=i)] = "bear"

    signals_fn = make_tail_risk_hedge_signals_fn(
        regime_by_date=regime_by_date,
        position_size_pct=0.25,
        initial_capital=10_000,
    )

    # Buy GLD during bull (bar 10 = day index 9, bull regime)
    bull_bars = _make_bars(10, start_date=start)
    buy_signal = signals_fn("GLD", bull_bars)
    assert buy_signal is not None and buy_signal["action"] == "buy"

    # After regime changes to bear, GLD should get a sell signal
    # (GLD is in bear allocation too, but the regime change triggers full rotation)
    bear_bars = _make_bars(11, start_date=start)
    sell_signal = signals_fn("GLD", bear_bars)
    assert sell_signal is not None and sell_signal["action"] == "sell"
    assert sell_signal["exit_reason"] == "regime_change"
```

### Step 2: Run tests to verify they fail

Run: `pytest tests/backtest/test_tail_risk_hedge_signals.py -v`
Expected: FAIL with `ImportError: cannot import name 'make_tail_risk_hedge_signals_fn'`

### Step 3: Write implementation

Add to `scripts/run_backtest.py` after `make_earnings_drift_signals_fn()` (after line 1107):

```python
def make_tail_risk_hedge_signals_fn(
    regime_by_date: dict,
    position_size_pct: float = 0.25,
    initial_capital: float = 100_000,
):
    """Create a tail-risk hedge signal function.

    Rotates between inverse ETFs and defensive assets based on market regime.
    Bull: 50% GLD + 50% TLT
    Neutral: 40% GLD + 40% TLT + 20% SH
    Bear: 40% SH + 30% PSQ + 20% SDS + 10% GLD
    Regime change triggers full rotation (sell all, re-buy per new allocation).
    """
    ALLOCATIONS = {
        "bull": {"GLD": 0.50, "TLT": 0.50},
        "neutral": {"GLD": 0.40, "TLT": 0.40, "SH": 0.20},
        "bear": {"SH": 0.40, "PSQ": 0.30, "SDS": 0.20, "GLD": 0.10},
    }

    tracked: dict[str, dict] = {}  # ticker -> {entry_price, regime_at_entry}
    last_regime: list[str | None] = [None]  # mutable container for closure

    def signals_fn(ticker: str, bars: list[dict]) -> dict | None:
        if len(bars) < 2:
            return None

        current_price = bars[-1]["close"]
        current_date = bars[-1]["date"]
        regime = regime_by_date.get(current_date, "bull")

        lot = tracked.get(ticker)

        # Detect regime change and sell existing positions
        if lot is not None and lot["regime_at_entry"] != regime:
            tracked.pop(ticker, None)
            return {
                "action": "sell",
                "ticker": ticker,
                "limit_price": current_price,
                "quantity": 0,
                "sector": "Unknown",
                "exit_reason": "regime_change",
            }

        # Entry: buy if ticker is in current regime allocation and not already held
        allocation = ALLOCATIONS.get(regime, {})
        if lot is None and ticker in allocation:
            weight = allocation[ticker]
            quantity = max(1, int(initial_capital * position_size_pct * weight / current_price))
            tracked[ticker] = {
                "entry_price": current_price,
                "regime_at_entry": regime,
            }
            return {
                "action": "buy",
                "ticker": ticker,
                "limit_price": current_price,
                "quantity": quantity,
                "sector": "Unknown",
                "signals": {
                    "strategy": "tail_risk_hedge",
                    "regime": regime,
                    "weight": weight,
                },
            }

        return None

    return signals_fn
```

### Step 4: Run tests to verify they pass

Run: `pytest tests/backtest/test_tail_risk_hedge_signals.py -v`
Expected: 3 passed

### Step 5: Commit

```bash
git add tests/backtest/test_tail_risk_hedge_signals.py scripts/run_backtest.py
git commit -m "feat: add tail-risk hedge signal function with regime-based rotation"
```

---

## Task 2: Performance-Adaptive Rebalancer

**Files:**
- Modify: `scripts/run_backtest.py` (add `simulate_rebalancer()` function)
- Create: `tests/backtest/test_rebalancer.py`

### Step 1: Write the failing tests

Create `tests/backtest/test_rebalancer.py`:

```python
from __future__ import annotations

import math

from scripts.run_backtest import simulate_rebalancer


def test_rebalancer_with_equal_performance():
    """When all strategies have equal returns, weights don't change."""
    # Two strategies, both grow 10% over 6 months (126 trading days)
    n_days = 150
    strategy_curves = {
        "alpha": [100_000 * (1 + 0.10 * i / n_days) for i in range(n_days)],
        "beta":  [100_000 * (1 + 0.10 * i / n_days) for i in range(n_days)],
    }
    initial_weights = {"alpha": 0.50, "beta": 0.50}

    result = simulate_rebalancer(
        strategy_curves=strategy_curves,
        initial_weights=initial_weights,
        rebalance_interval_days=21,
        lookback_days=126,
        max_shift_pct=0.05,
        floor_pct=0.05,
        ceiling_pct=0.25,
        special_floors={},
    )

    assert "weights_history" in result
    assert "rebalanced_values" in result
    assert len(result["rebalanced_values"]) == n_days

    # Final weights should be very close to initial (equal performance)
    final_weights = result["weights_history"][-1]["weights"]
    assert abs(final_weights["alpha"] - 0.50) < 0.01
    assert abs(final_weights["beta"] - 0.50) < 0.01


def test_rebalancer_shifts_toward_outperformer():
    """Strategy with higher Sharpe gets more weight."""
    n_days = 150
    # alpha: steady growth (high Sharpe)
    # beta: volatile with lower growth (lower Sharpe)
    strategy_curves = {
        "alpha": [100_000 * (1 + 0.20 * i / n_days) for i in range(n_days)],
        "beta":  [100_000 * (1 + 0.02 * i / n_days) for i in range(n_days)],
    }
    initial_weights = {"alpha": 0.50, "beta": 0.50}

    result = simulate_rebalancer(
        strategy_curves=strategy_curves,
        initial_weights=initial_weights,
        rebalance_interval_days=21,
        lookback_days=126,
        max_shift_pct=0.05,
        floor_pct=0.05,
        ceiling_pct=0.25,
        special_floors={},
    )

    final_weights = result["weights_history"][-1]["weights"]
    # Alpha should have gained weight, beta should have lost weight
    assert final_weights["alpha"] > 0.50
    assert final_weights["beta"] < 0.50


def test_rebalancer_respects_floor_and_ceiling():
    """Weights never go below floor or above ceiling."""
    n_days = 250  # Enough for multiple rebalance cycles
    # Extreme difference: alpha great, beta terrible
    strategy_curves = {
        "alpha": [100_000 * (1 + 0.50 * i / n_days) for i in range(n_days)],
        "beta":  [100_000 * max(0.5, 1 - 0.30 * i / n_days) for i in range(n_days)],
    }
    initial_weights = {"alpha": 0.50, "beta": 0.50}

    result = simulate_rebalancer(
        strategy_curves=strategy_curves,
        initial_weights=initial_weights,
        rebalance_interval_days=21,
        lookback_days=126,
        max_shift_pct=0.05,
        floor_pct=0.10,
        ceiling_pct=0.25,
        special_floors={"beta": 0.15},
    )

    for entry in result["weights_history"]:
        w = entry["weights"]
        assert w["alpha"] <= 0.25 + 0.001, f"alpha exceeded ceiling: {w['alpha']}"
        assert w["beta"] >= 0.15 - 0.001, f"beta below special floor: {w['beta']}"


def test_rebalancer_output_length_matches_input():
    """Rebalanced curve has same length as input curves."""
    n_days = 100
    strategy_curves = {
        "a": [10_000 * (1 + 0.1 * i / n_days) for i in range(n_days)],
    }
    initial_weights = {"a": 1.0}

    result = simulate_rebalancer(
        strategy_curves=strategy_curves,
        initial_weights=initial_weights,
        rebalance_interval_days=21,
        lookback_days=50,
        max_shift_pct=0.05,
        floor_pct=0.05,
        ceiling_pct=1.0,
        special_floors={},
    )

    assert len(result["rebalanced_values"]) == n_days
```

### Step 2: Run tests to verify they fail

Run: `pytest tests/backtest/test_rebalancer.py -v`
Expected: FAIL with `ImportError: cannot import name 'simulate_rebalancer'`

### Step 3: Write implementation

Add to `scripts/run_backtest.py` after `compute_aggregate_metrics()` (after line 1158):

```python
def simulate_rebalancer(
    strategy_curves: dict[str, list[float]],
    initial_weights: dict[str, float],
    rebalance_interval_days: int = 21,
    lookback_days: int = 126,
    max_shift_pct: float = 0.05,
    floor_pct: float = 0.05,
    ceiling_pct: float = 0.25,
    special_floors: dict[str, float] | None = None,
) -> dict:
    """Simulate performance-adaptive rebalancing on strategy equity curves.

    Post-processing: takes independently-run strategy curves and re-weights
    them monthly based on trailing 6-month Sharpe.

    Args:
        strategy_curves: {strategy_name: [daily_values]} — all same length
        initial_weights: {strategy_name: weight} — must sum to 1.0
        rebalance_interval_days: days between rebalance checks (21 = ~monthly)
        lookback_days: trailing window for Sharpe computation (126 = ~6 months)
        max_shift_pct: max weight change per strategy per rebalance
        floor_pct: minimum weight per strategy
        ceiling_pct: maximum weight per strategy
        special_floors: per-strategy floor overrides (e.g. {"tail_risk_hedge": 0.08})

    Returns:
        dict with keys:
        - rebalanced_values: list[float] — combined equity curve with rebalancing
        - weights_history: list[dict] — {day_index, weights: {name: weight}}
    """
    if special_floors is None:
        special_floors = {}

    names = list(strategy_curves.keys())
    n_days = len(next(iter(strategy_curves.values())))
    weights = dict(initial_weights)
    weights_history = [{"day_index": 0, "weights": dict(weights)}]

    # Compute daily returns for each strategy
    daily_returns: dict[str, list[float]] = {}
    for name, values in strategy_curves.items():
        returns = [0.0]  # day 0 has no return
        for i in range(1, len(values)):
            if values[i - 1] > 0:
                returns.append((values[i] - values[i - 1]) / values[i - 1])
            else:
                returns.append(0.0)
        daily_returns[name] = returns

    # Build rebalanced combined curve
    rebalanced_values = [sum(strategy_curves[n][0] * weights[n] / initial_weights[n]
                             for n in names if initial_weights[n] > 0)]

    # Track per-strategy notional values for weight tracking
    strategy_notional = {n: strategy_curves[n][0] for n in names}

    for day in range(1, n_days):
        # Apply daily returns to notional values
        for n in names:
            strategy_notional[n] *= (1 + daily_returns[n][day])

        # Compute combined value using current weights
        total = sum(strategy_notional[n] * weights[n] / initial_weights[n]
                    for n in names if initial_weights[n] > 0)
        rebalanced_values.append(total)

        # Monthly rebalance check
        if day > 0 and day % rebalance_interval_days == 0 and day >= lookback_days:
            # Compute trailing Sharpe for each strategy
            sharpes = {}
            for n in names:
                window = daily_returns[n][day - lookback_days:day]
                if len(window) < 20:
                    sharpes[n] = 0.0
                    continue
                mean_r = sum(window) / len(window)
                var_r = sum((r - mean_r) ** 2 for r in window) / len(window)
                std_r = var_r ** 0.5
                sharpes[n] = (mean_r / std_r * (252 ** 0.5)) if std_r > 0 else 0.0

            # Determine median Sharpe
            sorted_sharpes = sorted(sharpes.values())
            median_sharpe = sorted_sharpes[len(sorted_sharpes) // 2]

            # Shift weights: above-median gain, below-median lose
            adjustments = {}
            for n in names:
                if sharpes[n] > median_sharpe:
                    adjustments[n] = min(max_shift_pct, (sharpes[n] - median_sharpe) * 0.01)
                elif sharpes[n] < median_sharpe:
                    adjustments[n] = -min(max_shift_pct, (median_sharpe - sharpes[n]) * 0.01)
                else:
                    adjustments[n] = 0.0

            # Apply adjustments
            new_weights = {}
            for n in names:
                w = weights[n] + adjustments[n]
                effective_floor = special_floors.get(n, floor_pct)
                w = max(effective_floor, min(ceiling_pct, w))
                new_weights[n] = w

            # Normalize to sum to 1.0
            total_w = sum(new_weights.values())
            if total_w > 0:
                new_weights = {n: w / total_w for n, w in new_weights.items()}

            # Re-apply floors/ceilings after normalization
            for n in names:
                effective_floor = special_floors.get(n, floor_pct)
                new_weights[n] = max(effective_floor, min(ceiling_pct, new_weights[n]))

            # Re-normalize
            total_w = sum(new_weights.values())
            if total_w > 0:
                new_weights = {n: w / total_w for n, w in new_weights.items()}

            weights = new_weights
            weights_history.append({"day_index": day, "weights": dict(weights)})

            # Reset notional to match new weights
            for n in names:
                strategy_notional[n] = strategy_curves[n][0]

    return {
        "rebalanced_values": rebalanced_values,
        "weights_history": weights_history,
    }
```

### Step 4: Run tests to verify they pass

Run: `pytest tests/backtest/test_rebalancer.py -v`
Expected: 4 passed

### Step 5: Commit

```bash
git add tests/backtest/test_rebalancer.py scripts/run_backtest.py
git commit -m "feat: add performance-adaptive rebalancer simulation"
```

---

## Task 3: Wire Tail-Risk Hedge Into main()

**Files:**
- Modify: `scripts/run_backtest.py` — update `main()` to add 8th portfolio, compute regime, pass to rebalancer

### Step 1: Update main() to add tail-risk hedge portfolio and rebalancer

In `main()`, the changes are:

1. Add `"tail_risk_hedge"` to the `get_union_universe()` call (line ~1370)
2. Compute `regime_by_date` from `bars_by_ticker` before building portfolios
3. Create `tail_risk_hedge_signals_fn` and its `PortfolioConfig`
4. Re-normalize capital allocations to include 8th strategy (MR 12%, Mom 18%, Sector 12%, QV 12%, ED 15%, ST-MR 10%, Thematic 11%, Tail-Risk 10%)
5. After running all portfolios, run `simulate_rebalancer()` and include rebalanced metrics in output

**Updated capital allocations in main():**

```python
    # Capital allocations (sum to 100%)
    # MR 12%, Mom 18%, Sector 12%, QV 12%, ED 15%, ST-MR 10%, Thematic 11%, Tail-Risk 10%
```

**Add regime computation after data loading (after line ~1404):**

```python
    # Compute market regime for regime-dependent strategies
    regime_by_date = compute_regime_by_date(bars_by_ticker)
    print(f"  Computed regime for {len(regime_by_date)} trading days")
```

**Add tail-risk hedge signal function and portfolio config:**

```python
    tail_risk_signals_fn = make_tail_risk_hedge_signals_fn(
        regime_by_date=regime_by_date,
        position_size_pct=0.25,
        initial_capital=args.capital * 0.10,
    )
```

And add to portfolios dict:
```python
        "tail_risk_hedge": PortfolioConfig(
            name="tail_risk_hedge",
            capital=args.capital * 0.10,
            signals_fn=tail_risk_signals_fn,
            risk_engine=RiskEngine(
                position_entry_limit_pct=25.0,
                sector_concentration_pct=50.0,
                total_exposure_limit_pct=100.0,
                max_lots_per_ticker=1,
            ),
        ),
```

**Add rebalancer call after aggregate metrics (around where results are printed):**

After computing `aggregate`, add:

```python
        # Run rebalancer simulation
        strategy_curves = {name: result.portfolio_values for name, result in results.items()}
        total_capital = sum(pc.capital for pc in portfolios.values())
        initial_weights = {name: pc.capital / total_capital for name, pc in portfolios.items()}

        rebalancer_result = simulate_rebalancer(
            strategy_curves=strategy_curves,
            initial_weights=initial_weights,
            rebalance_interval_days=21,
            lookback_days=126,
            max_shift_pct=0.05,
            floor_pct=0.05,
            ceiling_pct=0.25,
            special_floors={"tail_risk_hedge": 0.08},
        )
```

### Step 2: Run all tests

Run: `pytest tests/backtest/ -v`
Expected: All tests pass (existing 78 + 3 tail-risk + 4 rebalancer = 85 total)

### Step 3: Commit

```bash
git add scripts/run_backtest.py
git commit -m "feat: wire tail-risk hedge and rebalancer into main()"
```

---

## Task 4: Add Multi-Portfolio Test for 8 Portfolios

**Files:**
- Modify: `tests/backtest/test_multi_portfolio.py` (add test for 8-portfolio aggregate)

### Step 1: Write the test

Add to `tests/backtest/test_multi_portfolio.py`:

```python
def test_eight_portfolios_aggregate():
    """Eight independent portfolios aggregate correctly."""
    from backtest.runner import BacktestResult
    from scripts.run_backtest import PortfolioConfig, compute_aggregate_metrics
    from services.risk_management.engine import RiskEngine

    def noop_fn(ticker, bars):
        return None

    configs = {}
    results = {}
    for i, name in enumerate([
        "mean_reversion", "momentum", "sector_rotation", "quality_value",
        "earnings_drift", "short_term_mr", "thematic_momentum", "tail_risk_hedge",
    ]):
        configs[name] = PortfolioConfig(
            name=name,
            capital=10_000 + i * 1_000,
            signals_fn=noop_fn,
            risk_engine=RiskEngine(),
        )
        results[name] = BacktestResult(
            trades=[],
            portfolio_values=[10_000 + i * 1_000, 10_500 + i * 1_000],
            dates=["2024-01-01", "2024-01-02"],
            metrics={"total_return": 0.05, "sharpe_ratio": 1.0,
                     "max_drawdown": 0.02, "win_rate": 0.5,
                     "total_trades": 0, "avg_holding_period_days": 0},
        )

    agg = compute_aggregate_metrics(results, configs)
    assert len(agg["portfolio_values"]) == 2
    # Sum of initial values: 10k + 11k + 12k + 13k + 14k + 15k + 16k + 17k = 108k
    assert agg["portfolio_values"][0] == sum(10_000 + i * 1_000 for i in range(8))
    assert len(agg["trades"]) == 0
```

### Step 2: Run test

Run: `pytest tests/backtest/test_multi_portfolio.py -v`
Expected: All pass

### Step 3: Commit

```bash
git add tests/backtest/test_multi_portfolio.py
git commit -m "test: add 8-portfolio aggregate test"
```

---

## Task 5: Update Documentation

**Files:**
- Modify: `docs/strategy.md`

### Step 1: Update strategy.md

Update the following sections:

1. **Current Portfolio Configuration table** — add 8th row for `tail_risk_hedge`:
   ```
   | `tail_risk_hedge` | 10% of total | Regime-based defensive rotation | 25% entry, 100% exposure, 1 lot |
   ```

2. **Implementation table** — add `make_tail_risk_hedge_signals_fn()` and `simulate_rebalancer()`:
   ```
   | `make_tail_risk_hedge_signals_fn()` | Tail-risk hedge: regime-based rotation between inverse/defensive ETFs |
   | `simulate_rebalancer()` | Post-processing: performance-adaptive weight rebalancing |
   ```

3. **Capital allocations** — update to 8-strategy split:
   MR 12%, Mom 18%, Sector 12%, QV 12%, ED 15%, ST-MR 10%, Thematic 11%, Tail-Risk 10%

4. **Add Rebalancer section** after Multi-Portfolio Infrastructure:
   ```markdown
   ## Performance-Adaptive Rebalancer

   A post-processing simulation that re-weights strategy equity curves based on trailing performance.

   ### How It Works

   After all portfolios run independently, `simulate_rebalancer()` takes their equity curves and:
   1. Every 21 trading days (~monthly), computes trailing 6-month Sharpe per strategy
   2. Strategies with above-median Sharpe gain weight; below-median lose weight
   3. Max shift: 5% per strategy per rebalance
   4. Floor: 5% per strategy (tail-risk hedge: 8%)
   5. Ceiling: 25% per strategy
   6. Weights re-normalized to sum to 1.0

   ### Output

   Returns a rebalanced combined equity curve and weights history for analysis.
   This is an approximation valid at retail scale where position scaling has no market impact.
   ```

### Step 2: Commit

```bash
git add docs/strategy.md
git commit -m "docs: update strategy.md with tail-risk hedge and rebalancer"
```

---

## Verification

After all tasks:

```bash
# All tests should pass
pytest tests/backtest/ -v

# Expected: ~86 tests pass (78 existing + 3 tail-risk + 4 rebalancer + 1 multi-portfolio)
```
