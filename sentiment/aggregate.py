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

        # Score baseline: message-days only — a ticker's sentiment on days it
        # wasn't mentioned is undefined, not zero, so those days are simply
        # absent from the sample (trailing `baseline_days` ROWS with data).
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
        row.sentiment_zscore = (
            _zscore(mean, score_baseline, min_baseline_days) if mean is not None else None
        )

        # Volume baseline: trailing `baseline_days` SESSIONS (not rows),
        # zero-filling sessions with no row. Rows-only baselines are biased
        # high for sparsely-mentioned tickers (quiet sessions just don't
        # contribute a data point instead of contributing a real zero),
        # which understates how anomalous a spike actually is. The zero-fill
        # window is bounded below by this ticker's first-seen session for
        # this source — sessions before the ticker was ever observed aren't
        # "quiet", they don't exist yet, so they must not be zero-filled.
        first_seen = session.execute(
            select(SentimentDaily.session_date)
            .where(
                SentimentDaily.ticker == ticker,
                SentimentDaily.source == source,
                SentimentDaily.session_date < session_day,
            )
            .order_by(SentimentDaily.session_date.asc())
            .limit(1)
        ).scalar_one_or_none()
        if first_seen is None:
            volume_baseline: list[float] = []
        else:
            # Generous calendar padding (>2x baseline_days) so the trading
            # calendar has more than enough sessions to trim to the trailing
            # `baseline_days`, weekends/holidays included.
            padded_start = session_day - timedelta(days=baseline_days * 2 + 15)
            candidate_sessions = cal.trading_sessions(padded_start, session_day - timedelta(days=1))
            window_sessions = candidate_sessions[-baseline_days:] if candidate_sessions else []
            fill_sessions = [s for s in window_sessions if s >= first_seen]
            counts_by_session = {
                r.session_date: r.message_count
                for r in baseline_rows
                if r.session_date >= (window_sessions[0] if window_sessions else session_day)
            }
            volume_baseline = [float(counts_by_session.get(s, 0)) for s in fill_sessions]
        row.volume_zscore = _zscore(float(len(msgs)), volume_baseline, min_baseline_days)
        upserted += 1

    session.commit()
    return upserted
