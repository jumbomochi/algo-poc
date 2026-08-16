from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class BrokerPosition:
    account_id: str
    con_id: int
    symbol: str
    quantity: float
    average_cost: float | None = None
    exchange: str | None = None
    currency: str | None = None


@dataclass(frozen=True)
class BrokerOpenOrder:
    account_id: str
    ib_order_id: str
    con_id: int
    symbol: str
    action: str
    total_quantity: float
    filled_quantity: float
    status: str
    # What kind of order this actually is (KAN-19). Without these three a
    # reader cannot tell a resting protective stop from a working entry, which
    # is the question KAN-20's verification has to answer. Optional so every
    # existing construction still builds, and None where IB did not report it.
    order_type: str | None = None
    aux_price: float | None = None
    tif: str | None = None

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.total_quantity - self.filled_quantity)


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    account_id: str
    mode: str
    base_currency: str
    trading_currency: str
    net_liquidation_base: float
    fx_base_per_trading: float
    net_liquidation_trading_equivalent: float
    settled_cash_trading: float
    fx_source: str
    fx_captured_at: datetime
    positions: dict[int, BrokerPosition] = field(default_factory=dict)
    open_orders: dict[str, BrokerOpenOrder] = field(default_factory=dict)
    captured_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
