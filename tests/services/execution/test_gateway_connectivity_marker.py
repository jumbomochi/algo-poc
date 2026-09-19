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


# --- IB-1101: 1101 (data lost) vs 1102 (data maintained) -------------------
#
# A 1100/1101 pair leaves the API socket up, so `connect()` never re-runs and
# `_reregister_open_trades()` never fires. On 2026-09-18 that left the executor
# blind to 15 fills: it repriced 119 times against a stale view and then
# cancelled orders IB had already executed. 1101 must re-request open orders;
# 1102 (subscriptions maintained) must not.


class _RestoreTrade:
    """Minimal ib_insync Trade stand-in with re-bindable callback events."""

    def __init__(self, order_id: int) -> None:
        self.order = types.SimpleNamespace(orderId=order_id)
        self.orderStatus = types.SimpleNamespace(status="Submitted", filled=0)
        self.statusEvent = _FakeEvent()
        self.commissionReportEvent = _FakeEvent()


class _RestoreIB:
    """Fake IB whose `reqOpenOrdersAsync` is observable."""

    def __init__(self, open_trades=None, raises: Exception | None = None) -> None:
        self._open_trades = list(open_trades or [])
        self._raises = raises
        self.req_open_orders_calls = 0

    async def reqOpenOrdersAsync(self):
        self.req_open_orders_calls += 1
        if self._raises is not None:
            raise self._raises
        return list(self._open_trades)

    def openTrades(self):
        return list(self._open_trades)

    def isConnected(self):
        return True


async def _drain(ex: IBExecutor) -> None:
    """Await every fire-and-forget task the executor spawned."""
    import asyncio

    for _ in range(5):
        pending = list(ex._pending_tasks)
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _restored_executor(tmp_path: Path, ib) -> IBExecutor:
    ex = _executor(tmp_path)
    ex._ib = ib
    return ex


@pytest.mark.asyncio
async def test_1102_clears_marker_and_does_not_resubscribe(tmp_path: Path) -> None:
    """1102 means connectivity restored with data MAINTAINED — the open-order
    and market-data subscriptions survived, so re-requesting is pure noise."""
    marker = tmp_path / MARKER_NAME
    marker.write_text("1754006400")
    ib = _RestoreIB()
    ex = _restored_executor(tmp_path, ib)

    ex._on_ib_error(reqId=-1, errorCode=1102, errorString="restored", contract=None)
    await _drain(ex)

    assert not marker.exists()
    assert ib.req_open_orders_calls == 0


@pytest.mark.asyncio
async def test_1101_clears_marker_and_re_requests_open_orders(
    tmp_path: Path,
) -> None:
    """1101 means data LOST: IB invalidated the open-order subscription and
    will push nothing further for previously-tracked orders. The client must
    re-request them and re-bind the per-Trade callbacks."""
    marker = tmp_path / MARKER_NAME
    marker.write_text("1754006400")

    fresh = _RestoreTrade(9)
    ib = _RestoreIB(open_trades=[fresh])
    ex = _restored_executor(tmp_path, ib)
    ex.set_fill_handler(_noop_async)
    ex.set_order_status_handler(_noop_async)
    stale = _RestoreTrade(9)
    ex._register_trade("9", stale, "AAPL", "buy")

    ex._on_ib_error(reqId=-1, errorCode=1101, errorString="data lost", contract=None)
    await _drain(ex)

    assert not marker.exists()
    assert ib.req_open_orders_calls == 1
    # _reregister_open_trades ran against the re-requested trades.
    assert ex._trades["9"] is fresh
    assert len(fresh.statusEvent) == 1
    assert len(fresh.commissionReportEvent) == 1


@pytest.mark.asyncio
async def test_1101_does_not_double_bind_callbacks_on_a_surviving_trade(
    tmp_path: Path,
) -> None:
    """ib_insync keys its Trade cache on (clientId, orderId), so a re-request
    over a live socket hands back the SAME Trade object. eventkit invokes a
    listener once per registration, so binding again would double every fill."""
    survivor = _RestoreTrade(9)
    ib = _RestoreIB(open_trades=[survivor])
    ex = _restored_executor(tmp_path, ib)
    ex.set_fill_handler(_noop_async)
    ex.set_order_status_handler(_noop_async)
    ex._register_trade("9", survivor, "AAPL", "buy")
    assert len(survivor.statusEvent) == 1

    ex._on_ib_error(reqId=-1, errorCode=1101, errorString="data lost", contract=None)
    await _drain(ex)

    assert len(survivor.statusEvent) == 1
    assert len(survivor.commissionReportEvent) == 1


@pytest.mark.asyncio
async def test_1101_re_request_failure_never_escapes_the_error_handler(
    tmp_path: Path,
) -> None:
    """`marker I/O must never disturb order routing` applies here too: a failed
    re-subscription is logged, never raised into ib_insync's dispatch."""
    ib = _RestoreIB(raises=RuntimeError("socket gone"))
    ex = _restored_executor(tmp_path, ib)
    ex._logger = _RecordingLogger()

    # Must not raise, either synchronously...
    ex._on_ib_error(reqId=-1, errorCode=1101, errorString="data lost", contract=None)
    # ...or out of the spawned task.
    await _drain(ex)

    assert any(
        call[0] in ("error", "exception") for call in ex._logger.calls
    ), ex._logger.calls


@pytest.mark.asyncio
async def test_1101_absent_orders_are_surfaced_not_terminalized(
    tmp_path: Path,
) -> None:
    """An order missing from IB's re-requested open orders may have FILLED
    during the blackout. Resolving it by guesswork is what turned 15 filled
    positions into phantoms — it gets logged for reconciliation, nothing more."""
    ib = _RestoreIB(open_trades=[])  # order 9 is not open at IB any more
    ex = _restored_executor(tmp_path, ib)
    ex.set_fill_handler(_noop_async)
    ex.set_order_status_handler(_noop_async)
    ex._logger = _RecordingLogger()
    stale = _RestoreTrade(9)
    ex._register_trade("9", stale, "AAPL", "buy")

    ex._on_ib_error(reqId=-1, errorCode=1101, errorString="data lost", contract=None)
    await _drain(ex)

    warned = [c for c in ex._logger.calls if c[0] == "warning"]
    assert any(c[2].get("order_ids") == ["9"] for c in warned), ex._logger.calls
    # Still tracked — nothing was terminalized or forgotten.
    assert "9" in ex._trades
    assert ex._trade_meta["9"] == ("AAPL", "buy")


@pytest.mark.asyncio
async def test_restore_codes_log_distinctly_with_the_code_in_the_payload(
    tmp_path: Path,
) -> None:
    """Today the restore path logs nothing, which is why the 2026-09-18 logs
    could not answer 'was that a 1101 or a 1102?'. Both must be recoverable."""
    ib = _RestoreIB()
    ex = _restored_executor(tmp_path, ib)
    ex._logger = _RecordingLogger()

    ex._on_ib_error(reqId=-1, errorCode=1102, errorString="ok", contract=None)
    await _drain(ex)
    infos = [c for c in ex._logger.calls if c[0] == "info"]
    assert any(c[2].get("error_code") == 1102 for c in infos), ex._logger.calls

    ex._logger.calls.clear()
    ex._on_ib_error(reqId=-1, errorCode=1101, errorString="lost", contract=None)
    await _drain(ex)
    warnings = [c for c in ex._logger.calls if c[0] == "warning"]
    assert any(c[2].get("error_code") == 1101 for c in warnings), ex._logger.calls
    # A 1101 is not merely informational — it may mean lost fills.
    assert not any(c[2].get("error_code") == 1101 for c in ex._logger.calls if c[0] == "info")


@pytest.mark.asyncio
async def test_1101_pages_the_operator(tmp_path: Path) -> None:
    """Going blind to fills is money on this system, and the daily broker
    reconciliation proved insufficient on 2026-08-28. The executor hands the
    condition to the runner's alert publisher."""
    ib = _RestoreIB(open_trades=[])
    ex = _restored_executor(tmp_path, ib)
    seen: list[dict] = []

    async def _alert(payload):
        seen.append(payload)

    ex.set_connectivity_alert_handler(_alert)
    ex._on_ib_error(reqId=-1, errorCode=1101, errorString="data lost", contract=None)
    await _drain(ex)

    assert len(seen) == 1, seen
    assert seen[0]["error_code"] == 1101


@pytest.mark.asyncio
async def test_1102_does_not_page(tmp_path: Path) -> None:
    ib = _RestoreIB()
    ex = _restored_executor(tmp_path, ib)
    seen: list[dict] = []

    async def _alert(payload):
        seen.append(payload)

    ex.set_connectivity_alert_handler(_alert)
    ex._on_ib_error(reqId=-1, errorCode=1102, errorString="ok", contract=None)
    await _drain(ex)

    assert seen == []


async def _noop_async(_payload) -> None:
    return None


class _RecordingLogger:
    """Records (level, message, kwargs) so tests can assert on the payload."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def _record(self, level):
        def _log(message="", **kwargs):
            self.calls.append((level, message, kwargs))

        return _log

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._record(name)
