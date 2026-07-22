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
        pending = self.pending_orders.get(ticker)
        return pending is not None and pending.action.lower() == action.lower()
