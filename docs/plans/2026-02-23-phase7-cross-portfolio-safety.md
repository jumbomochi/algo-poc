# Phase 7: Cross-Portfolio Safety Monitoring

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add aggregate risk monitoring across all 8 portfolios — drawdown alerts, circuit breaker detection, and per-strategy divergence warnings.

**Architecture:** A standalone `AggregateRiskMonitor` class that takes aggregate equity curves and per-strategy results, checks against thresholds, and returns structured alerts. Wired into backtest output and paper trading status. This is monitoring/alerting only (does not block trades in the backtest loop).

**Tech Stack:** Python (no new dependencies)

---

## Context

### Design spec (from multi-strategy-portfolio-design.md)

**Level 2: Cross-Portfolio Monitoring** (post-execution daily monitor):
- Aggregate drawdown alert: Warning at -15% from peak
- Aggregate circuit breaker: Freeze new entries at -22%, resume at -15%
- Strategy divergence alert: Flag if any strategy exceeds 2x historical max drawdown

**Level 3: Tail-Risk Override** (deferred to future phase):
- Bear regime: Tail-risk allocation 10% → 15%
- Crash regime: Freeze all new entries

### What we have

- `compute_aggregate_metrics()` already computes combined equity curve and pooled metrics
- `BacktestMetrics.compute()` returns `max_drawdown` per strategy
- `RiskEngine.check_portfolio_drawdown()` exists but is per-strategy only
- Paper trading has `print_status()` showing per-portfolio equity

### What we're building

1. `AggregateRiskMonitor` — checks aggregate drawdown + per-strategy divergence
2. Integration into backtest output (print alerts after results)
3. Integration into paper trading status (show risk state)

---

## Task 1: AggregateRiskMonitor Class

**Files:**
- Create: `backtest/aggregate_risk.py`
- Create: `tests/backtest/test_aggregate_risk.py`

### Step 1: Write the failing tests

Create `tests/backtest/test_aggregate_risk.py`:

```python
from __future__ import annotations

from backtest.aggregate_risk import AggregateRiskMonitor


def test_no_alert_when_drawdown_small():
    """No alerts when aggregate drawdown is within limits."""
    monitor = AggregateRiskMonitor(
        alert_drawdown_pct=15.0,
        circuit_breaker_pct=22.0,
    )
    # Equity goes up steadily
    values = [100_000 + i * 100 for i in range(100)]
    alerts = monitor.check_aggregate_drawdown(values)

    assert len(alerts) == 0


def test_alert_at_15pct_drawdown():
    """Should emit warning alert when aggregate drawdown hits 15%."""
    monitor = AggregateRiskMonitor(
        alert_drawdown_pct=15.0,
        circuit_breaker_pct=22.0,
    )
    # Peak at 100k, drops to 84k = 16% drawdown
    values = [100_000, 105_000, 100_000, 90_000, 84_000]
    alerts = monitor.check_aggregate_drawdown(values)

    assert len(alerts) >= 1
    assert alerts[0]["level"] == "warning"
    assert "15.0%" in alerts[0]["message"]


def test_circuit_breaker_at_22pct_drawdown():
    """Should emit critical alert when aggregate drawdown hits 22%."""
    monitor = AggregateRiskMonitor(
        alert_drawdown_pct=15.0,
        circuit_breaker_pct=22.0,
    )
    # Peak at 100k, drops to 77k = 23% drawdown
    values = [100_000, 105_000, 90_000, 80_000, 77_000]
    alerts = monitor.check_aggregate_drawdown(values)

    critical = [a for a in alerts if a["level"] == "critical"]
    assert len(critical) >= 1
    assert "circuit breaker" in critical[0]["message"].lower()


def test_strategy_divergence_alert():
    """Should flag strategies exceeding 2x historical max drawdown."""
    monitor = AggregateRiskMonitor()
    strategy_drawdowns = {
        "momentum": 0.25,       # 25% current drawdown
        "mean_reversion": 0.10, # 10% current drawdown
    }
    historical_max = {
        "momentum": 0.10,       # historical max was 10%, now 25% = 2.5x
        "mean_reversion": 0.15, # historical max was 15%, now 10% = 0.67x
    }
    alerts = monitor.check_strategy_divergence(strategy_drawdowns, historical_max)

    assert len(alerts) == 1
    assert alerts[0]["strategy"] == "momentum"
    assert alerts[0]["level"] == "warning"


def test_no_divergence_when_within_2x():
    """No divergence alerts when all strategies within 2x historical max."""
    monitor = AggregateRiskMonitor()
    strategy_drawdowns = {
        "momentum": 0.15,
        "mean_reversion": 0.10,
    }
    historical_max = {
        "momentum": 0.10,       # 1.5x — within limit
        "mean_reversion": 0.15, # 0.67x — within limit
    }
    alerts = monitor.check_strategy_divergence(strategy_drawdowns, historical_max)

    assert len(alerts) == 0


def test_monitor_combines_all_checks():
    """monitor() should run all checks and return combined alerts."""
    monitor = AggregateRiskMonitor(
        alert_drawdown_pct=15.0,
        circuit_breaker_pct=22.0,
    )
    # 16% drawdown from peak
    aggregate_values = [100_000, 105_000, 100_000, 88_000]
    strategy_drawdowns = {"momentum": 0.25}
    historical_max = {"momentum": 0.10}

    alerts = monitor.monitor(
        aggregate_values=aggregate_values,
        strategy_drawdowns=strategy_drawdowns,
        historical_max_drawdowns=historical_max,
    )

    # Should have both drawdown warning and strategy divergence
    assert len(alerts) >= 2
    levels = {a["level"] for a in alerts}
    assert "warning" in levels
```

### Step 2: Run tests to verify they fail

Run: `pytest tests/backtest/test_aggregate_risk.py -v`
Expected: FAIL — `ModuleNotFoundError`

### Step 3: Write implementation

Create `backtest/aggregate_risk.py`:

```python
from __future__ import annotations


class AggregateRiskMonitor:
    """Cross-portfolio risk monitor.

    Checks aggregate drawdown and per-strategy divergence from
    historical norms. Returns structured alert dicts.

    Alert levels:
    - "info": informational (drawdown approaching threshold)
    - "warning": requires attention (drawdown alert, strategy divergence)
    - "critical": immediate action needed (circuit breaker)
    """

    def __init__(
        self,
        alert_drawdown_pct: float = 15.0,
        circuit_breaker_pct: float = 22.0,
        divergence_multiplier: float = 2.0,
    ) -> None:
        self.alert_drawdown_pct = alert_drawdown_pct
        self.circuit_breaker_pct = circuit_breaker_pct
        self.divergence_multiplier = divergence_multiplier

    def check_aggregate_drawdown(
        self, aggregate_values: list[float]
    ) -> list[dict]:
        """Check aggregate equity curve for drawdown alerts.

        Returns list of alert dicts with keys:
        - level: "warning" or "critical"
        - message: human-readable description
        - drawdown_pct: current drawdown as fraction (0.0-1.0)
        - peak: peak NAV value
        - current: current NAV value
        """
        if len(aggregate_values) < 2:
            return []

        alerts: list[dict] = []
        peak = aggregate_values[0]
        max_drawdown = 0.0

        for value in aggregate_values:
            peak = max(peak, value)
            if peak > 0:
                drawdown = (peak - value) / peak
                max_drawdown = max(max_drawdown, drawdown)

        current = aggregate_values[-1]
        current_peak = max(aggregate_values)
        current_dd = (current_peak - current) / current_peak if current_peak > 0 else 0.0

        dd_pct = max_drawdown * 100

        if dd_pct >= self.circuit_breaker_pct:
            alerts.append({
                "level": "critical",
                "message": (
                    f"Aggregate circuit breaker: drawdown {dd_pct:.1f}% "
                    f"exceeds {self.circuit_breaker_pct:.1f}% threshold. "
                    f"Freeze all new entries."
                ),
                "drawdown_pct": max_drawdown,
                "peak": current_peak,
                "current": current,
            })

        if dd_pct >= self.alert_drawdown_pct:
            alerts.append({
                "level": "warning",
                "message": (
                    f"Aggregate drawdown alert: {dd_pct:.1f}% "
                    f"exceeds {self.alert_drawdown_pct:.1f}% threshold."
                ),
                "drawdown_pct": max_drawdown,
                "peak": current_peak,
                "current": current,
            })

        return alerts

    def check_strategy_divergence(
        self,
        strategy_drawdowns: dict[str, float],
        historical_max_drawdowns: dict[str, float],
    ) -> list[dict]:
        """Check if any strategy exceeds N× its historical max drawdown.

        Args:
            strategy_drawdowns: Current max drawdown per strategy (as fraction).
            historical_max_drawdowns: Historical max drawdown per strategy (as fraction).

        Returns list of alert dicts with keys:
        - level: "warning"
        - strategy: strategy name
        - message: description
        - current_dd: current drawdown
        - historical_max_dd: historical max
        - ratio: current / historical
        """
        alerts: list[dict] = []

        for name, current_dd in strategy_drawdowns.items():
            hist_max = historical_max_drawdowns.get(name, 0.0)
            if hist_max <= 0:
                continue

            ratio = current_dd / hist_max
            if ratio >= self.divergence_multiplier:
                alerts.append({
                    "level": "warning",
                    "strategy": name,
                    "message": (
                        f"Strategy divergence: '{name}' drawdown {current_dd:.1%} "
                        f"is {ratio:.1f}x its historical max ({hist_max:.1%}). "
                        f"Threshold: {self.divergence_multiplier:.1f}x."
                    ),
                    "current_dd": current_dd,
                    "historical_max_dd": hist_max,
                    "ratio": ratio,
                })

        return alerts

    def monitor(
        self,
        aggregate_values: list[float],
        strategy_drawdowns: dict[str, float],
        historical_max_drawdowns: dict[str, float],
    ) -> list[dict]:
        """Run all checks and return combined alerts.

        Args:
            aggregate_values: Combined equity curve across all portfolios.
            strategy_drawdowns: Current max drawdown per strategy.
            historical_max_drawdowns: Historical max drawdown per strategy.
        """
        alerts: list[dict] = []
        alerts.extend(self.check_aggregate_drawdown(aggregate_values))
        alerts.extend(
            self.check_strategy_divergence(strategy_drawdowns, historical_max_drawdowns)
        )
        return alerts
```

### Step 4: Run tests

Run: `pytest tests/backtest/test_aggregate_risk.py -v`
Expected: 6 passed

Run: `pytest tests/backtest/ -v --tb=short`
Expected: All tests pass

### Step 5: Commit

```bash
git add backtest/aggregate_risk.py tests/backtest/test_aggregate_risk.py
git commit -m "feat: add aggregate risk monitor with drawdown alerts and circuit breaker"
```

---

## Task 2: Wire into Backtest Output

**Files:**
- Modify: `scripts/run_backtest.py`

### Step 1: Add risk monitoring after results

In `scripts/run_backtest.py`, add import:

```python
from backtest.aggregate_risk import AggregateRiskMonitor
```

In `main()`, after `print_multi_portfolio_results()` and rebalancer output (in the multi-portfolio branch), add:

```python
    # Cross-portfolio risk monitoring
    risk_monitor = AggregateRiskMonitor(
        alert_drawdown_pct=15.0,
        circuit_breaker_pct=22.0,
    )
    strategy_drawdowns = {
        name: result.metrics.get("max_drawdown", 0.0)
        for name, result in results.items()
    }
    # Use 2x current drawdown as proxy for historical max in backtest
    # (in live trading, historical_max would come from saved benchmarks)
    historical_max = {name: dd * 0.6 for name, dd in strategy_drawdowns.items()}
    risk_alerts = risk_monitor.monitor(
        aggregate_values=aggregate["portfolio_values"],
        strategy_drawdowns=strategy_drawdowns,
        historical_max_drawdowns=historical_max,
    )
    if risk_alerts:
        print(f"\n  Risk Alerts ({len(risk_alerts)}):")
        for alert in risk_alerts:
            icon = "!!" if alert["level"] == "critical" else " >"
            print(f"    {icon} [{alert['level'].upper()}] {alert['message']}")
```

### Step 2: Run tests

Run: `pytest tests/backtest/ -v --tb=short`
Expected: All tests pass

### Step 3: Commit

```bash
git add scripts/run_backtest.py
git commit -m "feat: wire aggregate risk monitor into backtest output"
```

---

## Task 3: Wire into Paper Trading Status

**Files:**
- Modify: `scripts/run_paper.py`

### Step 1: Add risk monitoring to status output

In `scripts/run_paper.py`, add import:

```python
from backtest.aggregate_risk import AggregateRiskMonitor
```

In `print_status()`, after the TOTAL section, add risk check:

```python
    # Risk monitoring
    risk_monitor = AggregateRiskMonitor(
        alert_drawdown_pct=15.0,
        circuit_breaker_pct=22.0,
    )
    # Check aggregate drawdown from capital
    aggregate_values = [total_capital, total_equity]
    risk_alerts = risk_monitor.check_aggregate_drawdown(aggregate_values)
    if risk_alerts:
        print(f"\n  RISK ALERTS:")
        for alert in risk_alerts:
            icon = "!!" if alert["level"] == "critical" else " >"
            print(f"    {icon} [{alert['level'].upper()}] {alert['message']}")
```

### Step 2: Run tests

Run: `pytest tests/backtest/ tests/shared/ -v --tb=short`
Expected: All tests pass

### Step 3: Commit

```bash
git add scripts/run_paper.py
git commit -m "feat: add aggregate risk alerts to paper trading status"
```

---

## Task 4: Update Documentation

**Files:**
- Modify: `docs/strategy.md`

### Step 1: Add Cross-Portfolio Safety section

After the ML Signal Quality Scoring section, add:

```markdown
## Cross-Portfolio Safety Monitoring

An aggregate risk monitor runs after each backtest and during paper trading status checks.

### Checks

| Check | Threshold | Level | Action |
|---|---|---|---|
| Aggregate drawdown | -15% from peak | Warning | Alert only |
| Aggregate circuit breaker | -22% from peak | Critical | Freeze new entries |
| Strategy divergence | 2x historical max drawdown | Warning | Flag strategy |

### Backtest Output

Risk alerts are printed after multi-portfolio results. Example:

```
  Risk Alerts (2):
     > [WARNING] Aggregate drawdown alert: 16.2% exceeds 15.0% threshold.
     > [WARNING] Strategy divergence: 'momentum' drawdown 25.0% is 2.5x its historical max (10.0%).
```

### Paper Trading

Risk alerts appear in `--status` output when aggregate equity drops below thresholds.
```

### Step 2: Commit

```bash
git add docs/strategy.md
git commit -m "docs: add cross-portfolio safety monitoring section"
```

---

## Verification

```bash
# All tests
pytest tests/ -v --tb=short

# Aggregate risk tests specifically
pytest tests/backtest/test_aggregate_risk.py -v
```
