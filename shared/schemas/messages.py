from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class StreamSerializable(BaseModel):
    def to_stream_dict(self) -> dict[str, str]:
        data = self.model_dump(mode="json")
        result: dict[str, str] = {}
        for k, v in data.items():
            if v is None:
                # Omit None fields: str(None) wires the string "None", which
                # fails validation on read (e.g. the limit_price of a market
                # sell) — omission lets the pydantic default apply instead.
                continue
            if isinstance(v, str):
                result[k] = v
            elif isinstance(v, (dict, list)):
                result[k] = json.dumps(v)
            else:
                result[k] = str(v)
        return result

    @classmethod
    def from_stream_dict(cls, data: dict[str, str]):
        parsed: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, str) and v.startswith(("{", "[")):
                try:
                    parsed[k] = json.loads(v)
                except (json.JSONDecodeError, ValueError):
                    parsed[k] = v
            else:
                parsed[k] = v
        return cls.model_validate(parsed)


class MarketDataMessage(StreamSerializable):
    ticker: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class FundamentalMessage(StreamSerializable):
    ticker: str
    timestamp: datetime
    metric_type: str
    data: dict[str, Any]
    effective_at: datetime
    ingested_at: datetime
    source_revision: str


class EventMessage(StreamSerializable):
    ticker: str
    timestamp: datetime
    event_type: str
    data: dict[str, Any]
    sentiment_score: float | None = None
    effective_at: datetime
    ingested_at: datetime
    source_revision: str


class SignalMessage(StreamSerializable):
    ticker: str
    timestamp: datetime
    signal_name: str
    signal_value: float  # -1.0 to 1.0
    confidence: float    # 0.0 to 1.0
    computed_at: datetime


class RecommendationMessage(StreamSerializable):
    ticker: str
    timestamp: datetime
    action: Literal["buy", "sell", "hold"]
    confidence: float
    top_features: dict[str, float]
    recommendation_id: str
    # Sleeve-strategy bridge fields (run_paper --publish). None when the
    # recommendation comes from the ML path; when present, the risk service
    # uses the sleeve's own sizing/pricing instead of re-deriving them.
    limit_price: float | None = None
    quantity: float | None = None
    portfolio: str | None = None


class ApprovedOrderMessage(StreamSerializable):
    ticker: str
    timestamp: datetime
    action: Literal["buy", "sell"]
    # float: the system trades fractional shares (IBKR Share Slices)
    quantity: float
    order_type: Literal["limit", "market"]
    limit_price: float | None = None
    recommendation_id: str
    risk_adjustments: dict[str, Any] = Field(default_factory=dict)
    portfolio: str | None = None


class FillMessage(StreamSerializable):
    ticker: str
    timestamp: datetime
    side: Literal["buy", "sell"]
    # float: the system trades fractional shares (IBKR Share Slices)
    quantity: float
    fill_price: float
    commission: float
    commission_currency: str | None = None
    commission_trading: float | None = None
    commission_fx_base_per_trading: float | None = None
    recommendation_id: str
    order_id: str
    execution_id: str | None = None
    account_id: str | None = None
    cumulative_quantity: float | None = None
    portfolio: str | None = None
    con_id: int | None = None
    exchange: str | None = None
    currency: str | None = None


class AlertMessage(StreamSerializable):
    timestamp: datetime
    event_type: str
    priority: Literal["low", "medium", "high", "critical"]
    message: str
    context: dict[str, Any] = Field(default_factory=dict)


class KillMessage(StreamSerializable):
    timestamp: datetime
    triggered_by: str
    reason: str
