from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def optional_str(value: Any) -> str | None:
    """A reported string, or None when IB left the field empty."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def optional_float(value: Any) -> float | None:
    """A reported price, or None when IB reports its "unset" sentinel.

    IB fills numeric order fields it has no value for with ``DBL_MAX`` rather
    than leaving them absent (observed on ``trailingPercent`` during the KAN-18
    spike). Read literally that is a price of 1.8e308, so anything not finite
    is reported as absent — otherwise KAN-20's verifier would read an
    unprotected position as protected at an unreachable level.
    """
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


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
