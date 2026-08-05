from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import scripts.collect_sentiment as collect_sentiment
from scripts.collect_sentiment import build_sources, run_collection
from sentiment.sources.base import RawMessage
from sentiment.sources.discord import DiscordSource
from sentiment.store import get_cursor
from shared.config import AppConfig
from shared.models import Base, SentimentMessage

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


class GoodSource:
    name = "good"

    def fetch(self, tickers, since):
        return [
            RawMessage(
                source="good",
                source_id="1",
                ticker="AAPL",
                text="nice",
                posted_at=NOW - timedelta(hours=1),
            )
        ]


class BrokenSource:
    name = "broken"

    def fetch(self, tickers, since):
        raise RuntimeError("API down")


def test_run_collection_stores_and_advances_cursor(session):
    counts = run_collection(session, [GoodSource()], ["AAPL"], now=NOW)
    assert counts == {"good": 1}
    assert session.query(SentimentMessage).count() == 1
    assert get_cursor(session, "good", NOW) == NOW - timedelta(hours=1)


def test_failed_source_is_isolated_and_cursor_unmoved(session):
    default = NOW - timedelta(days=3)
    counts = run_collection(session, [BrokenSource(), GoodSource()], ["AAPL"], now=NOW)
    assert counts == {"broken": 0, "good": 1}
    assert get_cursor(session, "broken", default) == default


def test_source_store_failure_is_isolated_and_cursor_unmoved(session, monkeypatch):
    # A source whose fetch succeeds but whose store step blows up (bad row,
    # DB constraint, whatever) must not wedge collection: other sources
    # still collect, and the failing source's cursor stays put so the gap
    # is visible next cycle. Distinct from a fetch failure — this exercises
    # the store_messages call itself raising.
    real_store_messages = collect_sentiment.store_messages

    def flaky_store_messages(session_, messages, scorer):
        if messages and messages[0].source == "flaky":
            raise RuntimeError("db write failed")
        return real_store_messages(session_, messages, scorer)

    monkeypatch.setattr(collect_sentiment, "store_messages", flaky_store_messages)

    class FlakySource:
        name = "flaky"

        def fetch(self, tickers, since):
            return [
                RawMessage(
                    source="flaky",
                    source_id="1",
                    ticker="AAPL",
                    text="nice",
                    posted_at=NOW - timedelta(hours=1),
                )
            ]

    default = NOW - timedelta(days=3)
    counts = run_collection(session, [FlakySource(), GoodSource()], ["AAPL"], now=NOW)
    assert counts == {"flaky": 0, "good": 1}
    assert get_cursor(session, "flaky", default) == default
    # Only the good source's message made it into the archive.
    assert session.query(SentimentMessage).count() == 1
    assert session.query(SentimentMessage).one().source == "good"


class FakeDiscordResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class FakeDiscordClient:
    """Serves one page of messages per channel, keyed off the channel id
    embedded in the request URL (.../channels/{id}/messages)."""

    def __init__(self, pages_by_channel: dict[str, list]):
        self._pages_by_channel = {k: list(v) for k, v in pages_by_channel.items()}

    def get(self, url, params=None, headers=None):
        channel_id = url.split("/channels/")[1].split("/")[0]
        pages = self._pages_by_channel.get(channel_id, [])
        payload = pages.pop(0) if pages else []
        return FakeDiscordResponse(payload)


def test_discord_channels_advance_independent_cursors(session):
    ch1_msg = {
        "id": "1400000000000000001",
        "content": "$NVDA to the moon",
        "timestamp": "2026-08-01T12:00:00+00:00",
        "author": {"username": "gamma"},
    }
    ch2_msg = {
        "id": "1400000000000000002",
        "content": "$NVDA dip incoming",
        "timestamp": "2026-08-02T09:00:00+00:00",
        "author": {"username": "bear"},
    }
    client = FakeDiscordClient({"111": [[ch1_msg]], "222": [[ch2_msg]]})
    source = DiscordSource("tok123", channel_ids=["111", "222"], http_client=client)

    counts = run_collection(session, [source], ["NVDA"], now=NOW)
    assert counts == {"discord": 2}

    default = NOW - timedelta(days=3)
    cursor_ch1 = get_cursor(session, "discord:111", default)
    cursor_ch2 = get_cursor(session, "discord:222", default)
    assert cursor_ch1 == datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    assert cursor_ch2 == datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc)
    assert cursor_ch1 != cursor_ch2
    # The shared "discord" cursor key is not used at all.
    assert get_cursor(session, "discord", default) == default


def test_build_sources_skips_missing_credentials():
    config = AppConfig()
    config.sentiment.finnhub_news.enabled = True
    config.sentiment.stocktwits.enabled = True
    config.sentiment.reddit.enabled = True
    config.sentiment.discord.enabled = False
    # No FINNHUB_API_KEY / reddit creds in env -> only stocktwits builds
    sources = build_sources(config, env={})
    assert [s.name for s in sources] == ["stocktwits"]


def test_build_sources_with_credentials():
    config = AppConfig()
    config.sentiment.finnhub_news.enabled = True
    config.sentiment.discord.enabled = True
    config.sentiment.discord.channel_ids = ["123"]
    env = {"FINNHUB_API_KEY": "k", "DISCORD_BOT_TOKEN": "t"}
    names = [s.name for s in build_sources(config, env=env)]
    assert "finnhub_news" in names
    assert "discord" in names
