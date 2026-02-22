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
