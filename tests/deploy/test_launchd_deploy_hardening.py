"""Reboot-fragility / deploy-drift hardening assertions for the launchd jobs.

Companion to test_message_bus_lockdown.py (T3 credentials). These lock in the
fix for the 2026-08-11 cold-boot incident, where launchd ran a hand-copied
``~/ibc/run_divergence.sh`` that had drifted to a pre-T3 revision: it failed DB
auth at 04:45 and, missing the exit-3 handler, logged a BLIND baseline as
"UNEXPECTED exit code 3". The repo copy is canonical; ``deploy.sh`` syncs it and
each wrapper self-checks for drift. Plain-text assertions on checked-in files,
so they fail CI with no host state required.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

DEPLOY_DIR = Path("deploy/launchd")
DEPLOY_SCRIPT = DEPLOY_DIR / "deploy.sh"
RUN_PAPER = DEPLOY_DIR / "run_paper.sh"
RUN_DIVERGENCE = DEPLOY_DIR / "run_divergence.sh"
RUN_PIPELINE_REPORT = DEPLOY_DIR / "run_pipeline_report.sh"
RUN_DB_BACKUP = DEPLOY_DIR / "run_db_backup.sh"
RUN_BACKTEST_REFRESH = DEPLOY_DIR / "run_backtest_refresh.sh"
GATEWAY_WATCHDOG = DEPLOY_DIR / "gateway_watchdog.sh"
PAPER_PLIST = DEPLOY_DIR / "local.algo-paper-trading.plist"

# Every wrapper deploy.sh copies to ~/ibc — i.e. every .sh that is *executed*
# from the deployed location and can therefore drift from the repo. secrets.sh
# and deadman.sh are excluded because they are sourced by path from the repo and
# deploy.sh deliberately refuses to copy them (deploy.sh:78-85).
WRAPPERS = (
    RUN_PAPER,
    RUN_DIVERGENCE,
    RUN_PIPELINE_REPORT,
    RUN_DB_BACKUP,
    RUN_BACKTEST_REFRESH,
    GATEWAY_WATCHDOG,
)

# Only the two jobs that talk to the docker stack / IB Gateway on a cold boot.
PORT_WAITING_WRAPPERS = (RUN_PAPER, RUN_DIVERGENCE)

# Every launchd job definition. A rebuilt machine gets its wiring back only
# from tracked files, so the set is enumerated rather than globbed — see
# test_every_plist_is_version_controlled.
PLISTS = (
    DEPLOY_DIR / "local.algo-backtest-refresh.plist",
    DEPLOY_DIR / "local.algo-db-backup.plist",
    DEPLOY_DIR / "local.algo-divergence-monitor.plist",
    DEPLOY_DIR / "local.algo-gateway-watchdog.plist",
    PAPER_PLIST,
    DEPLOY_DIR / "local.algo-pipeline-report.plist",
)


def _is_git_tracked(path: Path, cwd: Path | None = None) -> bool:
    """True iff `path` is in git's index — which is what "version controlled"
    actually means. `Path.exists()` is not a substitute: an untracked file that
    happens to be on this machine's disk passes exists() and is still lost on a
    rebuild.
    """
    res = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", str(path)],
        capture_output=True, text=True, cwd=str(cwd) if cwd else None,
    )
    return res.returncode == 0


def test_deploy_script_exists_and_is_executable():
    assert DEPLOY_SCRIPT.exists(), "deploy/launchd/deploy.sh is missing"
    assert os.access(DEPLOY_SCRIPT, os.X_OK), "deploy.sh must be executable"
    text = DEPLOY_SCRIPT.read_text()
    assert text.startswith("#!"), "deploy.sh needs a shebang"
    # It must offer a no-write preview and must not run launchctl itself —
    # (re)loading a job is a human step (CLAUDE.md). Printed/commented
    # references are fine; an *executed* command (line starting with launchctl)
    # is not.
    assert "--dry-run" in text
    executed = [ln for ln in text.splitlines() if ln.lstrip().startswith("launchctl ")]
    assert not executed, f"deploy.sh must only print launchctl commands, never run them: {executed}"


def test_git_tracked_check_can_tell_tracked_from_merely_present(tmp_path):
    """The assertion below is only worth anything if it can fail.

    Its predecessor asserted `.exists()` under a message that said "tracked",
    so `git rm --cached` would have left it green while destroying the property
    it exists to protect. This pins the distinction on a throwaway repo, where
    both files are present on disk and only one is in the index.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, timeout=60)
    tracked = tmp_path / "tracked.plist"
    tracked.write_text("<plist/>")
    untracked = tmp_path / "untracked.plist"
    untracked.write_text("<plist/>")
    subprocess.run(["git", "add", "tracked.plist"], cwd=tmp_path, check=True, timeout=60)

    assert tracked.exists() and untracked.exists(), "exists() cannot separate these"
    assert _is_git_tracked(tracked, cwd=tmp_path)
    assert not _is_git_tracked(untracked, cwd=tmp_path)


def test_every_plist_is_version_controlled():
    # A machine rebuild restores wiring only for tracked files; the paper plist
    # was previously live-only. deploy.sh can only sync what is in the repo, and
    # the property is identical for all six jobs.
    on_disk = {p.name for p in DEPLOY_DIR.glob("*.plist")}
    assert on_disk == {p.name for p in PLISTS}, (
        f"plist set changed; add it to PLISTS so it is covered too: {on_disk}"
    )
    for plist in PLISTS:
        assert _is_git_tracked(plist), f"{plist.name} must be tracked in the repo"
    assert "run_paper.sh" in PAPER_PLIST.read_text()


def test_wrappers_have_a_drift_guard():
    for wrapper in WRAPPERS:
        text = wrapper.read_text()
        assert "repo canonical" in text, f"{wrapper} lacks the drift self-check"
        assert "deploy/launchd/deploy.sh" in text, (
            f"{wrapper} drift guard should point at deploy.sh"
        )
        assert 'cmp -s "$0" "$CANON"' in text, f"{wrapper} drift check is not comparing to the canonical"


def _run_deployed_copy(tmp_path, *, drifted: bool) -> str:
    """Run a *deployed* copy of run_paper.sh with the repo as its canonical, and
    return the log it wrote.

    run_paper.sh is the one wrapper that can be driven end-to-end safely: the
    `security` stub guarantees no credential resolves, so it aborts long before
    it could place an order. The guard runs before that abort.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    deployed = tmp_path / RUN_PAPER.name
    body = RUN_PAPER.read_text()
    if drifted:
        body += "\n# a hand edit on the deployed copy\n"
    deployed.write_text(body)
    deployed.chmod(0o755)

    security_stub = tmp_path / "security-stub"
    security_stub.write_text("#!/bin/bash\necho 'could not be found' >&2\nexit 44\n")
    security_stub.chmod(0o755)
    # Without this the failure path raises a real desktop notification on the
    # developer's machine every time this test runs.
    osascript_stub = tmp_path / "osascript-stub"
    osascript_stub.write_text("#!/bin/bash\nexit 0\n")
    osascript_stub.chmod(0o755)

    env = dict(
        os.environ,
        HOME=str(fake_home),
        ALGO_DIR=str(Path.cwd()),
        ALGO_SECURITY_BIN=str(security_stub),
        ALGO_OSASCRIPT_BIN=str(osascript_stub),
        ALGO_KEYCHAIN_SERVICE="algo-poc-absent-test-service",
    )
    res = subprocess.run(
        [str(deployed)], capture_output=True, text=True, timeout=120, env=env, cwd=Path.cwd(),
    )
    assert res.returncode != 0, "wrapper should abort when no credential resolves"
    logs = list((fake_home / "ibc" / "logs").glob("paper_trading_*.log"))
    assert logs, "wrapper wrote no log"
    return logs[0].read_text()


def test_drift_guard_warns_when_the_deployed_copy_differs(tmp_path):
    log = _run_deployed_copy(tmp_path, drifted=True)
    assert "differs from repo canonical" in log, log


def test_drift_guard_is_silent_when_the_deployed_copy_matches(tmp_path):
    # A legitimately in-sync copy must not cry wolf, or the warning stops
    # meaning anything on the day it fires for real.
    log = _run_deployed_copy(tmp_path, drifted=False)
    assert "differs from repo canonical" not in log, log


def test_wrappers_wait_for_ports_instead_of_failing_on_first_probe():
    for wrapper in PORT_WAITING_WRAPPERS:
        text = wrapper.read_text()
        assert "wait_for_port()" in text, f"{wrapper} must define the bounded wait helper"
        assert "wait_for_port 127.0.0.1 55432" in text, f"{wrapper} should wait for the paper DB"
    # Only the paper run drives IB orders, so only it waits on the Gateway.
    assert "wait_for_port 127.0.0.1 7497" in RUN_PAPER.read_text()


def test_dry_run_leaves_a_fresh_host_filesystem_untouched(tmp_path):
    """AC#16, iron rule: a preview must not write.

    `mkdir -p "$IBC" "$LA"` used to run before the dry-run branch, so previewing
    a deploy on a fresh host silently created ~/ibc and ~/Library/LaunchAgents —
    a read-only command with a write side effect.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = dict(os.environ, HOME=str(fake_home))
    res = subprocess.run(
        [str(DEPLOY_SCRIPT.resolve()), "--dry-run"],
        capture_output=True, text=True, timeout=60, env=env,
        cwd=Path.cwd(),
    )
    assert res.returncode == 0, res.stderr
    created = sorted(p.relative_to(fake_home).as_posix() for p in fake_home.rglob("*"))
    assert created == [], f"--dry-run wrote to a fresh HOME: {created}"


def test_run_paper_creates_its_log_dir_before_the_first_write(tmp_path):
    """AC#17, behavioural: on a fresh host the opening line and every error
    after it used to vanish into a failed redirect, so the run died with no log
    to explain why. Forced to abort at the credential step via a `security`
    stub that always misses, so the real paper run can never fire from a test.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    stub = tmp_path / "security-stub"
    stub.write_text("#!/bin/bash\necho 'could not be found' >&2\nexit 44\n")
    stub.chmod(0o755)

    env = dict(
        os.environ,
        HOME=str(fake_home),
        ALGO_SECURITY_BIN=str(stub),
        ALGO_KEYCHAIN_SERVICE="algo-poc-absent-test-service",
    )
    res = subprocess.run(
        [str(RUN_PAPER.resolve())],
        capture_output=True, text=True, timeout=120, env=env, cwd=Path.cwd(),
    )
    # It must fail at credentials, never proceed to trade.
    assert res.returncode != 0, "wrapper should abort when no credential resolves"

    log_dir = fake_home / "ibc" / "logs"
    assert log_dir.is_dir(), "run_paper.sh did not create LOG_DIR before writing"
    logs = list(log_dir.glob("paper_trading_*.log"))
    assert logs, f"no log written; dir contains {list(log_dir.iterdir())}"
    text = logs[0].read_text()
    assert "Starting daily paper trading run" in text, text
    # And the reason has to be in there, not just the banner.
    assert "ERROR" in text, text


def test_codegraph_index_is_gitignored():
    ignore = Path(".gitignore").read_text().splitlines()
    assert ".codegraph/" in ignore, "the local CodeGraph index must not be committable"


def test_divergence_wrapper_handles_the_blind_baseline_exit_code():
    # Regression: the drifted deployed copy lacked the exit-3 case, so a BLIND
    # baseline surfaced as "UNEXPECTED exit code 3" instead of a clear alert.
    text = RUN_DIVERGENCE.read_text()
    assert "3)" in text
    assert "BLIND" in text
