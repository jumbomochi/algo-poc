"""The market-data IB client's connection handling.

KAN-58. This client is instantiated in exactly one place
(``services/data_ingestion/runner.py``) and was never connected, so every
request raised ``AttributeError`` on a ``None`` handle.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.data_ingestion.ib_client import IBClient


@pytest.fixture
def fake_ib_insync(monkeypatch):
    """Stand in for the ib_insync module, which connect() imports lazily."""
    ib = MagicMock()
    ib.connectAsync = AsyncMock()
    ib.isConnected = MagicMock(return_value=True)
    module = types.ModuleType("ib_insync")
    module.IB = MagicMock(return_value=ib)
    monkeypatch.setitem(sys.modules, "ib_insync", module)
    return ib


class TestConnect:
    async def test_it_connects_to_the_configured_host_and_port(self, fake_ib_insync):
        client = IBClient(host="host.docker.internal", port=7497, client_id=2)

        await client.connect()

        args, kwargs = fake_ib_insync.connectAsync.await_args
        assert args[0] == "host.docker.internal"
        assert args[1] == 7497
        assert kwargs["clientId"] == 2

    async def test_it_connects_read_only(self, fake_ib_insync):
        """This service reads market data and fundamentals and has no business
        placing orders. A read-only session means a bug here cannot reach the
        book at all, rather than relying on the code not to."""
        await IBClient(host="h", port=7497, client_id=2).connect()

        assert fake_ib_insync.connectAsync.await_args.kwargs["readonly"] is True

    async def test_it_bounds_the_connect_attempt(self, fake_ib_insync):
        """An unbounded connect against a gateway sitting behind a stuck modal
        wedges the poll loop — the failure mode the heartbeat exists to expose,
        better avoided than detected."""
        await IBClient(host="h", port=7497, client_id=2).connect()

        assert fake_ib_insync.connectAsync.await_args.kwargs["timeout"] > 0


class TestIsConnected:
    def test_a_client_that_never_connected_is_not_connected(self):
        assert IBClient(host="h", port=7497, client_id=2).is_connected() is False

    async def test_it_reflects_the_live_session(self, fake_ib_insync):
        client = IBClient(host="h", port=7497, client_id=2)
        await client.connect()
        assert client.is_connected() is True

        fake_ib_insync.isConnected.return_value = False
        assert client.is_connected() is False

    async def test_a_failed_connect_leaves_it_disconnected(self, monkeypatch):
        """Not merely False-returning: a half-built handle would make the next
        request raise instead of prompting a reconnect."""
        ib = MagicMock()
        ib.connectAsync = AsyncMock(side_effect=OSError("connection refused"))
        ib.isConnected = MagicMock(return_value=False)
        module = types.ModuleType("ib_insync")
        module.IB = MagicMock(return_value=ib)
        monkeypatch.setitem(sys.modules, "ib_insync", module)

        client = IBClient(host="h", port=7497, client_id=2)
        with pytest.raises(OSError):
            await client.connect()

        assert client.is_connected() is False


class TestDisconnect:
    async def test_disconnecting_an_unconnected_client_is_a_no_op(self):
        await IBClient(host="h", port=7497, client_id=2).disconnect()

    async def test_it_clears_the_handle_so_a_reconnect_is_clean(self, fake_ib_insync):
        client = IBClient(host="h", port=7497, client_id=2)
        await client.connect()

        await client.disconnect()

        fake_ib_insync.disconnect.assert_called_once()
        assert client.is_connected() is False
