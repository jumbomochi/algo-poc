from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

import httpx

from sentiment.sources.base import RawMessage


class FinnhubNewsSource:
    """Company news via Finnhub /company-news. Free tier: 60 calls/min.

    The free response carries no sentiment score, so provider_score is None
    and the store's local scorer rates headline+summary.

    One call per ticker, so a cycle over the ~140-ticker universe must be
    paced: unpaced, the first live cycle (2026-10-18) hit 429 after about 17
    seconds, and because one failure fails the source, it stored nothing.
    Calls are spaced ``min_interval_s`` apart (55/min at the default), and a
    429 that still arrives is waited out (its Retry-After, else
    ``retry_wait_s``) up to ``max_retries`` times before the source fails.
    """

    name = "finnhub_news"
    BASE_URL = "https://finnhub.io/api/v1/company-news"

    def __init__(
        self,
        api_key: str,
        http_client=None,
        *,
        min_interval_s: float = 1.1,
        retry_wait_s: float = 30.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api_key = api_key
        self._client = http_client or httpx.Client(timeout=30)
        self._min_interval_s = min_interval_s
        self._retry_wait_s = retry_wait_s
        self._max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None

    def _get(self, params: dict):
        retries = 0
        while True:
            if self._last_call is not None:
                wait = self._min_interval_s - (self._clock() - self._last_call)
                if wait > 0:
                    self._sleep(wait)
            self._last_call = self._clock()
            resp = self._client.get(self.BASE_URL, params=params)
            if resp.status_code != 429 or retries >= self._max_retries:
                resp.raise_for_status()
                return resp
            retries += 1
            self._sleep(_retry_after(resp, self._retry_wait_s))

    def fetch(self, tickers: list[str], since: datetime) -> list[RawMessage]:
        out: list[RawMessage] = []
        today = datetime.now(timezone.utc).date()
        for ticker in tickers:
            symbol = ticker.replace(" ", ".")  # "BRK B" -> "BRK.B"
            resp = self._get(
                {
                    "symbol": symbol,
                    "from": since.date().isoformat(),
                    "to": today.isoformat(),
                    "token": self._api_key,
                }
            )
            for item in resp.json():
                posted = datetime.fromtimestamp(item["datetime"], tz=timezone.utc)
                if posted <= since:
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


def _retry_after(resp, default: float) -> float:
    headers = getattr(resp, "headers", None) or {}
    try:
        return max(float(headers.get("Retry-After", default)), 0.0)
    except (TypeError, ValueError):
        return default
