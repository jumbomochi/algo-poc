from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scripts.collect_sentiment import build_sources, run_collection
from sentiment.sources.base import RawMessage
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
