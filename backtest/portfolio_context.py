from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class HeldPosition:
    quantity: float
    avg_entry_price: float
    peak_price: float
    entry_date: date


@dataclass(frozen=True)
class PendingOrder:
    ticker: str
    action: str
    quantity: float
    limit_price: float | None
    recommendation_id: str


@dataclass(frozen=True)
class PortfolioContext:
    positions: Mapping[str, HeldPosition]
    pending_orders: Mapping[str, PendingOrder]
    sleeve_budget: float
    reserved_notional: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "positions", MappingProxyType(dict(self.positions)))
        object.__setattr__(
            self, "pending_orders", MappingProxyType(dict(self.pending_orders))
        )

    def has_pending(self, ticker: str, action: str) -> bool:
        return self.pending_quantity(ticker, action) > 0

    def pending_quantity(self, ticker: str, action: str) -> float:
        """Return uncovered active quantity for one symbol and side."""
        return sum(
            order.quantity
            for order in self.pending_orders.values()
            if order.ticker == ticker and order.action.lower() == action.lower()
        )


class RunnerBook:
    """KAN-116: a replay's own book, in the shape live hands a sleeve.

    Live builds one :class:`PortfolioContext` per sleeve per day from the real
    book (``PaperTradingState.build_portfolio_context``) and passes it to the
    signal factory, so every decision reads positions that actually filled.
    A replay cannot rebuild its signal functions every session (each factory
    precomputes rankings over the whole bar history), so it hands them this
    stand-in instead and :class:`backtest.runner.BacktestRunner` re-binds it to
    a fresh ``PortfolioContext`` of its own lots before each session's
    decisions. The signal functions then run the exact branch live runs.

    Reading it before a runner has bound it raises: a sleeve wired to a book
    the runner does not refresh would see an empty book forever and re-buy
    every session, which must fail loudly rather than trade.
    """

    def __init__(self) -> None:
        self._context: PortfolioContext | None = None

    def bind(self, context: PortfolioContext) -> None:
        self._context = context

    def _require(self) -> PortfolioContext:
        if self._context is None:
            raise RuntimeError(
                "RunnerBook read before BacktestRunner.run(book=...) bound it; "
                "pass the sleeve's book to the runner that replays it"
            )
        return self._context

    @property
    def positions(self) -> Mapping[str, HeldPosition]:
        return self._require().positions

    @property
    def pending_orders(self) -> Mapping[str, PendingOrder]:
        return self._require().pending_orders

    @property
    def sleeve_budget(self) -> float:
        return self._require().sleeve_budget

    @property
    def reserved_notional(self) -> float:
        return self._require().reserved_notional

    def has_pending(self, ticker: str, action: str) -> bool:
        return self._require().has_pending(ticker, action)

    def pending_quantity(self, ticker: str, action: str) -> float:
        return self._require().pending_quantity(ticker, action)
