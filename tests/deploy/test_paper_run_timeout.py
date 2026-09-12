"""The 04:15 paper run must stop when it cannot finish — KAN-78.

WHAT HAPPENED
-------------
The 2026-09-09 run started at 04:15:06. At 19:00 it was still going — 14h45m —
stalled at ticker 88 of 140 with 87 consecutive zero-bar fetches, holding IB
clientId 58 the whole time. It had produced no shadow series, so the 04:47
divergence monitor had already exited 3 (BLIND) twelve hours earlier. Nothing
would have stopped it before the next 04:15 slot, at which point two paper runs
would have contended for the same clientId — the failure KAN-69 closed for
refresh-versus-paper, returning as paper-versus-paper.

``run_paper.sh`` contained exactly one ``timeout``, and it belonged to the
port-wait helper. Nothing bounded the run itself.

WHERE THE DEADLINE COMES FROM
-----------------------------
Measured from ``~/ibc/logs/paper_trading_*.log`` (start line to "run
completed"), the last fourteen sessions::

    healthy    5.8  6.0  6.1  6.8  6.8  6.9  7.9  8.6   minutes
    degraded   144  502  527  889                        minutes

The two populations do not overlap and are not close. The default is **3h**,
which is ~25x the healthy median and still clears the 144-minute run that did
eventually complete — a merely slow run should finish and give the book its day,
while the 8-to-15-hour pathologies are ended less than three hours in.
Overridable through ``ALGO_PAPER_TIMEOUT_SECONDS``.

WHY 124 AND WHY NOT SILENTLY
----------------------------
124 is timeout(1)'s conventional expiry code, already the refresh's contract,
and not one ``run_paper.py`` can produce. A timeout withholds the dead-man ping
— the run did not succeed — so the external check pages at its next deadline,
and the local alert plus Telegram name the deadline so the operator is not left
to infer it from a stale process.

These tests drive the shipped wrapper with a stub run that genuinely ignores
SIGTERM, under an injected deadline of a few seconds. Nothing here sleeps
through a real one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

DEPLOY_DIR = Path("deploy/launchd")
RUN_PAPER = DEPLOY_DIR / "run_paper.sh"
PING_URL = "https://hc.example.test/ping/deadbeef-1234"

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the launchd wrappers are POSIX shell"
)


def _write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


#: A run that ignores SIGTERM, in ONE process. Not `bash -c "trap '' TERM;
#: sleep"`: the bound signals the whole process group, so that shell's `sleep`
#: would take the TERM and die and its parent would exit right behind it — the
#: test would pass in two seconds without ever reaching SIGKILL.
_IGNORES_TERM = (
    "import signal, time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "time.sleep(600)"
)


def _stub_tree(tmp_path: Path, *, paper_run: str) -> tuple[Path, Path, dict[str, str]]:
    """A throwaway ALGO_DIR the real run_paper.sh can be pointed at.

    Same shape as tests/deploy/test_deadman_ping.py's: nothing real is
    reachable — the python that would run scripts/run_paper.py, alembic, curl,
    nc and the keychain are all stubs, and the wrapper under test is the repo's.
    """
    tree = tmp_path / "algo-dir"
    shutil.copytree(DEPLOY_DIR, tree / "deploy" / "launchd")

    _write_exec(tree / ".venv" / "bin" / "python", paper_run)
    _write_exec(tree / ".venv" / "bin" / "alembic", "#!/bin/bash\necho 'a1b2c3d4e5f6 (head)'\n")
    _write_exec(tmp_path / "bin" / "nc", "#!/bin/bash\nexit 0\n")

    curl_log = tmp_path / "curl-invocations.log"
    stub_bin = _write_exec(
        tmp_path / "bin" / "curl",
        f'#!/bin/bash\nprintf "%s\\n" "$*" >> {curl_log}\nexit 0\n',
    )
    security = _write_exec(
        tmp_path / "stubs" / "security",
        "#!/bin/bash\n"
        'name=""; prev=""\n'
        'for arg in "$@"; do [ "$prev" = "-a" ] && name="$arg"; prev="$arg"; done\n'
        'case "$name" in\n'
        "  POSTGRES_PASSWORD) echo 'stub-pg' ;;\n"
        "  REDIS_PASSWORD) echo 'stub-redis' ;;\n"
        "  TELEGRAM_BOT_TOKEN) echo '123456789:stub' ;;\n"
        "  TELEGRAM_CHAT_ID) echo '-100123' ;;\n"
        "  *) echo 'could not be found' >&2; exit 44 ;;\n"
        "esac\n",
    )
    home = tmp_path / "home"
    home.mkdir()
    env = dict(
        PATH=f"{stub_bin.parent}:/usr/bin:/bin",
        HOME=str(home),
        ALGO_DIR=str(tree),
        ALGO_SECURITY_BIN=str(security),
        ALGO_OSASCRIPT_BIN=str(
            _write_exec(tmp_path / "stubs" / "osascript", "#!/bin/bash\nexit 0\n")
        ),
        ALGO_DEADMAN_PAPER_URL=PING_URL,
        # The injected deadline, and a bound that escalates in seconds rather
        # than the production 20.
        ALGO_PAPER_TIMEOUT_SECONDS="2",
        ALGO_BOUNDED_POLL_SECONDS="1",
        ALGO_BOUNDED_KILL_GRACE_SECONDS="2",
    )
    return curl_log, home, env


class _Result:
    def __init__(self, returncode: int, log: str, alerts: str, curl: list[str], secs: float):
        self.returncode = returncode
        self.log = log
        self.alerts = alerts
        self.curl = curl
        self.seconds = secs

    @property
    def telegram(self) -> str:
        return " ".join(c for c in self.curl if "api.telegram.org" in c or "sendMessage" in c)


def _run_wrapper(tmp_path: Path, *, paper_run: str, timeout: int = 120) -> _Result:
    curl_log, home, env = _stub_tree(tmp_path, paper_run=paper_run)
    start = time.monotonic()
    proc = subprocess.run(
        [str(RUN_PAPER.resolve())],
        capture_output=True, text=True, timeout=timeout, env=env, cwd=Path.cwd(),
    )
    elapsed = time.monotonic() - start
    logs = list((home / "ibc" / "logs").glob("paper_trading_*.log"))
    alerts = home / "ibc" / "logs" / "ALERTS.log"
    return _Result(
        proc.returncode,
        logs[0].read_text() if logs else "",
        alerts.read_text() if alerts.exists() else "",
        curl_log.read_text().splitlines() if curl_log.exists() else [],
        elapsed,
    )


def _wedged_run() -> str:
    return f"#!/bin/bash\nexec {sys.executable} -c '{_IGNORES_TERM}'\n"


# ---------------------------------------------------------------------------
# AC1 / AC6 — the bound fires, and only when it should
# ---------------------------------------------------------------------------


def test_a_run_past_its_deadline_is_killed_and_the_wrapper_exits_124(tmp_path: Path):
    """AC1, AC3. On 2026-09-09 this run would have continued for another
    fourteen hours and collided with the next day's slot."""
    res = _run_wrapper(tmp_path, paper_run=_wedged_run())
    assert res.returncode == 124, f"exit {res.returncode}\n{res.log}"
    assert res.seconds < 30, f"took {res.seconds:.1f}s — the bound did not fire"


def test_sigkill_follows_for_a_run_that_ignores_sigterm(tmp_path: Path):
    """AC1. A run wedged on a socket read may not take a polite signal, and the
    whole point is that it stops. The stub ignores SIGTERM in a single process,
    so only SIGKILL can end it — if it did not, this would run for 600s."""
    res = _run_wrapper(tmp_path, paper_run=_wedged_run())
    assert res.returncode == 124
    # 2s deadline + 2s grace, generously bounded.
    assert res.seconds < 30, f"took {res.seconds:.1f}s — SIGKILL did not follow"


def test_a_normal_run_is_untouched_by_the_bound(tmp_path: Path):
    """AC6. The bound must be invisible on the ~7-minute days, which is all of
    them when the host is behaving."""
    res = _run_wrapper(
        tmp_path, paper_run="#!/bin/bash\necho 'stub paper run'\nexit 0\n"
    )
    assert res.returncode == 0, res.log
    assert "TIMED OUT" not in res.log, res.log
    assert [c for c in res.curl if PING_URL in c], "the dead-man ping was withheld"


def test_a_normal_run_leaves_no_watchdog_process_behind(tmp_path: Path):
    """AC6. A killer outliving its child fires at an unrelated pid later. The
    wrapper must not return while one is still armed — if it did, this would
    hang until the injected deadline rather than exiting immediately."""
    res = _run_wrapper(
        tmp_path, paper_run="#!/bin/bash\necho 'stub paper run'\nexit 0\n", timeout=60
    )
    assert res.returncode == 0
    assert res.seconds < 15, (
        f"took {res.seconds:.1f}s — the wrapper waited for its own watchdog"
    )


# ---------------------------------------------------------------------------
# AC2 / AC4 — a timeout is loud, named, and withholds the heartbeat
# ---------------------------------------------------------------------------


def test_a_timeout_alerts_locally_and_names_the_deadline(tmp_path: Path):
    """AC2. 'FAILED' alone would send the operator looking for a traceback."""
    res = _run_wrapper(tmp_path, paper_run=_wedged_run())
    assert "TIMED OUT" in res.alerts, res.alerts
    assert "2s" in res.alerts, res.alerts


def test_a_timeout_alerts_through_telegram_too(tmp_path: Path):
    """AC2. Every local alert is sent by this host about this host. Telegram is
    the path that reaches the operator who is not at the machine."""
    res = _run_wrapper(tmp_path, paper_run=_wedged_run())
    assert "TIMED" in res.telegram.upper(), res.curl


def test_a_timeout_is_distinguishable_from_every_other_abort(tmp_path: Path):
    """AC2. The wrapper already aborts for an unreachable gateway, an
    unreachable DB, a schema mismatch and a missing credential. A fifth failure
    that reads like the other four is a fifth failure nobody diagnoses."""
    res = _run_wrapper(tmp_path, paper_run=_wedged_run())
    for other in (
        "IB Gateway never came up",
        "paper DB never came up",
        "DB schema at",
        "could not determine alembic head",
    ):
        assert other not in res.alerts, f"a timeout alerted as {other!r}"
    assert "TIMED OUT" in res.alerts


def test_a_timeout_withholds_the_dead_man_ping(tmp_path: Path):
    """AC4. The run did not succeed, so the external check must page at its
    next deadline. A wrapper that pinged here would report the 14h45m wedge as
    a healthy day."""
    res = _run_wrapper(tmp_path, paper_run=_wedged_run())
    assert not [c for c in res.curl if PING_URL in c], res.curl
    assert "dead-man switch: not pinged (run exited 124)" in res.log, res.log


# ---------------------------------------------------------------------------
# AC3 / AC4 / AC5 — structure, so the next edit cannot quietly undo this
# ---------------------------------------------------------------------------


def test_the_exit_code_contract_documents_124() -> None:
    """AC3. The refresh documents its codes in its header; this had no contract
    at all, which is part of why nothing noticed that it had no bound."""
    header = RUN_PAPER.read_text().split("\n\n")[0]
    assert "124" in header, "run_paper.sh's header does not document exit 124"


def test_the_bound_uses_the_shared_helper_and_not_a_private_copy() -> None:
    """AC5. There were three private bounded-exec copies before KAN-75, each
    with its own bugs; the refresh's counted sleeps instead of the wall clock.
    A fourth would reintroduce whichever of those its author did not know
    about."""
    text = RUN_PAPER.read_text()
    assert "lib/bounded.sh" in text, "run_paper.sh does not source the shared helper"
    assert "algo_run_bounded" in text, "run_paper.sh does not use it"
    assert not re.search(r"^\s*_algo\w*bounded\(\)", text, re.MULTILINE), (
        "run_paper.sh grew a private bounded-exec copy"
    )


def test_every_exit_after_the_run_goes_through_the_single_exit_helper() -> None:
    """AC4. The ping decision is made in exactly one place, from the code being
    returned, so a new early-abort added later cannot become a healthy beat by
    omission — the same shape run_backtest_refresh.sh uses."""
    text = RUN_PAPER.read_text()
    assert "paper_exit()" in text, "run_paper.sh has no single exit helper"
    bare = [
        line for line in text.splitlines()
        # `exit "$1"` is paper_exit's own body — the one place the wrapper is
        # allowed to leave.
        if re.match(r"^\s*exit\s", line) and line.strip() != 'exit "$1"'
    ]
    assert not bare, "exits that bypass paper_exit(): " + "; ".join(bare)
