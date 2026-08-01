"""Error 1100 observer in IBExecutor.

The Gateway watchdog is blind to IB Error 1100 ("connectivity between IB and
the Gateway lost") because the API port stays open during a 1100. The always-on
execution client is the only component that sees the API event, so it drops a
marker file the host watchdog reads. See
docs/superpowers/specs/2026-08-01-gateway-watchdog-error-1100-design.md.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from services.execution.ib_executor import IBExecutor

MARKER_NAME = "gateway_connectivity_lost"


def _executor(state_dir: Path) -> IBExecutor:
    return IBExecutor("h", 7497, 1, state_dir=state_dir)


def test_error_1100_writes_marker_with_epoch(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    ex._on_ib_error(reqId=-1, errorCode=1100, errorString="lost", contract=None)

    marker = tmp_path / MARKER_NAME
    assert marker.exists()
    assert int(marker.read_text().strip()) > 0


@pytest.mark.parametrize("restore_code", [1101, 1102])
def test_connectivity_restored_clears_marker(tmp_path: Path, restore_code: int) -> None:
    marker = tmp_path / MARKER_NAME
    marker.write_text("1754006400")  # a stale lost-marker

    ex = _executor(tmp_path)
    ex._on_ib_error(reqId=-1, errorCode=restore_code, errorString="ok", contract=None)

    assert not marker.exists()


def test_unrelated_error_code_is_ignored(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    # 2104 (farm OK), 201 (order rejected) etc. must not touch the marker.
    ex._on_ib_error(reqId=7, errorCode=2104, errorString="farm ok", contract=None)
    assert not (tmp_path / MARKER_NAME).exists()


def test_marker_write_failure_is_swallowed(tmp_path: Path, monkeypatch) -> None:
    """A marker-I/O failure must never disturb order routing."""
    ex = _executor(tmp_path)

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _boom)
    # Must not raise.
    ex._on_ib_error(reqId=-1, errorCode=1100, errorString="lost", contract=None)


def test_no_state_dir_is_a_noop(tmp_path: Path) -> None:
    """With no state dir configured the observer is inert (no crash)."""
    ex = IBExecutor("h", 7497, 1, state_dir=None)
    ex._on_ib_error(reqId=-1, errorCode=1100, errorString="lost", contract=None)
    ex._on_ib_error(reqId=-1, errorCode=1102, errorString="ok", contract=None)


class _FakeEvent(list):
    """Mimics ib_insync's `errorEvent` supporting `event += handler`."""

    def __iadd__(self, handler):
        self.append(handler)
        return self


class _FakeIB:
    def __init__(self) -> None:
        self.errorEvent = _FakeEvent()
        self._connected = True

    async def connectAsync(self, host, port, clientId):  # noqa: ANN001
        return None

    def managedAccounts(self):
        return ["DUN551088"]

    def isConnected(self):
        return self._connected

    def disconnect(self):
        self._connected = False


@pytest.mark.asyncio
async def test_connect_attaches_handler_and_clears_stale_marker(
    tmp_path: Path, monkeypatch
) -> None:
    fake_module = types.ModuleType("ib_insync")
    fake_module.IB = _FakeIB
    monkeypatch.setitem(sys.modules, "ib_insync", fake_module)

    marker = tmp_path / MARKER_NAME
    marker.write_text("1754006400")  # stale marker left by a prior outage

    ex = _executor(tmp_path)
    await ex.connect(expect_paper=True)

    # A healthy session proves connectivity: the stale marker is cleared...
    assert not marker.exists()
    # ...and future 1100s are observed.
    assert ex._on_ib_error in ex._ib.errorEvent
