from __future__ import annotations

from datetime import datetime

import httpx

from sentiment.sources.base import RawMessage
from sentiment.tickers import extract_tickers

_DISCORD_EPOCH_MS = 1_420_070_400_000  # 2015-01-01T00:00:00Z
_PAGE_SIZE = 100


def snowflake_for(dt: datetime) -> str:
    """Discord snowflake whose timestamp component equals dt."""
    return str((int(dt.timestamp() * 1000) - _DISCORD_EPOCH_MS) << 22)


class DiscordSource:
    """Incremental channel-history reads via the bot REST API — one-shot,
    no live gateway connection. Only usable on channels where the bot is
    a permitted member (spec open item: user-supplied channel list).
    """

    name = "discord"
    BASE_URL = "https://discord.com/api/v10"

    def __init__(self, bot_token: str, channel_ids: list[str], http_client=None) -> None:
        self._headers = {"Authorization": f"Bot {bot_token}"}
        self.channel_ids = channel_ids
        self._client = http_client or httpx.Client(timeout=30)

    def cursor_key(self, channel_id: str) -> str:
        """Per-channel cursor key (shared/models/sentiment.py documents this
        convention): channels are independent streams with independent
        history, so a stalled channel shouldn't hold back the others behind
        a single shared `discord` cursor."""
        return f"discord:{channel_id}"

    def fetch_channel(self, channel_id: str, tickers: list[str], since: datetime) -> list[RawMessage]:
        universe = set(tickers)
        out: list[RawMessage] = []
        after = snowflake_for(since)
        while True:
            resp = self._client.get(
                f"{self.BASE_URL}/channels/{channel_id}/messages",
                params={"after": after, "limit": _PAGE_SIZE},
                headers=self._headers,
            )
            resp.raise_for_status()
            batch = sorted(resp.json(), key=lambda m: int(m["id"]))
            if not batch:
                break
            for item in batch:
                posted = datetime.fromisoformat(item["timestamp"])
                for ticker in extract_tickers(item.get("content", ""), universe):
                    out.append(
                        RawMessage(
                            source=self.name,
                            source_id=f"{channel_id}:{item['id']}",
                            ticker=ticker,
                            text=item["content"],
                            posted_at=posted,
                            author=(item.get("author") or {}).get("username"),
                            provider_score=None,
                            meta={"channel_id": channel_id},
                        )
                    )
            after = batch[-1]["id"]
            if len(batch) < _PAGE_SIZE:
                break
        return out

    def fetch(self, tickers: list[str], since: datetime) -> list[RawMessage]:
        """Single-cursor fetch across all channels — satisfies
        SentimentSourceProtocol for callers that don't need per-channel
        cursors. `scripts/collect_sentiment.py` uses `fetch_channel` directly
        instead, so each channel's cursor advances independently.
        """
        out: list[RawMessage] = []
        for channel_id in self.channel_ids:
            out.extend(self.fetch_channel(channel_id, tickers, since))
        return out
