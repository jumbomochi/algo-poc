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

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
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
    _, params, _ = client.calls[0]
    assert params["symbol"] == "BRK.B"


def test_the_key_is_a_header_never_in_the_url():
    """A failed request's exception message carries the full URL, and the
    collector logs that traceback: the first live cycle wrote the key into
    ~/ibc/logs/sentiment-collect.log that way."""
    client = FakeClient([])
    FinnhubNewsSource(api_key="secret-key", http_client=client).fetch(["AAPL"], since=SINCE)
    url, params, headers = client.calls[0]
    assert "secret-key" not in url
    assert "secret-key" not in str(params)
    assert headers == {"X-Finnhub-Token": "secret-key"}


class ScriptedClient:
    """Answers each call with the next status in ``statuses``."""

    def __init__(self, statuses, headers=None):
        self._statuses = list(statuses)
        self._headers = headers or {}
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append(params["symbol"])
        response = FakeResponse([], status_code=self._statuses.pop(0))
        response.headers = self._headers
        return response


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


def _source(client, fake_time, **kwargs):
    return FinnhubNewsSource(
        api_key="k", http_client=client, sleep=fake_time.sleep, clock=fake_time.clock, **kwargs
    )


def test_calls_are_paced_under_the_free_tier_limit():
    """The first live cycle sent ~140 calls unpaced and was refused with 429
    after about 17 seconds."""
    fake_time = FakeTime()
    client = ScriptedClient([200, 200, 200])
    _source(client, fake_time).fetch(["AAPL", "MSFT", "XLU"], since=SINCE)
    assert client.calls == ["AAPL", "MSFT", "XLU"]
    assert fake_time.sleeps == [1.1, 1.1]  # none before the first call


def test_a_429_is_waited_out_and_retried():
    fake_time = FakeTime()
    client = ScriptedClient([429, 200], headers={"Retry-After": "7"})
    _source(client, fake_time).fetch(["AAPL"], since=SINCE)
    assert client.calls == ["AAPL", "AAPL"]
    assert 7.0 in fake_time.sleeps


def test_a_persistent_429_still_fails_the_source():
    fake_time = FakeTime()
    client = ScriptedClient([429, 429, 429])
    try:
        _source(client, fake_time, max_retries=2, retry_wait_s=5).fetch(["AAPL"], since=SINCE)
    except RuntimeError as exc:
        assert "429" in str(exc)
    else:
        raise AssertionError("a persistent 429 must fail the source")
    assert client.calls == ["AAPL"] * 3


def test_a_huge_or_unparseable_retry_after_is_bounded():
    for header, expected in (("3600", 60.0), ("inf", 30.0), ("nan", 30.0), ("Wed, 21 Oct 2026 07:28:00 GMT", 30.0)):
        fake_time = FakeTime()
        client = ScriptedClient([429, 200], headers={"Retry-After": header})
        _source(client, fake_time).fetch(["AAPL"], since=SINCE)
        assert fake_time.sleeps == [expected], header


def test_a_cycle_past_its_deadline_fails_the_source():
    """launchd never overlaps runs, so an overrun would skip the next cycle."""
    fake_time = FakeTime()
    client = ScriptedClient([200] * 5)
    source = _source(client, fake_time, min_interval_s=10, deadline_s=25)
    try:
        source.fetch(["A", "B", "C", "D", "E"], since=SINCE)
    except RuntimeError as exc:
        assert "exceeded 25s" in str(exc)
    else:
        raise AssertionError("an overrunning cycle must fail")
    assert client.calls == ["A", "B", "C"]
