from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class RawMessage:
    """One (message, ticker) pair from any source. posted_at is tz-aware UTC."""

    source: str
    source_id: str
    ticker: str
    text: str
    posted_at: datetime
    author: str | None = None
    url: str | None = None
    provider_score: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class SentimentSourceProtocol(Protocol):
    name: str

    def fetch(self, tickers: list[str], since: datetime) -> list[RawMessage]: ...
