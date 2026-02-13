from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Any

import numpy as np


@dataclass
class AlertMessage:
    """Advisory alert from correlation monitoring.

    These alerts are informational only and never block trades.
    """

    message: str
    priority: str  # "low" | "medium" | "high"
    context: dict[str, Any] | None = None


class CorrelationMonitor:
    """Monitors portfolio correlation risk — advisory only.

    Checks:
    - Portfolio beta vs market (SPY proxy)
    - Pairwise correlation between positions
    - Minimum lookback requirement (default 60 days)

    Never blocks trades; only produces AlertMessage instances.
    """

    def check_portfolio_beta(
        self,
        positions: dict[str, dict[str, Any]],
        market_returns: np.ndarray,
        position_returns: dict[str, np.ndarray],
    ) -> float:
        """Calculate value-weighted portfolio beta vs market.

        Args:
            positions: Dict of ticker -> position info with ``value`` key.
            market_returns: Array of market (SPY) returns.
            position_returns: Dict of ticker -> return series.

        Returns:
            Portfolio beta as a float.
        """
        total_value = sum(
            pos.get("value", 0) for pos in positions.values()
        )
        if total_value <= 0:
            return 0.0

        market_var = np.var(market_returns, ddof=1)
        if market_var == 0:
            return 0.0

        portfolio_beta = 0.0
        for ticker, pos_info in positions.items():
            if ticker not in position_returns:
                continue
            weight = pos_info.get("value", 0) / total_value
            returns = position_returns[ticker]
            cov = np.cov(returns, market_returns, ddof=1)[0, 1]
            beta = cov / market_var
            portfolio_beta += weight * beta

        return float(portfolio_beta)

    def check_pairwise_correlation(
        self,
        position_returns: dict[str, np.ndarray],
        threshold: float = 0.7,
    ) -> list[tuple[str, str, float]]:
        """Find pairs of positions with correlation exceeding threshold.

        Args:
            position_returns: Dict of ticker -> return series.
            threshold: Minimum absolute correlation to flag.

        Returns:
            List of (ticker_a, ticker_b, correlation) tuples.
        """
        flagged: list[tuple[str, str, float]] = []
        tickers = list(position_returns.keys())

        for ticker_a, ticker_b in combinations(tickers, 2):
            returns_a = position_returns[ticker_a]
            returns_b = position_returns[ticker_b]
            corr_matrix = np.corrcoef(returns_a, returns_b)
            corr = float(corr_matrix[0, 1])

            if abs(corr) >= threshold:
                flagged.append((ticker_a, ticker_b, corr))

        return flagged

    def check(
        self,
        positions: dict[str, dict[str, Any]],
        market_returns: np.ndarray,
        position_returns: dict[str, np.ndarray],
        beta_threshold: float = 1.5,
        correlation_threshold: float = 0.7,
        min_lookback_days: int = 60,
    ) -> list[AlertMessage]:
        """Run all correlation checks and produce advisory alerts.

        Args:
            positions: Dict of ticker -> position info with ``value`` key.
            market_returns: Array of market returns.
            position_returns: Dict of ticker -> return series.
            beta_threshold: Portfolio beta above this triggers alert.
            correlation_threshold: Pairwise correlation above this triggers alert.
            min_lookback_days: Minimum data points required.

        Returns:
            List of AlertMessage instances (advisory only).
        """
        alerts: list[AlertMessage] = []

        # Check minimum lookback requirement
        data_length = len(market_returns) if len(market_returns) > 0 else 0
        if data_length < min_lookback_days:
            alerts.append(
                AlertMessage(
                    message=(
                        f"Insufficient data for correlation analysis: "
                        f"{data_length} days available, {min_lookback_days} required. "
                        f"Skipping correlation checks."
                    ),
                    priority="low",
                    context={
                        "available_days": data_length,
                        "required_days": min_lookback_days,
                    },
                )
            )
            return alerts

        # Check portfolio beta
        beta = self.check_portfolio_beta(positions, market_returns, position_returns)
        if abs(beta) > beta_threshold:
            alerts.append(
                AlertMessage(
                    message=(
                        f"Portfolio beta {beta:.2f} exceeds threshold "
                        f"{beta_threshold:.2f}. Consider diversifying."
                    ),
                    priority="high" if abs(beta) > beta_threshold * 1.5 else "medium",
                    context={"beta": beta, "threshold": beta_threshold},
                )
            )

        # Check pairwise correlations
        correlated_pairs = self.check_pairwise_correlation(
            position_returns, threshold=correlation_threshold
        )
        for ticker_a, ticker_b, corr in correlated_pairs:
            alerts.append(
                AlertMessage(
                    message=(
                        f"High correlation ({corr:.2f}) between {ticker_a} and "
                        f"{ticker_b} exceeds threshold {correlation_threshold:.2f}."
                    ),
                    priority="medium",
                    context={
                        "pair": (ticker_a, ticker_b),
                        "correlation": corr,
                        "threshold": correlation_threshold,
                    },
                )
            )

        return alerts
