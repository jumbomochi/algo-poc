from __future__ import annotations

from datetime import datetime, timezone

from sentiment.sources.finnhub_news import FinnhubNewsSource

SINCE = datetime(2026, 8, 1, tzinfo=timezone.utc)

PAYLOAD = [
    {
        "category": "company",
        "datetime": 1785657600,  # 2026-08-02 08:00:00 UTC
        "headline": "Apple beats on earnings",
        "id": 999001,
        "related": "AAPL",
        "source": "Reuters",
        "summary": "Strong iPhone quarter.",
        "url": "https://example.com/apple",
    },
    {
        "category": "company",
        "datetime": 1785024000,  # 2026-07-26 — before `since`, must be dropped
        "headline": "Old news",
        "id": 999000,
        "related": "AAPL",
        "source": "Reuters",
        "summary": "",
        "url": "https://example.com/old",
    },
]


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
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return FakeResponse(self._payload)


def test_fetch_maps_payload_and_filters_since():
    client = FakeClient(PAYLOAD)
    source = FinnhubNewsSource(api_key="k", http_client=client)
    msgs = source.fetch(["AAPL"], since=SINCE)
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.source == "finnhub_news"
    assert msg.source_id == "999001"
    assert msg.ticker == "AAPL"
    assert "Apple beats on earnings" in msg.text
    assert "Strong iPhone quarter." in msg.text
    assert msg.provider_score is None
    assert msg.posted_at == datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)


def test_symbol_mapping_for_class_shares():
    client = FakeClient([])
    source = FinnhubNewsSource(api_key="k", http_client=client)
    source.fetch(["BRK B"], since=SINCE)
    _, params = client.calls[0]
    assert params["symbol"] == "BRK.B"
