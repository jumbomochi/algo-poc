from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from backtest.metrics import BacktestMetrics
from backtest.simulator import SimulatedExecutor


@dataclass
class BacktestResult:
    """Container for backtest output."""

    trades: list[dict] = field(default_factory=list)
    portfolio_values: list[float] = field(default_factory=list)
    dates: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


class BacktestRunner:
    """Replays historical data day-by-day through a signal/risk pipeline.

    Tracks portfolio state, positions, and trades. Uses SimulatedExecutor
    for order fills.

    Usage:
        runner = BacktestRunner(executor=executor, initial_capital=100_000)
        result = runner.run(bars_by_ticker, signals_fn, risk_engine)
    """

    def __init__(
        self,
        executor: SimulatedExecutor,
        initial_capital: float = 100_000.0,
    ) -> None:
        self.executor = executor
        self.initial_capital = initial_capital

    def run(
        self,
        bars_by_ticker: dict[str, list[dict]],
        signals_fn: Callable[[str, list[dict]], dict | None],
        risk_engine: Any,
    ) -> BacktestResult:
        """Run a backtest over the provided bar data.

        Args:
            bars_by_ticker: Map of ticker -> list of bar dicts (sorted by date).
                Each bar has keys: date, open, high, low, close.
            signals_fn: Callable(ticker, bars_so_far) -> signal dict or None.
                Signal dict keys: action ("buy"/"sell"), ticker, limit_price,
                quantity, sector (for buys).
            risk_engine: Object with check_entry(ticker, quantity, price,
                sector, portfolio) -> decision with .approved, .adjusted_quantity.

        Returns:
            BacktestResult with trades, portfolio_values, and metrics.
        """
        cash = self.initial_capital
        positions: dict[str, list[_Lot]] = {}
        trades: list[dict] = []
        portfolio_values: list[float] = [self.initial_capital]
        dates: list = []

        # Collect all unique dates across tickers, sorted
        all_dates = _collect_sorted_dates(bars_by_ticker)

        # Build date-indexed bar lookups for each ticker
        bars_by_date: dict[str, dict] = {}
        bars_history: dict[str, list[dict]] = {t: [] for t in bars_by_ticker}
        for ticker, bars in bars_by_ticker.items():
            for bar in bars:
                bars_by_date[(ticker, bar["date"])] = bar

        for current_date in all_dates:
            # Process each ticker for this day
            for ticker in bars_by_ticker:
                bar = bars_by_date.get((ticker, current_date))
                if bar is None:
                    continue

                bars_history[ticker].append(bar)

                # Get signal for this ticker
                signal = signals_fn(ticker, bars_history[ticker])
                if signal is None:
                    continue

                action = signal.get("action")

                if action == "sell" and ticker in positions:
                    # Exit all lots for this ticker
                    lots = positions.pop(ticker)
                    for lot in lots:
                        fill = self.executor.fill_market_exit(
                            quantity=lot.quantity, bar=bar
                        )
                        exit_value = fill["fill_price"] * fill["quantity"]
                        entry_value = lot.entry_price * lot.quantity
                        pnl = exit_value - entry_value - lot.entry_commission - fill["commission"]
                        cash += exit_value - fill["commission"]

                        trades.append({
                            "ticker": ticker,
                            "entry_date": lot.entry_date,
                            "exit_date": fill["date"],
                            "entry_price": lot.entry_price,
                            "exit_price": fill["fill_price"],
                            "quantity": lot.quantity,
                            "pnl": pnl,
                            "entry_commission": lot.entry_commission,
                            "exit_commission": fill["commission"],
                            "entry_signals": lot.entry_signals,
                            "exit_reason": signal.get("exit_reason", "unknown"),
                        })

                elif action == "buy":
                    # Check risk with existing lot count
                    existing_lots = positions.get(ticker, [])
                    limit_price = signal["limit_price"]
                    quantity = signal["quantity"]
                    sector = signal.get("sector", "Unknown")

                    portfolio_state = _make_simple_portfolio(
                        cash, positions, bars_by_date, current_date
                    )
                    decision = risk_engine.check_entry(
                        ticker, quantity, limit_price, sector, portfolio_state,
                        existing_lots=len(existing_lots),
                    )

                    if not decision.approved:
                        continue

                    adjusted_qty = decision.adjusted_quantity
                    fill = self.executor.try_fill_limit_entry(
                        limit_price=limit_price,
                        quantity=adjusted_qty,
                        bar=bar,
                    )

                    if fill is None:
                        continue

                    cost = fill["fill_price"] * fill["quantity"] + fill["commission"]
                    cash -= cost
                    lot = _Lot(
                        ticker=ticker,
                        quantity=fill["quantity"],
                        entry_price=fill["fill_price"],
                        entry_date=fill["date"],
                        entry_commission=fill["commission"],
                        entry_signals=signal.get("signals", {}),
                    )
                    if ticker not in positions:
                        positions[ticker] = []
                    positions[ticker].append(lot)

            # End of day: compute portfolio value and update peak prices
            nav = cash
            for ticker, lots in positions.items():
                bar = bars_by_date.get((ticker, current_date))
                for lot in lots:
                    if bar is not None:
                        nav += bar["close"] * lot.quantity
                        lot.peak_price = max(lot.peak_price, bar["close"])
                    else:
                        nav += lot.entry_price * lot.quantity
            portfolio_values.append(nav)
            dates.append(current_date)

        # Compute metrics
        metrics = BacktestMetrics.compute(
            portfolio_values=portfolio_values,
            trades=trades,
        )

        return BacktestResult(
            trades=trades,
            portfolio_values=portfolio_values,
            dates=dates,
            metrics=metrics,
        )


@dataclass
class _Lot:
    """Individual lot within a position."""

    ticker: str
    quantity: float
    entry_price: float
    entry_date: Any
    entry_commission: float
    entry_signals: dict = field(default_factory=dict)
    peak_price: float = 0.0

    def __post_init__(self):
        if self.peak_price == 0.0:
            self.peak_price = self.entry_price


def _collect_sorted_dates(bars_by_ticker: dict[str, list[dict]]) -> list:
    """Collect all unique dates across tickers, sorted chronologically."""
    dates = set()
    for bars in bars_by_ticker.values():
        for bar in bars:
            dates.add(bar["date"])
    return sorted(dates)


def _make_simple_portfolio(
    cash: float,
    positions: dict[str, list[_Lot]],
    bars_by_date: dict,
    current_date: Any,
) -> Any:
    """Build a minimal portfolio-like object for risk engine calls.

    The risk engine expects a portfolio with nav, peak_nav, positions,
    sector_exposure, total_exposure_pct, margin_utilization_pct.
    For backtesting we provide simplified values.
    """
    from backtest._portfolio_state import SimplePortfolioState

    nav = cash
    all_positions = {}
    for ticker, lots in positions.items():
        total_qty = sum(lot.quantity for lot in lots)
        all_positions[ticker] = {"quantity": total_qty}
        for lot in lots:
            bar = bars_by_date.get((ticker, current_date))
            price = bar["close"] if bar else lot.entry_price
            nav += price * lot.quantity

    return SimplePortfolioState(
        nav=nav,
        peak_nav=nav,
        positions=all_positions,
        sector_exposure={},
        total_exposure_pct=0.0,
        margin_utilization_pct=0.0,
    )
