from __future__ import annotations

from datetime import datetime, timezone

from sentiment.sources.base import RawMessage
from sentiment.tickers import extract_tickers


class RedditSource:
    """New posts from configured subreddits via a PRAW Reddit instance.

    Reddit keeps deep listing history, so daily polling is enough; the
    posts_per_subreddit limit bounds each cycle.
    """

    name = "reddit"

    def __init__(self, reddit, subreddits: list[str], posts_per_subreddit: int = 100) -> None:
        self._reddit = reddit
        self._subreddits = subreddits
        self._limit = posts_per_subreddit

    def fetch(self, tickers: list[str], since: datetime) -> list[RawMessage]:
        universe = set(tickers)
        out: list[RawMessage] = []
        for sub_name in self._subreddits:
            for post in self._reddit.subreddit(sub_name).new(limit=self._limit):
                posted = datetime.fromtimestamp(post.created_utc, tz=timezone.utc)
                if posted <= since:
                    continue
                text = post.title
                if post.selftext:
                    text = f"{post.title}\n{post.selftext}"
                for ticker in extract_tickers(text, universe):
                    out.append(
                        RawMessage(
                            source=self.name,
                            source_id=post.id,
                            ticker=ticker,
                            text=text,
                            posted_at=posted,
                            author=str(post.author) if post.author else None,
                            url=f"https://reddit.com{post.permalink}",
                            provider_score=None,
                            meta={"subreddit": sub_name, "likes": post.score},
                        )
                    )
        return out
