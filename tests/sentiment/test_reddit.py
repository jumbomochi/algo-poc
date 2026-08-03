from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from sentiment.sources.reddit import RedditSource

SINCE = datetime(2026, 8, 1, tzinfo=timezone.utc)


def make_post(post_id, title, selftext, created_utc, score=10):
    return SimpleNamespace(
        id=post_id,
        title=title,
        selftext=selftext,
        author="deep_value",
        permalink=f"/r/stocks/comments/{post_id}/x/",
        score=score,
        created_utc=created_utc,
    )


class FakeSubreddit:
    def __init__(self, posts):
        self._posts = posts

    def new(self, limit=100):
        return iter(self._posts[:limit])


class FakeReddit:
    def __init__(self, posts_by_sub):
        self._posts_by_sub = posts_by_sub

    def subreddit(self, name):
        return FakeSubreddit(self._posts_by_sub[name])


def test_fetch_extracts_tickers_and_filters_since():
    posts = [
        make_post("p1", "$TSLA and NVDA both printing", "", 1785657600),  # 2026-08-02
        make_post("p2", "old $TSLA post", "", 1785024000),  # 2026-07-26 — dropped
        make_post("p3", "no tickers here", "just vibes", 1785657600),
    ]
    reddit = FakeReddit({"stocks": posts})
    source = RedditSource(reddit, subreddits=["stocks"])
    msgs = source.fetch(["TSLA", "NVDA"], since=SINCE)
    assert {(m.source_id, m.ticker) for m in msgs} == {("p1", "TSLA"), ("p1", "NVDA")}
    msg = msgs[0]
    assert msg.source == "reddit"
    assert msg.meta["subreddit"] == "stocks"
    assert msg.meta["likes"] == 10
    assert msg.url.startswith("https://reddit.com/")
    assert msg.provider_score is None
