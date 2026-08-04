from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from sentiment.sources.base import RawMessage
from shared.models import SentimentCursor, SentimentMessage

# Column capacities from shared/models/sentiment.py — truncate defensively
# before insert rather than rely on the DB to reject an oversized value
# (SQLite, used in tests, doesn't enforce VARCHAR(n) at all, and even on
# Postgres a single bad row would otherwise abort the whole batch commit and
# wedge every other message in the same collection cycle).
_URL_MAX = 500
_AUTHOR_MAX = 100
_SOURCE_ID_MAX = 120
_SOURCE_MAX = 20
_TICKER_MAX = 10
_SCORE_MODEL_MAX = 50


def _truncate(value: str | None, max_len: int) -> str | None:
    if value is None:
        return None
    return value[:max_len]


def store_messages(session: Session, messages: list[RawMessage], scorer) -> int:
    """Insert new messages, skipping any existing (source, source_id, ticker).

    Every message gets a local score (cheap) alongside any provider score.
    """
    inserted = 0
    now = datetime.now(timezone.utc)
    for msg in messages:
        # Truncate the unique-constraint fields before the dedup lookup too,
        # so the check compares against what will actually be stored — a
        # re-poll of the same over-long source_id must still dedup cleanly.
        source = _truncate(msg.source, _SOURCE_MAX)
        source_id = _truncate(msg.source_id, _SOURCE_ID_MAX)
        ticker = _truncate(msg.ticker, _TICKER_MAX)
        exists = session.execute(
            select(SentimentMessage.id).where(
                SentimentMessage.source == source,
                SentimentMessage.source_id == source_id,
                SentimentMessage.ticker == ticker,
            )
        ).first()
        if exists:
            continue
        session.add(
            SentimentMessage(
                source=source,
                source_id=source_id,
                ticker=ticker,
                author=_truncate(msg.author, _AUTHOR_MAX),
                text=msg.text,
                url=_truncate(msg.url, _URL_MAX),
                posted_at=msg.posted_at,
                collected_at=now,
                provider_score=msg.provider_score,
                local_score=scorer.score(msg.text),
                score_model=_truncate(scorer.model_name, _SCORE_MODEL_MAX),
                meta=dict(msg.meta),
            )
        )
        inserted += 1
    session.commit()
    return inserted


def get_cursor(session: Session, key: str, default: datetime) -> datetime:
    row = session.get(SentimentCursor, key)
    if row is None:
        return default
    return datetime.fromisoformat(row.position)


def set_cursor(session: Session, key: str, position: datetime) -> None:
    row = session.get(SentimentCursor, key)
    now = datetime.now(timezone.utc)
    if row is None:
        session.add(SentimentCursor(key=key, position=position.isoformat(), updated_at=now))
    else:
        row.position = position.isoformat()
        row.updated_at = now
    session.commit()
