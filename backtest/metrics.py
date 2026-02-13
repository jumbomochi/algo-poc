from __future__ import annotations

import math


class BacktestMetrics:
    """Computes performance metrics from backtest results."""

    @staticmethod
    def compute(
        portfolio_values: list[float],
        trades: list[dict],
        benchmark_values: list[float] | None = None,
    ) -> dict:
        """Compute performance metrics from portfolio values and trades.

        Args:
            portfolio_values: Daily portfolio values (at least 2 entries).
            trades: List of trade dicts with keys: pnl, entry_date, exit_date.
            benchmark_values: Optional benchmark price series for comparison.

        Returns:
            Dict with keys: total_return, sharpe_ratio, max_drawdown,
            win_rate, avg_holding_period_days, total_trades.
            If benchmark_values provided, also includes benchmark_return.
        """
        total_return = _total_return(portfolio_values)
        sharpe = _sharpe_ratio(portfolio_values)
        max_dd = _max_drawdown(portfolio_values)
        win_rate = _win_rate(trades)
        avg_holding = _avg_holding_period_days(trades)
        total_trades = len(trades)

        result: dict = {
            "total_return": total_return,
            "sharpe_ratio": sharpe,
            "max_drawdown": max_dd,
            "win_rate": win_rate,
            "avg_holding_period_days": avg_holding,
            "total_trades": total_trades,
        }

        if benchmark_values is not None:
            result["benchmark_return"] = _total_return(benchmark_values)

        return result


def _total_return(values: list[float]) -> float:
    """Calculate total return as (final - initial) / initial."""
    if len(values) < 2 or values[0] == 0:
        return 0.0
    return (values[-1] - values[0]) / values[0]


def _sharpe_ratio(
    portfolio_values: list[float],
    annualization_factor: float = 252.0,
    risk_free_rate: float = 0.0,
) -> float:
    """Compute annualized Sharpe ratio from daily portfolio values.

    Uses daily returns, subtracts daily risk-free rate, then annualizes.
    """
    if len(portfolio_values) < 2:
        return 0.0

    daily_returns: list[float] = []
    for i in range(1, len(portfolio_values)):
        prev = portfolio_values[i - 1]
        if prev == 0:
            continue
        daily_returns.append((portfolio_values[i] - prev) / prev)

    if not daily_returns:
        return 0.0

    daily_rf = risk_free_rate / annualization_factor
    excess_returns = [r - daily_rf for r in daily_returns]

    n = len(excess_returns)
    mean = sum(excess_returns) / n
    variance = sum((r - mean) ** 2 for r in excess_returns) / n
    std = math.sqrt(variance)

    if std == 0.0:
        return 0.0

    return (mean / std) * math.sqrt(annualization_factor)


def _max_drawdown(values: list[float]) -> float:
    """Calculate maximum drawdown as a fraction (0.0 to 1.0).

    Drawdown is measured as (peak - trough) / peak.
    """
    if len(values) < 2:
        return 0.0

    peak = values[0]
    max_dd = 0.0

    for v in values[1:]:
        if v > peak:
            peak = v
        else:
            dd = (peak - v) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd

    return max_dd


def _win_rate(trades: list[dict]) -> float:
    """Calculate win rate as fraction of trades with positive pnl."""
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return wins / len(trades)


def _avg_holding_period_days(trades: list[dict]) -> float:
    """Calculate average holding period in calendar days."""
    if not trades:
        return 0.0
    total_days = sum(
        (t["exit_date"] - t["entry_date"]).days for t in trades
    )
    return total_days / len(trades)
