from __future__ import annotations

from shared.config import AppConfig, load_config


def test_sentiment_defaults():
    config = AppConfig()
    assert config.sentiment.finnhub_news.enabled is False
    assert config.sentiment.reddit.subreddits == ["wallstreetbets", "stocks", "investing"]
    assert config.sentiment.reddit.posts_per_subreddit == 100
    assert config.sentiment.discord.channel_ids == []
    assert config.sentiment.zscore_baseline_days == 60
    assert config.sentiment.zscore_min_baseline_days == 20


def test_default_yaml_enables_phase1_sources():
    config = load_config("config/default.yaml")
    assert config.sentiment.finnhub_news.enabled is True
    assert config.sentiment.stocktwits.enabled is True
    assert config.sentiment.reddit.enabled is True
    # Discord waits for a confirmed server/channel list (spec open item)
    assert config.sentiment.discord.enabled is False
