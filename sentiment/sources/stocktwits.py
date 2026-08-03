from __future__ import annotations

from datetime import datetime

import httpx

from sentiment.sources.base import RawMessage

_SENTIMENT_MAP = {"Bullish": 1.0, "Bearish": -1.0}


class StockTwitsSource:
    """Per-symbol public stream. Returns only the last ~30 messages per
    symbol, so this source needs roughly hourly polling during US market
    hours (launchd job in Task 14). ~200 req/hr unauthenticated.
    """

    name = "stocktwits"
    BASE_URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"

    def __init__(self, http_client=None) -> None:
        self._client = http_client or httpx.Client(timeout=30)

    def fetch(self, tickers: list[str], since: datetime) -> list[RawMessage]:
        out: list[RawMessage] = []
        for ticker in tickers:
            symbol = ticker.replace(" ", ".")
            resp = self._client.get(self.BASE_URL.format(symbol=symbol))
            if resp.status_code == 404:  # symbol not on StockTwits
                continue
            resp.raise_for_status()
            for item in resp.json().get("messages", []):
                posted = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                if posted <= since:
                    continue
                sentiment = (item.get("entities") or {}).get("sentiment") or {}
                likes = (item.get("likes") or {}).get("total", 0)
                out.append(
                    RawMessage(
                        source=self.name,
                        source_id=str(item["id"]),
                        ticker=ticker,
                        text=item["body"],
                        posted_at=posted,
                        author=(item.get("user") or {}).get("username"),
                        url=f"https://stocktwits.com/message/{item['id']}",
                        provider_score=_SENTIMENT_MAP.get(sentiment.get("basic")),
                        meta={"likes": likes},
                    )
                )
        return out
