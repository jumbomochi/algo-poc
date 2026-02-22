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
        """Check if any strategy exceeds N x its historical max drawdown.

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
