"""Position sources for signal-function unit tests (KAN-116).

A sleeve's signal function reads what it holds from a ``PortfolioContext`` —
the live book, or a ``RunnerBook`` the replay's runner refreshes from its own
fills. It no longer keeps a private record of what it asked to buy, so a unit
test states the book explicitly: empty for an entry, or holding the lot a
runner would hold after filling an earlier buy.
"""
from __future__ import annotations

from datetime import date

from backtest.portfolio_context import HeldPosition, PendingOrder, PortfolioContext


def context(
    positions: dict[str, HeldPosition] | None = None,
    pending: dict[str, PendingOrder] | None = None,
    *,
    budget: float = 100_000,
) -> PortfolioContext:
    return PortfolioContext(
        positions=positions or {},
        pending_orders=pending or {},
        sleeve_budget=budget,
        reserved_notional=0,
    )


def empty_context() -> PortfolioContext:
    """A book holding nothing: every ticker is evaluated for entry."""
    return context()


def holding(
    ticker: str,
    *,
    entry_price: float,
    entry_date: date,
    peak_price: float | None = None,
    quantity: float = 1.0,
) -> PortfolioContext:
    return context({
        ticker: HeldPosition(
            quantity=quantity,
            avg_entry_price=entry_price,
            peak_price=entry_price if peak_price is None else peak_price,
            entry_date=entry_date,
        )
    })


def filled(
    ticker: str,
    bars: list[dict],
    *,
    signal_index: int,
    decision_index: int,
    quantity: float = 1.0,
) -> PortfolioContext:
    """The book a ``BacktestRunner`` holds when deciding on ``bars[decision_index]``
    after the buy signalled on ``bars[signal_index]`` filled at the next open.

    The runner raises a lot's peak at each close *after* that session's
    decisions, so the peak seen on the decision bar covers the fill price and
    every close from the fill session up to the bar before it — the signal
    function then takes ``max(peak, today's close)`` itself, as live does.
    """
    fill = bars[signal_index + 1]
    peak = max(
        [fill["open"]]
        + [b["close"] for b in bars[signal_index + 1:decision_index]]
    )
    return holding(
        ticker,
        entry_price=fill["open"],
        entry_date=fill["date"],
        peak_price=peak,
        quantity=quantity,
    )
