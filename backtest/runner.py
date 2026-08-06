from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable

from backtest.metrics import BacktestMetrics
from backtest.simulator import SimulatedExecutor
from research.shadow import CandidateObserver
from shared.universe import MembershipCalendar


logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """Container for backtest output."""

    trades: list[dict] = field(default_factory=list)
    portfolio_values: list[float] = field(default_factory=list)
    dates: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    shadow_candidates: list[dict] = field(default_factory=list)


class BacktestRunner:
    """Replays historical data day-by-day through a signal/risk pipeline.

    Tracks portfolio state, positions, and trades. Uses SimulatedExecutor
    for order fills.

    **Execution model: next-bar fills.** Signals are computed from bars up to
    and including ``close[t]``, and the resulting orders are queued for the
    following session — entries fill against ``open[t+1]`` (or the limit price
    if only the intraday low reaches it) and exits fill at ``open[t+1]``.
    Entries are day orders: an unreachable limit expires with the session
    rather than resting. This replaced same-bar filling, which let a decision
    taken on the close trade at that same day's low (entries) or that same
    day's open (exits) — findings 4.2 and 4.3 of the 2026-08-06 review.

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
        trade_start_date: Any = None,
        *,
        candidate_observer: CandidateObserver | None = None,
        portfolio_name: str = "",
        membership: MembershipCalendar | None = None,
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
            trade_start_date: If set, only allow new buy entries on or after this
                date. Earlier bars are still fed to signals_fn for indicator
                warm-up. Sell signals for existing positions are always processed.
            candidate_observer: Optional observational hook for raw buy candidates.
            portfolio_name: Portfolio identity supplied to candidate_observer.
            membership: Optional point-in-time universe. When supplied, a ticker
                only produces signals on dates when it was a member, and an open
                position in a ticker that leaves the universe (index removal or
                delisting) is exited at the next open. When omitted the whole of
                ``bars_by_ticker`` is treated as tradable on every date, which
                carries survivorship bias if those bars came from a
                present-day ticker list.

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

        # Orders decided on the previous session, waiting for the next open.
        pending_entries: dict[str, _PendingEntry] = {}
        pending_exits: dict[str, str] = {}  # ticker -> exit_reason
        # Last close seen per ticker, so a position in a name that stops
        # printing bars (delisting, halt) is marked at its final price rather
        # than frozen at cost.
        last_close: dict[str, float] = {}

        for current_date in all_dates:
            # --- Phase 1: fill orders queued yesterday, at today's open. ---
            # Exits settle before entries so freed cash is available the same
            # session, matching how the live sleeves sequence their orders.
            for ticker, exit_reason in list(pending_exits.items()):
                bar = bars_by_date.get((ticker, current_date))
                if bar is None:
                    # The ticker did not trade today; a market exit stays live
                    # until it can actually be worked.
                    continue
                del pending_exits[ticker]
                lots = positions.pop(ticker, None)
                if not lots:
                    continue
                for lot in lots:
                    fill = self.executor.fill_market_exit(
                        quantity=lot.quantity, bar=bar, ticker=ticker
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
                        "exit_reason": exit_reason,
                    })

            for ticker, order in list(pending_entries.items()):
                bar = bars_by_date.get((ticker, current_date))
                if bar is None:
                    # No session for this ticker; the day order could not have
                    # been working, so it carries to its next session.
                    continue
                # Day-order semantics: this is the order's only session.
                del pending_entries[ticker]
                fill = self.executor.try_fill_limit_entry(
                    limit_price=order.limit_price,
                    quantity=order.quantity,
                    bar=bar,
                    ticker=ticker,
                )
                if fill is None:
                    continue

                cash -= fill["fill_price"] * fill["quantity"] + fill["commission"]
                positions.setdefault(ticker, []).append(
                    _Lot(
                        ticker=ticker,
                        quantity=fill["quantity"],
                        entry_price=fill["fill_price"],
                        entry_date=fill["date"],
                        entry_commission=fill["commission"],
                        entry_signals=order.entry_signals,
                    )
                )

            # --- Phase 2: decide, using bars up to and including today's close. ---
            for ticker in bars_by_ticker:
                bar = bars_by_date.get((ticker, current_date))
                if bar is None:
                    continue

                bars_history[ticker].append(bar)
                last_close[ticker] = bar["close"]

                if membership is not None and not membership.contains(ticker, current_date):
                    # Out of the point-in-time universe: not tradable, and any
                    # holding has to be liquidated (index removal / delisting).
                    if ticker in positions and ticker not in pending_exits:
                        pending_exits[ticker] = "universe_removal"
                    pending_entries.pop(ticker, None)
                    continue

                # Get signal for this ticker
                signal = signals_fn(ticker, bars_history[ticker])
                if signal is None:
                    continue

                action = signal.get("action")

                if action == "sell" and ticker in positions:
                    pending_exits.setdefault(
                        ticker, signal.get("exit_reason", "unknown")
                    )

                elif action == "buy":
                    # Skip new entries before trade_start_date
                    if trade_start_date is not None and current_date < trade_start_date:
                        continue
                    limit_price = signal["limit_price"]
                    quantity = signal["quantity"]
                    sector = signal.get("sector", "Unknown")

                    # A queued entry is a committed lot and committed cash that
                    # has not settled yet — both have to be visible to risk.
                    pending_entry = pending_entries.get(ticker)
                    existing_lots = len(positions.get(ticker, [])) + (
                        1 if pending_entry is not None else 0
                    )
                    portfolio_state = _make_simple_portfolio(
                        cash, positions, bars_by_date, current_date, last_close
                    )
                    decision = risk_engine.check_entry(
                        ticker, quantity, limit_price, sector, portfolio_state,
                        existing_lots=existing_lots,
                        reserved_notional=sum(
                            order.notional for order in pending_entries.values()
                        ),
                    )

                    if candidate_observer is not None:
                        try:
                            signal_snapshot = deepcopy(signal)
                            candidate_observer.observe(
                                portfolio=portfolio_name,
                                ticker=ticker,
                                as_of=current_date,
                                signal=signal_snapshot,
                                risk_approved=bool(decision.approved),
                                risk_reason=str(decision.reason),
                            )
                        except Exception:
                            logger.exception(
                                "Research shadow observer failed; trading result is unchanged"
                            )

                    if not decision.approved:
                        continue

                    pending_entries[ticker] = _PendingEntry(
                        limit_price=limit_price,
                        quantity=decision.adjusted_quantity,
                        entry_signals=signal.get("signals", {}),
                    )

            # End of day: compute portfolio value and update peak prices
            nav = cash
            for ticker, lots in positions.items():
                bar = bars_by_date.get((ticker, current_date))
                mark = bar["close"] if bar is not None else last_close.get(ticker)
                for lot in lots:
                    if mark is None:
                        nav += lot.entry_price * lot.quantity
                        continue
                    nav += mark * lot.quantity
                    if bar is not None:
                        lot.peak_price = max(lot.peak_price, mark)
            portfolio_values.append(nav)
            dates.append(current_date)

        # Compute metrics
        metrics = BacktestMetrics.compute(
            portfolio_values=portfolio_values,
            trades=trades,
        )
        try:
            shadow_records = getattr(candidate_observer, "records", [])
            shadow_candidates = [record.to_dict() for record in shadow_records]
        except Exception:
            logger.exception(
                "Research shadow export failed; trading result is unchanged"
            )
            shadow_candidates = []

        return BacktestResult(
            trades=trades,
            portfolio_values=portfolio_values,
            dates=dates,
            metrics=metrics,
            shadow_candidates=shadow_candidates,
        )


@dataclass
class _PendingEntry:
    """A limit buy decided on one session's close, working the next session."""

    limit_price: float
    quantity: float
    entry_signals: dict = field(default_factory=dict)

    @property
    def notional(self) -> float:
        return self.limit_price * self.quantity


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
    last_close: dict[str, float] | None = None,
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
        bar = bars_by_date.get((ticker, current_date))
        if bar is not None:
            mark = bar["close"]
        else:
            mark = (last_close or {}).get(ticker)
        for lot in lots:
            nav += (mark if mark is not None else lot.entry_price) * lot.quantity

    # Exposure = market value / nav. Was hard-coded 0.0, which silently
    # disabled the RiskEngine's total-exposure limit in every backtest —
    # sleeves could lever internally (cash below zero) bounded only by
    # per-position caps and max-lots.
    market_value = nav - cash
    exposure_pct = (market_value / nav) * 100.0 if nav > 0 else 0.0

    return SimplePortfolioState(
        nav=nav,
        peak_nav=nav,
        positions=all_positions,
        sector_exposure={},
        total_exposure_pct=exposure_pct,
        margin_utilization_pct=0.0,
    )
