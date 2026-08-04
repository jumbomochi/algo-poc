from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from sentiment.sources.base import RawMessage
from sentiment.store import get_cursor, set_cursor, store_messages
from shared.models import Base, SentimentMessage

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


class FakeScorer:
    model_name = "fake"

    def score(self, text: str) -> float:
        return 0.5


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def make_raw(source_id: str = "m1", provider_score: float | None = None) -> RawMessage:
    return RawMessage(
        source="stocktwits",
        source_id=source_id,
        ticker="AAPL",
        text="great quarter",
        posted_at=NOW,
        author="joe",
        provider_score=provider_score,
        meta={"likes": 2},
    )


def test_store_inserts_and_scores(session):
    n = store_messages(session, [make_raw(provider_score=1.0)], FakeScorer())
    assert n == 1
    row = session.query(SentimentMessage).one()
    assert row.provider_score == 1.0
    assert row.local_score == 0.5
    assert row.score_model == "fake"
    assert row.collected_at is not None


def test_store_is_idempotent(session):
    store_messages(session, [make_raw()], FakeScorer())
    n = store_messages(session, [make_raw(), make_raw("m2")], FakeScorer())
    assert n == 1
    assert session.query(SentimentMessage).count() == 2


def test_cursor_default_then_roundtrip(session):
    default = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert get_cursor(session, "reddit", default) == default
    set_cursor(session, "reddit", NOW)
    assert get_cursor(session, "reddit", default) == NOW


def test_store_truncates_oversized_url(session):
    # A 600-char url would overflow the sentiment_messages.url VARCHAR(500)
    # column on Postgres and abort the whole batch commit (SQLite, used in
    # this test, doesn't enforce the length at all — the defensive
    # truncation in store_messages is what actually protects production).
    long_url = "https://example.com/" + ("a" * 600)
    msg = RawMessage(
        source="stocktwits",
        source_id="m-long-url",
        ticker="AAPL",
        text="great quarter",
        posted_at=NOW,
        url=long_url,
    )
    n = store_messages(session, [msg], FakeScorer())
    assert n == 1
    row = session.query(SentimentMessage).one()
    assert len(row.url) == 500
    assert row.url == long_url[:500]
