from __future__ import annotations

from datetime import datetime, timezone

from sentiment.sources.discord import DiscordSource, snowflake_for

SINCE = datetime(2026, 8, 1, tzinfo=timezone.utc)

PAGE = [
    {
        "id": "1400000000000000001",
        "content": "loading up on $NVDA calls",
        "timestamp": "2026-08-02T13:00:00+00:00",
        "author": {"username": "gamma_gang"},
    },
    {
        "id": "1400000000000000002",
        "content": "nothing ticker related",
        "timestamp": "2026-08-02T13:05:00+00:00",
        "author": {"username": "lurker"},
    },
]


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class FakeClient:
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
        return FakeResponse(self._pages.pop(0) if self._pages else [])


def test_snowflake_roundtrip_ordering():
    early = snowflake_for(datetime(2026, 8, 1, tzinfo=timezone.utc))
    late = snowflake_for(datetime(2026, 8, 2, tzinfo=timezone.utc))
    assert int(late) > int(early)


def test_fetch_extracts_tickers_and_authenticates():
    client = FakeClient([PAGE])
    source = DiscordSource("tok123", channel_ids=["999"], http_client=client)
    msgs = source.fetch(["NVDA"], since=SINCE)
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.source_id == "999:1400000000000000001"
    assert msg.ticker == "NVDA"
    assert msg.author == "gamma_gang"
    _, params, headers = client.calls[0]
    assert headers["Authorization"] == "Bot tok123"
    assert params["after"] == snowflake_for(SINCE)
