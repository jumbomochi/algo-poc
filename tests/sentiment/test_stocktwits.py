from __future__ import annotations

from datetime import datetime, timezone

from sentiment.sources.stocktwits import StockTwitsSource

SINCE = datetime(2026, 8, 1, tzinfo=timezone.utc)

PAYLOAD = {
    "messages": [
        {
            "id": 555001,
            "body": "$AAPL breaking out",
            "created_at": "2026-08-02T14:30:00Z",
            "user": {"username": "bulls_r_us"},
            "entities": {"sentiment": {"basic": "Bullish"}},
            "likes": {"total": 7},
        },
        {
            "id": 555002,
            "body": "$AAPL no opinion",
            "created_at": "2026-08-02T15:00:00Z",
            "user": {"username": "neutral_nick"},
            "entities": {"sentiment": None},
        },
        {
            "id": 555000,
            "body": "old message",
            "created_at": "2026-07-20T10:00:00Z",
            "user": {"username": "old_timer"},
            "entities": {"sentiment": {"basic": "Bearish"}},
        },
    ]
}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, responses):
        # responses: dict url-substring -> FakeResponse
        self._responses = responses
        self.calls = []

    def get(self, url, params=None):
        self.calls.append(url)
        for fragment, response in self._responses.items():
            if fragment in url:
                return response
        return FakeResponse({}, status_code=404)


def test_fetch_maps_sentiment_and_filters_since():
    client = FakeClient({"AAPL.json": FakeResponse(PAYLOAD)})
    source = StockTwitsSource(http_client=client)
    msgs = source.fetch(["AAPL"], since=SINCE)
    assert len(msgs) == 2
    bullish = next(m for m in msgs if m.source_id == "555001")
    assert bullish.provider_score == 1.0
    assert bullish.author == "bulls_r_us"
    assert bullish.meta["likes"] == 7
    untagged = next(m for m in msgs if m.source_id == "555002")
    assert untagged.provider_score is None


def test_unknown_symbol_404_is_skipped():
    client = FakeClient({"AAPL.json": FakeResponse(PAYLOAD)})
    source = StockTwitsSource(http_client=client)
    msgs = source.fetch(["ZZZZ", "AAPL"], since=SINCE)
    assert {m.ticker for m in msgs} == {"AAPL"}
