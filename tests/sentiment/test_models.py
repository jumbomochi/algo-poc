from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.models import Base, SentimentDaily, SentimentMessage, SentimentCursor

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def make_message(source_id: str = "m1", ticker: str = "AAPL") -> SentimentMessage:
    return SentimentMessage(
        source="stocktwits",
        source_id=source_id,
        ticker=ticker,
        author="trader_joe",
        text="$AAPL to the moon",
        url="https://stocktwits.com/x/1",
        posted_at=NOW,
        collected_at=NOW,
        provider_score=1.0,
        local_score=0.6,
        score_model="vader",
        meta={"likes": 3},
    )


def test_sentiment_message_roundtrip(session):
    session.add(make_message())
    session.commit()
    row = session.query(SentimentMessage).one()
    assert row.ticker == "AAPL"
    assert row.meta == {"likes": 3}
    assert row.provider_score == 1.0


def test_sentiment_message_unique_constraint(session):
    session.add(make_message())
    session.commit()
    session.add(make_message())  # same (source, source_id, ticker)
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_message_different_ticker_allowed(session):
    session.add(make_message(ticker="AAPL"))
    session.add(make_message(ticker="MSFT"))
    session.commit()
    assert session.query(SentimentMessage).count() == 2


def test_sentiment_daily_unique_constraint(session):
    def make_daily():
        return SentimentDaily(
            ticker="AAPL",
            session_date=date(2026, 8, 3),
            source="reddit",
            message_count=5,
            mean_score=0.2,
            weighted_score=0.3,
            score_std=0.1,
            unique_authors=4,
            sentiment_zscore=None,
            volume_zscore=None,
            computed_at=NOW,
        )

    session.add(make_daily())
    session.commit()
    session.add(make_daily())
    with pytest.raises(IntegrityError):
        session.commit()


def test_cursor_roundtrip(session):
    session.add(SentimentCursor(key="discord:123", position=NOW.isoformat(), updated_at=NOW))
    session.commit()
    row = session.get(SentimentCursor, "discord:123")
    assert row.position == NOW.isoformat()
