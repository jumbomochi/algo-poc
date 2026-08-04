#!/usr/bin/env python3
"""One-shot sentiment collection cycle (launchd-scheduled hourly).

For each enabled source with available credentials: read the cursor,
fetch messages since it, score + store them (idempotent), and advance the
cursor to the newest posted_at seen. A failing source is logged and
skipped; its cursor stays put so the gap remains visible. Afterwards,
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

import structlog
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
from shared.market_calendar import MarketCalendar
from shared.universe import ACTIVE_SLEEVES, get_union_universe

logger = structlog.get_logger("collect_sentiment")

DEFAULT_LOOKBACK = timedelta(days=3)


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


def run_collection(
    session: Session, sources: list, tickers: list[str], now: datetime
) -> dict[str, int]:
    scorer = VaderScorer()
    counts: dict[str, int] = {}
    for source in sources:
        since = get_cursor(session, source.name, default=now - DEFAULT_LOOKBACK)
        try:
            messages = source.fetch(tickers, since)
        except Exception:
            logger.exception("source_fetch_failed", source=source.name)
            counts[source.name] = 0
            continue
        inserted = store_messages(session, messages, scorer)
        counts[source.name] = inserted
        if messages:
            newest = max(m.posted_at for m in messages)
            set_cursor(session, source.name, max(newest, since))
        logger.info(
            "source_collected",
            source=source.name,
            fetched=len(messages),
            inserted=inserted,
        )
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
