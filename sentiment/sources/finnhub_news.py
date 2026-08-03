from __future__ import annotations

from datetime import datetime, timezone

import httpx

from sentiment.sources.base import RawMessage


class FinnhubNewsSource:
    """Company news via Finnhub /company-news. Free tier: ~60 calls/min.

    The free response carries no sentiment score, so provider_score is None
    and the store's local scorer rates headline+summary.
    """

    name = "finnhub_news"
    BASE_URL = "https://finnhub.io/api/v1/company-news"

    def __init__(self, api_key: str, http_client=None) -> None:
        self._api_key = api_key
        self._client = http_client or httpx.Client(timeout=30)

    def fetch(self, tickers: list[str], since: datetime) -> list[RawMessage]:
        out: list[RawMessage] = []
        today = datetime.now(timezone.utc).date()
        for ticker in tickers:
            symbol = ticker.replace(" ", ".")  # "BRK B" -> "BRK.B"
            resp = self._client.get(
                self.BASE_URL,
                params={
                    "symbol": symbol,
                    "from": since.date().isoformat(),
                    "to": today.isoformat(),
                    "token": self._api_key,
                },
            )
            resp.raise_for_status()
            for item in resp.json():
                posted = datetime.fromtimestamp(item["datetime"], tz=timezone.utc)
                if posted < since:
                    continue
                text = item["headline"]
                if item.get("summary"):
                    text = f"{text}. {item['summary']}"
                out.append(
                    RawMessage(
                        source=self.name,
                        source_id=str(item["id"]),
                        ticker=ticker,
                        text=text,
                        posted_at=posted,
                        url=item.get("url"),
                        provider_score=None,
                        meta={"news_source": item.get("source")},
                    )
                )
        return out
