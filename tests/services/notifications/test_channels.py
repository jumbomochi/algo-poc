from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.notifications.channels import (
    TELEGRAM_API_BASE,
    TELEGRAM_MAX_BODY,
    TelegramChannel,
)
from services.notifications.dispatcher import NotificationDispatcher
from shared.schemas.messages import AlertMessage


def make_alert(priority: str = "low") -> AlertMessage:
    return AlertMessage(
        timestamp=datetime.now(timezone.utc),
        event_type="test_event",
        priority=priority,
        message="test message",
    )


def make_channel(**kwargs) -> TelegramChannel:
    defaults = {"bot_token": "123:abc", "chat_id": "42"}
    defaults.update(kwargs)
    return TelegramChannel(**defaults)


def mock_async_client(response: MagicMock) -> MagicMock:
    """Build a mock for httpx.AsyncClient used as an async context manager."""
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, client


class TestTelegramChannel:
    def test_is_configured_with_explicit_credentials(self):
        assert make_channel().is_configured

    def test_not_configured_without_credentials(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        assert not TelegramChannel().is_configured

    def test_credentials_fall_back_to_env(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:xyz")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "7")
        assert TelegramChannel().is_configured

    @pytest.mark.asyncio
    async def test_send_posts_to_bot_api(self):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        ctx, client = mock_async_client(response)
        with patch("services.notifications.channels.httpx.AsyncClient", return_value=ctx):
            await make_channel().send(subject="[HIGH] drawdown", body="down 11%")

        url = client.post.call_args.args[0]
        payload = client.post.call_args.kwargs["json"]
        assert url == f"{TELEGRAM_API_BASE}/bot123:abc/sendMessage"
        assert payload["chat_id"] == "42"
        assert payload["text"].startswith("[HIGH] drawdown\n\n")
        assert "down 11%" in payload["text"]

    @pytest.mark.asyncio
    async def test_send_truncates_long_body(self):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        ctx, client = mock_async_client(response)
        with patch("services.notifications.channels.httpx.AsyncClient", return_value=ctx):
            await make_channel().send(subject="s", body="x" * 10_000)

        text = client.post.call_args.kwargs["json"]["text"]
        assert len(text) <= TELEGRAM_MAX_BODY + len("s\n\n")

    @pytest.mark.asyncio
    async def test_send_raises_without_credentials(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
            await TelegramChannel().send(subject="s", body="b")

    @pytest.mark.asyncio
    async def test_send_raises_on_http_error(self):
        response = MagicMock()
        response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "401", request=MagicMock(), response=MagicMock()
            )
        )
        ctx, _ = mock_async_client(response)
        with patch("services.notifications.channels.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(httpx.HTTPStatusError):
                await make_channel().send(subject="s", body="b")


class TestTelegramRouting:
    """Telegram is the primary operator channel: every priority reaches it."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("priority", ["critical", "high", "medium", "low"])
    async def test_all_priorities_route_to_telegram(self, priority):
        telegram = AsyncMock()
        dispatcher = NotificationDispatcher(telegram=telegram)
        await dispatcher.dispatch(make_alert(priority))
        telegram.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disabled_channels_are_skipped(self):
        """Only-telegram dispatcher must not error on None channels."""
        telegram = AsyncMock()
        dispatcher = NotificationDispatcher(
            slack=None, email=None, sms=None, telegram=telegram
        )
        await dispatcher.dispatch(make_alert("critical"))
        telegram.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_telegram_failure_does_not_block_other_channels(self):
        telegram = AsyncMock()
        telegram.send.side_effect = RuntimeError("telegram down")
        slack = AsyncMock()
        dispatcher = NotificationDispatcher(slack=slack, telegram=telegram)
        await dispatcher.dispatch(make_alert("low"))
        slack.send.assert_awaited_once()
