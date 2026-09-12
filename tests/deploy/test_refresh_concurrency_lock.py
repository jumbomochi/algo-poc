"""Only one backtest refresh at a time — KAN-76.

WHAT HAPPENED
-------------
Three refreshes ran on 2026-09-08, all appending to one daily log::

    05:11:10  launchd's scheduled run  -> FAILED exit 143 (SIGTERM, dead stack)
    06:30:27  a manual run             -> still running at 19:12, 12h42m in
    19:11:26  a second manual run      -> FAILED exit 1 at 19:11:43

The third lasted seventeen seconds. It called ``run_backtest.py``, which
connects to IB with the backtest's clientId 10, and IB refuses a second
connection presenting a clientId already in use::

    ib.connect(host, port, clientId=client_id, timeout=15)
    ...
    TimeoutError
  2026-09-08 19:11:43: refresh FAILED (exit 1)

It did not fail on its own merits. It collided with the 06:30 run, which nobody
knew was still alive.

WHY THE SILENCE IS THE EXPENSIVE PART
-------------------------------------
The diagnosis was misleading in three ways at once:

- the failure is an asyncio ``TimeoutError`` deep in ib_insync, which names the
  symptom (could not connect) and not the cause (something else holds the id);
- all three runs append to the SAME daily log, so the file ends in "refresh
  FAILED" while a healthy run is still going. Reading the tail — which is what
  an operator does — reports the wrong state of the world;
- the failed run correctly withholds its dead-man ping, so a collision looks
  identical to a refresh that never happened.

"A refresh started at 06:30:27 is still running (pid 87473)" is the sentence
that was missing, and it is the one thing these tests are really about.

WHY A REFUSAL IS NOT A FAILURE
------------------------------
Nothing went wrong when a second invocation declines to start: the first one is
doing the work. So the refusal takes its own exit code (75, EX_TEMPFAIL — the
conventional "try again later", and distinct from the wrapper's 1, 2 and 124),
raises no failure alert, and does not ping the dead-man switch, which belongs to
the run that is actually running.

Manual catch-ups after a failed Tuesday are the normal way this job gets run, so
two overlapping invocations is the expected shape of the problem, not an edge
case.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO / "deploy" / "launchd"
RUN_REFRESH = DEPLOY_DIR / "run_backtest_refresh.sh"

#: "Another refresh holds the lock." EX_TEMPFAIL, and distinct from 1 (gateway),
#: 2 (snapshot) and 124 (timeout) so the log says which happened.
EXIT_LOCKED = 75

DEADMAN_URL = "https://hc.example.test/ping/refresh-1234"

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the launchd wrappers are POSIX shell"
)


class Host:
    """A scratch host the wrapper can be driven against, more than once.

    The same ``HOME`` across invocations is the point: the lock lives under
    ``$LOG_DIR``, so two drives against one Host contend for the real thing at
    its real default path rather than an injected one.
    """

    def __init__(self, tmp_path: Path, *, snapshot: bool = True) -> None:
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.log_dir = self.home / "ibc" / "logs"
        self.lock_dir = self.log_dir / "backtest_refresh.lock"

        algo = tmp_path / "algo"
        algo.mkdir()
        for name in ("deploy", "scripts", "config"):
            (algo / name).symlink_to(REPO / name)
        (algo / "output").mkdir()
        (algo / "data" / "universe").mkdir(parents=True)
        if snapshot:
            (algo / "data" / "universe" / "sp500_membership.json").write_text(
                json.dumps({"snapshots": {"2015-01-01": ["AAPL"]}})
            )
        self.algo = algo

        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.curl_log = tmp_path / "curl.log"
        self.ping_log = tmp_path / "pings.log"
        self.argv_log = tmp_path / "argv.log"

        self._stub("nc", "#!/bin/bash\nexit 0\n")
        self._stub("osascript", "#!/bin/bash\nexit 0\n")
        self._stub(
            "security",
            '#!/bin/bash\ncase "${@: -1}" in\n'
            '  TELEGRAM_BOT_TOKEN) echo "stub-token" ;;\n'
            '  TELEGRAM_CHAT_ID)   echo "stub-chat" ;;\n'
            '  *) echo "could not be found" >&2; exit 44 ;;\n'
            "esac\n",
        )
        self._stub(
            "curl",
            f"""#!/bin/bash
if printf '%s\\n' "$@" | grep -q 'hc.example.test'; then
    printf '%s\\n' "$*" >> {self.ping_log}
else
    {{ for a in "$@"; do printf '%s\\n' "$a"; done; printf -- '---END---\\n'; }} >> {self.curl_log}
fi
exit 0
""",
        )

    def _stub(self, name: str, body: str) -> Path:
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)
        return path

    def backtest(self, *, sleep: float = 0, exit_code: int = 0, ignore_term: bool = False) -> Path:
        """The stubbed `python`. Records argv so a refused run is provably one
        that never started a backtest, not one that started and failed."""
        if ignore_term:
            # One process that genuinely ignores SIGTERM, so only the bound's
            # SIGKILL can end it. `trap '' TERM` in a shell would not do: the
            # bound signals the whole process group, so the shell's `sleep`
            # would take the TERM and the parent would exit right behind it.
            body = (
                "#!/bin/bash\n"
                f'case "$1" in *baseline_pin.py|*protected_artifacts.py) exec {sys.executable} "$@" ;; esac\n'
                f"{{ for a in \"$@\"; do printf '%s\\n' \"$a\"; done; printf -- '---END---\\n'; }} >> {self.argv_log}\n"
                f"exec {sys.executable} -c 'import signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)'\n"
            )
        else:
            body = (
                "#!/bin/bash\n"
                f'case "$1" in *baseline_pin.py|*protected_artifacts.py) exec {sys.executable} "$@" ;; esac\n'
                f"{{ for a in \"$@\"; do printf '%s\\n' \"$a\"; done; printf -- '---END---\\n'; }} >> {self.argv_log}\n"
                f"sleep {sleep}\n"
                'echo "AGGREGATE"\n'
                'echo "  Total Return: 42.0%"\n'
                f"touch {self.algo}/output/backtest_multi_20260817_000000.json\n"
                f"exit {exit_code}\n"
            )
        return self._stub("fake-python", body)

    def env(self, *, python: Path, timeout_seconds: int = 600) -> dict[str, str]:
        env = dict(
            os.environ,
            HOME=str(self.home),
            ALGO_DEADMAN_REFRESH_URL=DEADMAN_URL,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            ALGO_DIR=str(self.algo),
            ALGO_PYTHON=str(python),
            ALGO_SECURITY_BIN=str(self.bin / "security"),
            ALGO_OSASCRIPT_BIN=str(self.bin / "osascript"),
            ALGO_KEYCHAIN_SERVICE="algo-poc-absent-test-service",
            ALGO_REFRESH_TIMEOUT_SECONDS=str(timeout_seconds),
            ALGO_BOUNDED_POLL_SECONDS="1",
            ALGO_BOUNDED_KILL_GRACE_SECONDS="2",
        )
        env.pop("ALGO_BASELINE_PIN", None)
        return env

    # -- driving ---------------------------------------------------------

    def run(self, *, python: Path | None = None, timeout_seconds: int = 600,
            wait: int = 180) -> subprocess.CompletedProcess[str]:
        python = python or self.backtest()
        return subprocess.run(
            [str(RUN_REFRESH)], capture_output=True, text=True, timeout=wait,
            env=self.env(python=python, timeout_seconds=timeout_seconds),
            cwd=str(REPO),
        )

    def launch(self, *, python: Path, timeout_seconds: int = 600) -> subprocess.Popen:
        return subprocess.Popen(
            [str(RUN_REFRESH)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=self.env(python=python, timeout_seconds=timeout_seconds),
            cwd=str(REPO),
        )

    # -- observations ----------------------------------------------------

    @property
    def log(self) -> str:
        logs = sorted(self.log_dir.glob("backtest_refresh_*.log"))
        return "\n".join(p.read_text() for p in logs)

    @property
    def backtests_started(self) -> int:
        if not self.argv_log.exists():
            return 0
        return len([s for s in self.argv_log.read_text().split("---END---\n") if s.strip()])

    @property
    def pings(self) -> list[str]:
        return self.ping_log.read_text().splitlines() if self.ping_log.exists() else []

    @property
    def telegram(self) -> list[str]:
        if not self.curl_log.exists():
            return []
        return [s for s in self.curl_log.read_text().split("---END---\n") if s.strip()]


def _await_lock(host: Host, deadline: float = 30.0) -> None:
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        if (host.lock_dir / "pid").exists() and host.backtests_started >= 1:
            return
        time.sleep(0.1)
    raise AssertionError(f"the first run never took the lock (log:\n{host.log})")


# ---------------------------------------------------------------------------
# AC1, AC2, AC3 — the refusal
# ---------------------------------------------------------------------------


def test_a_second_run_refuses_while_the_first_holds_the_lock(tmp_path: Path):
    """AC1. The 19:11 run on 2026-09-08 got seventeen seconds into a backtest
    before IB refused clientId 10. It should never have started one."""
    host = Host(tmp_path)
    first = host.launch(python=host.backtest(sleep=30))
    try:
        _await_lock(host)
        second = host.run(python=host.backtest(sleep=30), wait=60)
        assert second.returncode == EXIT_LOCKED, (
            f"exit {second.returncode}\n{second.stdout}\n{second.stderr}"
        )
        # Exactly one backtest was ever started: the first run's.
        assert host.backtests_started == 1, host.argv_log.read_text()
    finally:
        first.kill()
        first.wait(timeout=30)


def test_the_refusal_names_the_holding_pid_and_when_it_started(tmp_path: Path):
    """AC1, and the whole point of the story. An operator reading the log on
    2026-09-08 had no way to learn that a refresh from twelve hours earlier was
    still alive."""
    host = Host(tmp_path)
    first = host.launch(python=host.backtest(sleep=30))
    try:
        _await_lock(host)
        holder_pid = (host.lock_dir / "pid").read_text().strip()
        second = host.run(python=host.backtest(sleep=30), wait=60)
        said = second.stdout + second.stderr + host.log
        assert holder_pid in said, said
        assert "still running" in said, said
        # The start time, not just the pid — "since when" is what tells the
        # operator whether to wait or to go killing things.
        started = (host.lock_dir / "started").read_text().strip()
        assert started in said, f"the refusal does not say when: {said}"
    finally:
        first.kill()
        first.wait(timeout=30)


def test_the_refusal_neither_pings_the_dead_man_nor_alerts(tmp_path: Path):
    """AC3. Nothing failed — the other run is doing the work. A failure alert
    here trains the operator to ignore this job's alerts, and a ping would claim
    a refresh happened when this invocation did nothing at all."""
    host = Host(tmp_path)
    first = host.launch(python=host.backtest(sleep=30))
    try:
        _await_lock(host)
        pings_before = len(host.pings)
        sends_before = len(host.telegram)
        host.run(python=host.backtest(sleep=30), wait=60)
        assert len(host.pings) == pings_before, host.pings
        assert len(host.telegram) == sends_before, host.telegram
        assert "FAILED" not in host.log.split("still running")[-1], host.log
    finally:
        first.kill()
        first.wait(timeout=30)


def test_the_refusing_run_does_not_release_the_holders_lock(tmp_path: Path):
    """The trap in any lock built out of a shared release helper: the refusing
    process must not tidy up a lock it never took, or the third invocation walks
    straight past a live holder."""
    host = Host(tmp_path)
    first = host.launch(python=host.backtest(sleep=30))
    try:
        _await_lock(host)
        host.run(python=host.backtest(sleep=30), wait=60)
        assert host.lock_dir.is_dir(), "the refusing run removed the live lock"
        third = host.run(python=host.backtest(sleep=30), wait=60)
        assert third.returncode == EXIT_LOCKED
    finally:
        first.kill()
        first.wait(timeout=30)


# ---------------------------------------------------------------------------
# AC4 — a stale lock must not wedge the job
# ---------------------------------------------------------------------------


def test_a_lock_left_by_a_dead_run_is_reclaimed(tmp_path: Path):
    """AC4. The 05:11 run died on SIGTERM (exit 143) with a dead docker stack,
    which is exactly how a lock gets orphaned. One hard kill must not take the
    weekly refresh out of service until someone notices."""
    host = Host(tmp_path)
    host.lock_dir.mkdir(parents=True)
    # A pid that is certainly not running. Allocated and reaped here so the
    # number is real rather than a guess that could collide.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (host.lock_dir / "pid").write_text(f"{dead.pid}\n")
    (host.lock_dir / "started").write_text("2026-09-08 05:11:10\n")

    result = host.run()
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}\n{host.log}"
    assert host.backtests_started == 1
    assert "stale" in host.log.lower(), host.log


def test_reclaiming_a_stale_lock_is_said_out_loud(tmp_path: Path):
    """A silent reclaim hides the fact that a previous run died without
    cleaning up — which is a real event worth one line in the log."""
    host = Host(tmp_path)
    host.lock_dir.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (host.lock_dir / "pid").write_text(f"{dead.pid}\n")
    (host.lock_dir / "started").write_text("2026-09-08 05:11:10\n")

    host.run()
    assert str(dead.pid) in host.log, host.log
    assert "2026-09-08 05:11:10" in host.log, host.log


# ---------------------------------------------------------------------------
# AC5 — released on every exit path
# ---------------------------------------------------------------------------


def test_a_healthy_run_releases_the_lock(tmp_path: Path):
    host = Host(tmp_path)
    result = host.run()
    assert result.returncode == 0, host.log
    assert not host.lock_dir.exists(), "the lock outlived a successful run"


def test_an_abort_releases_the_lock(tmp_path: Path):
    """AC5. The snapshot abort (exit 2) happens after the lock is taken, so a
    missing membership file must not leave the job locked out until a human
    deletes a directory."""
    host = Host(tmp_path, snapshot=False)
    result = host.run()
    assert result.returncode == 2, f"{result.stdout}\n{host.log}"
    assert not host.lock_dir.exists(), "the lock outlived an abort"
    # And the next invocation is free to run.
    (host.algo / "data" / "universe" / "sp500_membership.json").write_text(
        json.dumps({"snapshots": {"2015-01-01": ["AAPL"]}})
    )
    assert host.run().returncode == 0, host.log


def test_a_timeout_releases_the_lock(tmp_path: Path):
    """AC5. The path that matters most: a wedged refresh is exactly the state in
    which someone reaches for a manual catch-up, and a bound that killed the run
    but kept the lock would refuse it."""
    host = Host(tmp_path)
    result = host.run(
        python=host.backtest(ignore_term=True), timeout_seconds=2, wait=90
    )
    assert result.returncode == 124, f"{result.stdout}\n{host.log}"
    assert not host.lock_dir.exists(), "the lock outlived a timeout"


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_the_exit_code_contract_documents_the_locked_code() -> None:
    """AC2. Three codes were already documented in the header; a fourth that is
    not is a code nobody can look up at 05:00."""
    header = RUN_REFRESH.read_text().split("\n\n")[0]
    assert str(EXIT_LOCKED) in header, (
        "run_backtest_refresh.sh's header does not document the locked exit code"
    )
