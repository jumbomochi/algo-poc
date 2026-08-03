from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.market_calendar import MarketCalendar
from shared.models import SentimentDaily, SentimentMessage

ET = ZoneInfo("America/New_York")


def session_date_for(posted_at: datetime, cal: MarketCalendar) -> date:
    """The NYSE session this message can inform: the session whose close is
    the next close at or after posted_at. After-close and weekend messages
    roll forward — the look-ahead guard for later backtests."""
    probe = posted_at
    close = cal.get_next_market_close(probe)
    while close < posted_at:
        probe = probe + timedelta(days=1)
        close = cal.get_next_market_close(probe)
    return close.astimezone(ET).date()


def best_score(provider_score: float | None, local_score: float | None) -> float | None:
    return provider_score if provider_score is not None else local_score


def _zscore(value: float, baseline: list[float], min_n: int) -> float | None:
    if len(baseline) < min_n:
        return None
    mean = statistics.fmean(baseline)
    std = statistics.pstdev(baseline)
    if std == 0:
        return None
    return (value - mean) / std


def rebuild_daily(
    session: Session,
    cal: MarketCalendar,
    start: date,
    end: date,
    baseline_days: int = 60,
    min_baseline_days: int = 20,
) -> int:
    """Upsert SentimentDaily for sessions in [start, end]; never deletes.

    Messages are pulled from a padded posted_at window (weekends/holidays
    roll forward, so pad the left edge) and bucketed by session_date_for.
    """
    window_start = datetime(start.year, start.month, start.day, tzinfo=timezone.utc) - timedelta(days=5)
    window_end = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)
    rows = session.execute(
        select(SentimentMessage).where(
            SentimentMessage.posted_at >= window_start,
            SentimentMessage.posted_at <= window_end,
        )
    ).scalars().all()

    groups: dict[tuple[str, date, str], list[SentimentMessage]] = {}
    for msg in rows:
        # SQLite (used in tests) doesn't round-trip tzinfo on DateTime(timezone=True)
        # columns and hands back naive datetimes; posted_at is always written as UTC
        # (see sentiment/store.py), so treat a naive value as UTC rather than mutate
        # the ORM instance (which would dirty and rewrite the row on next flush).
        posted_at = msg.posted_at if msg.posted_at.tzinfo is not None else msg.posted_at.replace(tzinfo=timezone.utc)
        session_day = session_date_for(posted_at, cal)
        if not (start <= session_day <= end):
            continue
        groups.setdefault((msg.ticker, session_day, msg.source), []).append(msg)

    now = datetime.now(timezone.utc)
    upserted = 0
    for (ticker, session_day, source), msgs in sorted(groups.items(), key=lambda kv: kv[0][1]):
        scores = [s for s in (best_score(m.provider_score, m.local_score) for m in msgs) if s is not None]
        weights = [
            1 + (m.meta or {}).get("likes", 0)
            for m in msgs
            if best_score(m.provider_score, m.local_score) is not None
        ]
        mean = statistics.fmean(scores) if scores else None
        weighted = (
            sum(s * w for s, w in zip(scores, weights)) / sum(weights) if scores else None
        )
        std = statistics.pstdev(scores) if len(scores) > 1 else (0.0 if scores else None)

        existing = session.execute(
            select(SentimentDaily).where(
                SentimentDaily.ticker == ticker,
                SentimentDaily.session_date == session_day,
                SentimentDaily.source == source,
            )
        ).scalar_one_or_none()
        row = existing or SentimentDaily(ticker=ticker, session_date=session_day, source=source)
        row.message_count = len(msgs)
        row.mean_score = mean
        row.weighted_score = weighted
        row.score_std = std
        row.unique_authors = len({m.author for m in msgs if m.author})
        row.computed_at = now
        if existing is None:
            session.add(row)
        session.flush()

        baseline_rows = session.execute(
            select(SentimentDaily)
            .where(
                SentimentDaily.ticker == ticker,
                SentimentDaily.source == source,
                SentimentDaily.session_date < session_day,
            )
            .order_by(SentimentDaily.session_date.desc())
            .limit(baseline_days)
        ).scalars().all()
        score_baseline = [r.mean_score for r in baseline_rows if r.mean_score is not None]
        volume_baseline = [float(r.message_count) for r in baseline_rows]
        row.sentiment_zscore = (
            _zscore(mean, score_baseline, min_baseline_days) if mean is not None else None
        )
        row.volume_zscore = _zscore(float(len(msgs)), volume_baseline, min_baseline_days)
        upserted += 1

    session.commit()
    return upserted
