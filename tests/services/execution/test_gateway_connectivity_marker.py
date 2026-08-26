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
    assert int(marker.read_text().splitlines()[0].strip()) > 0


def test_line_one_stays_a_bare_epoch_any_reader_can_parse(tmp_path: Path) -> None:
    """KAN-63 added a `key=value` tail. It has to be a tail.

    The watchdog reads this file with `head -1`, and so would any deployed copy
    of it predating the change — including one that has drifted, which is
    exactly the state the per-wrapper drift guard exists to warn about rather
    than to prevent. Putting the epoch anywhere but line 1, or prefixing it with
    a key, turns a stale deployed reader into one that computes a garbage age.
    """
    ex = _executor(tmp_path)
    ex._on_ib_error(reqId=-1, errorCode=1100, errorString="lost", contract=None)

    lines = (tmp_path / MARKER_NAME).read_text().splitlines()
    assert lines[0].isdigit(), lines
    assert "=" not in lines[0]


def test_the_marker_records_who_wrote_it_and_which_gateway(tmp_path: Path) -> None:
    """KAN-63 AC1, the half execution can actually observe.

    A 1100 that outlives the Gateway session that raised it is not evidence of
    anything, and on 2026-08-21 one was re-alerted for 20h07m across a full
    Gateway process replacement. Execution runs in a container and cannot see
    the host Gateway's pid or start time, so it records the endpoint and its own
    authorship; the watchdog stamps `gateway_pid` / `gateway_started_at` on
    first observation, being the only party that can.
    """
    ex = IBExecutor("gw-host", 7497, 1, state_dir=tmp_path)
    ex._on_ib_error(reqId=-1, errorCode=1100, errorString="lost", contract=None)

    text = (tmp_path / MARKER_NAME).read_text()
    assert "writer=execution" in text, text
    assert "gateway_endpoint=gw-host:7497" in text, text


def test_a_second_1100_rewrites_rather_than_appends(tmp_path: Path) -> None:
    """Two losses in one session must not leave two epochs in the file, or
    `head -1` starts reporting the older one forever."""
    ex = _executor(tmp_path)
    ex._on_ib_error(reqId=-1, errorCode=1100, errorString="lost", contract=None)
    ex._on_ib_error(reqId=-1, errorCode=1100, errorString="lost again", contract=None)

    lines = [ln for ln in (tmp_path / MARKER_NAME).read_text().splitlines() if ln]
    assert sum(1 for ln in lines if ln.isdigit()) == 1, lines


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
