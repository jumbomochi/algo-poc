from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from sentiment.sources.base import RawMessage
from shared.models import SentimentCursor, SentimentMessage


def store_messages(session: Session, messages: list[RawMessage], scorer) -> int:
    """Insert new messages, skipping any existing (source, source_id, ticker).

    Every message gets a local score (cheap) alongside any provider score.
    """
    inserted = 0
    now = datetime.now(timezone.utc)
    for msg in messages:
        exists = session.execute(
            select(SentimentMessage.id).where(
                SentimentMessage.source == msg.source,
                SentimentMessage.source_id == msg.source_id,
                SentimentMessage.ticker == msg.ticker,
            )
        ).first()
        if exists:
            continue
        session.add(
            SentimentMessage(
                source=msg.source,
                source_id=msg.source_id,
                ticker=msg.ticker,
                author=msg.author,
                text=msg.text,
                url=msg.url,
                posted_at=msg.posted_at,
                collected_at=now,
                provider_score=msg.provider_score,
                local_score=scorer.score(msg.text),
                score_model=scorer.model_name,
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
