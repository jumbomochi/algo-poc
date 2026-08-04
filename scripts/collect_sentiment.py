#!/usr/bin/env python3
"""One-shot sentiment collection cycle (launchd-scheduled hourly).

For each enabled source with available credentials: read the cursor,
fetch messages since it, score + store them (idempotent), and advance the
cursor to the newest posted_at seen. A failing source (fetch OR store) is
logged and skipped; its cursor stays put so the gap remains visible, and
the remaining sources plus the aggregation rebuild still proceed. Discord
uses a per-channel cursor (`discord:<channel_id>`) instead of one shared
`discord` cursor, since channels are independent streams. Afterwards,
rebuild the last N sessions of sentiment_daily.

Usage:
    python scripts/collect_sentiment.py
    python scripts/collect_sentiment.py --config config/default.yaml --aggregate-days 5
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from sentiment.aggregate import rebuild_daily
from sentiment.scoring import VaderScorer
from sentiment.sources.discord import DiscordSource
from sentiment.sources.finnhub_news import FinnhubNewsSource
from sentiment.sources.reddit import RedditSource
from sentiment.sources.stocktwits import StockTwitsSource
from sentiment.store import get_cursor, set_cursor, store_messages
from shared.config import AppConfig, load_config
from shared.logging import get_logger
from shared.market_calendar import MarketCalendar
from shared.universe import ACTIVE_SLEEVES, get_union_universe

logger = get_logger("collect_sentiment")

DEFAULT_LOOKBACK = timedelta(days=3)
# Cursors are read with an hour of overlap: a message that lands after the
# previous cycle already advanced the cursor past its true posted_at (clock
# skew, provider-side indexing lag) would otherwise never be re-fetched. The
# (source, source_id, ticker) unique constraint in sentiment/store.py makes
# re-fetching the overlap a no-op, so the slack is free.
REFETCH_SLACK = timedelta(hours=1)


def build_sources(config: AppConfig, env: dict[str, str]) -> list:
    sources: list = []
    cfg = config.sentiment
    if cfg.finnhub_news.enabled:
        if env.get("FINNHUB_API_KEY"):
            sources.append(FinnhubNewsSource(api_key=env["FINNHUB_API_KEY"]))
        else:
            logger.warning("source_skipped_missing_creds", source="finnhub_news")
    if cfg.stocktwits.enabled:
        sources.append(StockTwitsSource())
    if cfg.reddit.enabled:
        if env.get("REDDIT_CLIENT_ID") and env.get("REDDIT_CLIENT_SECRET"):
            import praw

            reddit = praw.Reddit(
                client_id=env["REDDIT_CLIENT_ID"],
                client_secret=env["REDDIT_CLIENT_SECRET"],
                user_agent=env.get("REDDIT_USER_AGENT", "algo-poc-sentiment/0.1"),
            )
            sources.append(
                RedditSource(
                    reddit,
                    subreddits=cfg.reddit.subreddits,
                    posts_per_subreddit=cfg.reddit.posts_per_subreddit,
                )
            )
        else:
            logger.warning("source_skipped_missing_creds", source="reddit")
    if cfg.discord.enabled:
        if env.get("DISCORD_BOT_TOKEN") and cfg.discord.channel_ids:
            sources.append(
                DiscordSource(env["DISCORD_BOT_TOKEN"], channel_ids=cfg.discord.channel_ids)
            )
        else:
            logger.warning("source_skipped_missing_creds", source="discord")
    return sources


def _collect_one(
    session: Session,
    scorer: VaderScorer,
    fetch_label: str,
    cursor_key: str,
    since: datetime,
    fetch,
) -> int | None:
    """Fetch + store + advance-cursor for one (source, cursor_key) unit.

    Returns the inserted count, or None if the unit failed — fetch and store
    failures are both caught here (and here alone) so a bad row from one
    source/channel can never wedge the others or the aggregation rebuild
    that follows. store_messages runs a partial `session.add()` batch before
    its own commit, so a failure there must roll back before the loop moves
    on, or the session would carry a dirty, uncommittable state into the
    next source.
    """
    try:
        messages = fetch(since)
    except Exception:
        logger.exception("source_fetch_failed", source=fetch_label)
        return None
    try:
        inserted = store_messages(session, messages, scorer)
        if messages:
            newest = max(m.posted_at for m in messages)
            set_cursor(session, cursor_key, max(newest, since))
    except Exception:
        session.rollback()
        logger.exception("source_store_failed", source=fetch_label)
        return None
    logger.info(
        "source_collected",
        source=fetch_label,
        fetched=len(messages),
        inserted=inserted,
    )
    return inserted


def run_collection(
    session: Session, sources: list, tickers: list[str], now: datetime
) -> dict[str, int]:
    scorer = VaderScorer()
    counts: dict[str, int] = {}
    for source in sources:
        if isinstance(source, DiscordSource):
            total = 0
            for channel_id in source.channel_ids:
                cursor_key = source.cursor_key(channel_id)
                since = get_cursor(session, cursor_key, default=now - DEFAULT_LOOKBACK) - REFETCH_SLACK
                result = _collect_one(
                    session,
                    scorer,
                    fetch_label=f"{source.name}:{channel_id}",
                    cursor_key=cursor_key,
                    since=since,
                    fetch=lambda s, channel_id=channel_id: source.fetch_channel(channel_id, tickers, s),
                )
                total += result or 0
            counts[source.name] = total
            continue
        since = get_cursor(session, source.name, default=now - DEFAULT_LOOKBACK) - REFETCH_SLACK
        result = _collect_one(
            session,
            scorer,
            fetch_label=source.name,
            cursor_key=source.name,
            since=since,
            fetch=lambda s: source.fetch(tickers, s),
        )
        counts[source.name] = result or 0
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/default.yaml")
    parser.add_argument("--aggregate-days", type=int, default=5)
    args = parser.parse_args()

    config = load_config(args.config)
    engine = create_engine(config.database.url)
    tickers = get_union_universe(ACTIVE_SLEEVES)
    now = datetime.now(timezone.utc)
    cal = MarketCalendar()

    with Session(engine) as session:
        sources = build_sources(config, env=dict(os.environ))
        if not sources:
            logger.error("no_sources_enabled")
            return 1
        counts = run_collection(session, sources, tickers, now)
        end = date.today()
        start = end - timedelta(days=args.aggregate_days + 4)
        upserted = rebuild_daily(
            session,
            cal,
            start,
            end,
            baseline_days=config.sentiment.zscore_baseline_days,
            min_baseline_days=config.sentiment.zscore_min_baseline_days,
        )
    logger.info("collection_cycle_done", counts=counts, daily_rows=upserted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
