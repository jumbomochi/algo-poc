"""KAN-15 (P1-12) — the external dead-man switch on the 04:15 paper run.

Every other alert in this repo is sent by this host, about this host. None of
them can fire when the Mac is off, asleep, or off the network — and that is
exactly what "the daily run never happened" looks like from the inside. It is
also indistinguishable, to every metric in config/alert_rules.yml, from a
perfectly healthy day on which every signal was a SKIP.

So the check is inverted and moved outside: a *successful* run pings an
external checker, and the checker pages when the ping does not arrive. The two
properties that make that work are the two things asserted here — it pings on
success, and it stays silent on failure. A wrapper that pinged unconditionally
would report a crashed run as a healthy one, which is worse than no check.

The wrapper is executed, not grepped (the pattern from
test_launchd_secrets_keychain.py and test_observability_healthchecks.py): the
thing worth proving is that the ping happens, not that the source contains the
word curl.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY_DIR = Path("deploy/launchd")
DEADMAN = DEPLOY_DIR / "deadman.sh"
SECRETS = DEPLOY_DIR / "secrets.sh"
RUN_PAPER = DEPLOY_DIR / "run_paper.sh"

PING_URL = "https://hc.example.test/ping/deadbeef-1234"


# ---------------------------------------------------------------------------
# Stub host
# ---------------------------------------------------------------------------


def _write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _curl_stub(tmp_path: Path) -> tuple[Path, Path]:
    """A `curl` that records every invocation instead of making a request."""
    log = tmp_path / "curl-invocations.log"
    stub = _write_exec(
        tmp_path / "bin" / "curl",
        f'#!/bin/bash\nprintf "%s\\n" "$*" >> {log}\nexit 0\n',
    )
    return stub, log


def _run_deadman(
    tmp_path: Path,
    exit_code: str,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Source deadman.sh in a real bash and call algo_deadman_ping."""
    stub_bin, log = _curl_stub(tmp_path)
    script = _write_exec(
        tmp_path / "drive.sh",
        "#!/bin/bash\n"
        f'. "{SECRETS.resolve()}"\n'
        f'. "{DEADMAN.resolve()}"\n'
        f"algo_deadman_ping {exit_code}\n"
        'printf "STATUS=%s\\n" "$ALGO_DEADMAN_STATUS"\n',
    )
    result = subprocess.run(
        ["/bin/bash", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": f"{stub_bin.parent}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            # A keychain lookup that always misses, so nothing on the
            # developer's real keychain can leak into a test.
            "ALGO_SECURITY_BIN": str(_missing_security_stub(tmp_path)),
            "ALGO_SECRETS_ENV_FILE": str(tmp_path / "no-such.env"),
            **(env or {}),
        },
    )
    invocations = log.read_text().splitlines() if log.exists() else []
    return result, invocations


def _missing_security_stub(tmp_path: Path) -> Path:
    return _write_exec(
        tmp_path / "stubs" / "security-missing",
        "#!/bin/bash\necho 'could not be found' >&2\nexit 44\n",
    )


# ---------------------------------------------------------------------------
# AC6, unit half — the ping helper itself
# ---------------------------------------------------------------------------


def test_a_successful_run_pings_the_dead_man_url(tmp_path: Path) -> None:
    result, invocations = _run_deadman(
        tmp_path, "0", {"ALGO_DEADMAN_PAPER_URL": PING_URL}
    )
    assert result.returncode == 0, result.stderr
    assert len(invocations) == 1, invocations
    assert PING_URL in invocations[0]
    assert "pinged" in result.stdout


def test_a_failed_run_does_not_ping(tmp_path: Path) -> None:
    """The whole mechanism depends on this. If a failed run pinged, the
    external checker would see a healthy heartbeat for a day on which nothing
    was committed — the 2026-08-13 silence, reproduced with extra steps."""
    result, invocations = _run_deadman(
        tmp_path, "1", {"ALGO_DEADMAN_PAPER_URL": PING_URL}
    )
    assert result.returncode == 0, result.stderr
    assert invocations == []
    assert "not pinged" in result.stdout


def test_an_unconfigured_switch_says_so_instead_of_failing_silently(
    tmp_path: Path,
) -> None:
    """Fire-and-forget must not become fail-silently: the operator has to be
    able to tell "nothing external is watching" from "the ping went out"."""
    result, invocations = _run_deadman(tmp_path, "0")
    assert result.returncode == 0, result.stderr
    assert invocations == []
    assert "NOT CONFIGURED" in result.stdout


def test_a_ping_failure_never_fails_the_run(tmp_path: Path) -> None:
    """Monitoring must not be able to cause the outage it exists to detect: a
    flaky network on an otherwise successful trading day must not turn into a
    non-zero exit."""
    stub_bin = _write_exec(tmp_path / "bin" / "curl", "#!/bin/bash\nexit 7\n")
    script = _write_exec(
        tmp_path / "drive.sh",
        "#!/bin/bash\n"
        "set -e\n"
        f'. "{SECRETS.resolve()}"\n'
        f'. "{DEADMAN.resolve()}"\n'
        "algo_deadman_ping 0\n"
        'printf "STATUS=%s\\n" "$ALGO_DEADMAN_STATUS"\n',
    )
    result = subprocess.run(
        ["/bin/bash", str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": f"{stub_bin.parent}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "ALGO_SECURITY_BIN": str(_missing_security_stub(tmp_path)),
            "ALGO_DEADMAN_PAPER_URL": PING_URL,
        },
    )
    assert result.returncode == 0, result.stderr
    assert "PING FAILED" in result.stdout


def test_the_ping_url_is_never_written_to_a_log_verbatim(tmp_path: Path) -> None:
    """A healthchecks.io URL is a bearer capability — anyone who reads it out
    of a log can forge a healthy ping and turn the switch off for good."""
    result, _ = _run_deadman(tmp_path, "0", {"ALGO_DEADMAN_PAPER_URL": PING_URL})
    status = result.stdout
    assert PING_URL not in status, status
    assert "hc.example.test" in status, "the status is too redacted to debug a typo'd host"


# ---------------------------------------------------------------------------
# AC6, wrapper half — the real run_paper.sh, driven both ways
# ---------------------------------------------------------------------------


def _stub_tree(tmp_path: Path, paper_exit: int) -> tuple[Path, Path, dict[str, str]]:
    """A throwaway ALGO_DIR that run_paper.sh can be pointed at.

    Nothing real is reachable from here: the python that would run
    scripts/run_paper.py, the alembic that would talk to the paper database,
    curl, nc and the keychain are all stubs. The wrapper under test is the
    real one from the repo.
    """
    tree = tmp_path / "algo-dir"
    shutil.copytree(DEPLOY_DIR, tree / "deploy" / "launchd")

    # The paper run itself. This is the one stub that must never be missing —
    # the real command trades. It is why ALGO_DIR is overridable at all.
    _write_exec(
        tree / ".venv" / "bin" / "python",
        f"#!/bin/bash\necho 'stub paper run'\nexit {paper_exit}\n",
    )
    # Schema check: DB revision == head, so the wrapper proceeds.
    _write_exec(
        tree / ".venv" / "bin" / "alembic",
        "#!/bin/bash\necho 'a1b2c3d4e5f6 (head)'\n",
    )
    _write_exec(tmp_path / "bin" / "nc", "#!/bin/bash\nexit 0\n")
    stub_bin, curl_log = _curl_stub(tmp_path)

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
        ALGO_OSASCRIPT_BIN=str(_write_exec(tmp_path / "stubs" / "osascript", "#!/bin/bash\nexit 0\n")),
        ALGO_DEADMAN_PAPER_URL=PING_URL,
    )
    return curl_log, home, env


def _run_wrapper(tmp_path: Path, paper_exit: int) -> tuple[list[str], str, int]:
    curl_log, home, env = _stub_tree(tmp_path, paper_exit)
    result = subprocess.run(
        [str(RUN_PAPER.resolve())],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=Path.cwd(),
    )
    invocations = curl_log.read_text().splitlines() if curl_log.exists() else []
    logs = list((home / "ibc" / "logs").glob("paper_trading_*.log"))
    log_text = logs[0].read_text() if logs else ""
    return invocations, log_text, result.returncode


@pytest.mark.skipif(os.name != "posix", reason="the launchd wrappers are POSIX shell")
def test_the_wrapper_pings_after_a_successful_run(tmp_path: Path) -> None:
    invocations, log_text, returncode = _run_wrapper(tmp_path, paper_exit=0)
    assert returncode == 0, log_text
    pings = [call for call in invocations if PING_URL in call]
    assert len(pings) == 1, f"expected exactly one dead-man ping, got {invocations}"
    assert "dead-man switch: pinged" in log_text, log_text


@pytest.mark.skipif(os.name != "posix", reason="the launchd wrappers are POSIX shell")
def test_the_wrapper_stays_silent_after_a_failed_run(tmp_path: Path) -> None:
    invocations, log_text, returncode = _run_wrapper(tmp_path, paper_exit=3)
    assert returncode == 3, log_text
    assert not [call for call in invocations if PING_URL in call], invocations
    # The failure still alerts through the normal (host-local) paths — the
    # dead-man switch is an addition to those, not a replacement.
    assert "Paper trading run FAILED" in " ".join(invocations)
    assert "dead-man switch: not pinged (run exited 3)" in log_text, log_text


# ---------------------------------------------------------------------------
# Deploy wiring
# ---------------------------------------------------------------------------


def test_deadman_is_sourced_by_path_and_never_deployed_to_ibc() -> None:
    """Same rule as secrets.sh: a copy under ~/ibc would never be executed, so
    deploying one only plants the stale-copy trap that broke the 2026-08-11
    cold boot."""
    assert '. "$ALGO_DIR/deploy/launchd/deadman.sh"' in RUN_PAPER.read_text()
    deploy = (DEPLOY_DIR / "deploy.sh").read_text()
    assert '[ "$(basename "$f")" = "deadman.sh" ] && continue' in deploy


def test_the_dead_man_url_is_an_optional_secret_not_a_required_one() -> None:
    """A host that has not configured a dead-man switch yet must not make
    `secrets.sh --check` report that the stack cannot authenticate."""
    text = SECRETS.read_text()
    assert "ALGO_OPTIONAL_SECRET_NAMES" in text
    required = next(
        line for line in text.splitlines() if line.startswith("ALGO_SECRET_NAMES=")
    )
    assert "ALGO_DEADMAN_PAPER_URL" not in required
    assert "DEADMAN_WATCHDOG_URL" not in required
