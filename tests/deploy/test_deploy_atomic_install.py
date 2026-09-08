"""Deploying must not corrupt a job that is currently running the wrapper.

``deploy.sh`` installed each wrapper with ``cp "$src" "$dst"``. cp truncates and
rewrites the *existing inode*, and bash does not read a script into memory — it
reads incrementally and remembers a byte offset. A shell already executing
``$dst`` therefore resumes reading at its old offset inside the new content,
which is a different position in a different file.

Observed on 2026-09-08:

    06:30:27  a backtest refresh starts, executing ~/ibc/run_backtest_refresh.sh
    07:54     deploy.sh copies a new version over it (+7 lines, KAN-69's comment)
    19:11     the running shell emits:

        run_backtest_refresh.sh: line 248: ith: command not found
        run_backtest_refresh.sh: line 251: syntax error near unexpected token `fi'

Line 248 of the file on disk is ``# with no pin and no accepted biases.`` — the
shell resumed mid-word, seven lines adrift, exactly the offset that edit
introduced. ``bash -n`` on the file passes: the file is fine, the process is not.

The corruption lands in the wrapper's **tail**, which is where every wrapper
keeps its bookkeeping — ``refresh_exit``'s single dead-man decision, the
TIMEOUT_FLAG branch, the pin protection around the ``output/`` prune. So the
failure mode is a job that appears to run and then silently skips its cleanup.
And it applies to every deployed wrapper: ``run_paper.sh`` executes for minutes
from 04:15, so a deploy landing in that window corrupts a live trading run.

It is invisible afterwards. The file left on disk is valid, the drift guard
(``cmp -s "$0" "$CANON"``) compares clean, and only the running shell's stderr —
which goes to the launchd log, not the job's own log — records anything.

These tests assert the **property** (a running job survives a deploy) rather
than the implementation, so a future rewrite that keeps the guarantee still
passes. The padding below is not decoration: bash reads a script in blocks, so
the victim must be large enough to need a second read *after* the file has been
replaced, or the whole thing sits in the first buffer and nothing can go wrong.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LAUNCHD = REPO / "deploy/launchd"

# Comfortably more than one read block, so the tail is fetched from disk after
# the `sleep` — which is the window the deploy lands in.
PADDING_LINES = 3000
SLEEP_SECONDS = 4


def _victim(extra_header_lines: int = 0) -> str:
    """A wrapper that sleeps early and prints its marker from the far tail."""
    head = "".join(f"# shift_{i:05d}\n" for i in range(extra_header_lines))
    padding = "".join(
        f"# padding_line_{i:05d} aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        for i in range(PADDING_LINES)
    )
    return (
        "#!/bin/bash\n"
        f"{head}"
        f"sleep {SLEEP_SECONDS}\n"
        f"{padding}"
        "echo MARKER_DONE\n"
        "exit 0\n"
    )


@pytest.fixture()
def host(tmp_path):
    """A fake repo containing the shipped deploy.sh, and a fake HOME."""
    src = tmp_path / "repo" / "deploy" / "launchd"
    (src / "lib").mkdir(parents=True)
    # The code under test, verbatim. deploy.sh derives ALGO_DIR from
    # BASH_SOURCE, so it must be run from inside the fake tree.
    for name in ("deploy.sh",):
        (src / name).write_bytes((LAUNCHD / name).read_bytes())
        (src / name).chmod(0o755)
    (src / "lib" / "launchd_wiring.sh").write_bytes(
        (LAUNCHD / "lib" / "launchd_wiring.sh").read_bytes()
    )

    home = tmp_path / "home"
    (home / "ibc").mkdir(parents=True)
    (home / "Library" / "LaunchAgents").mkdir(parents=True)

    launchctl = tmp_path / "launchctl-stub"
    launchctl.write_text("#!/bin/bash\nexit 0\n")
    launchctl.chmod(0o755)

    return src, home, launchctl


def _deploy(host) -> subprocess.CompletedProcess:
    src, home, launchctl = host
    return subprocess.run(
        [str(src / "deploy.sh")],
        capture_output=True, text=True, timeout=120,
        env=dict(os.environ, HOME=str(home), ALGO_LAUNCHCTL_BIN=str(launchctl)),
    )


def test_a_job_running_from_the_deployed_path_survives_a_deploy(host, tmp_path):
    """The 2026-09-08 defect, reproduced end to end.

    Against `cp` the running shell resumes at a stale offset and dies with
    "command not found" / "syntax error" partway through its own tail.
    """
    src, home, _ = host
    (src / "victim.sh").write_text(_victim())
    assert _deploy(host).returncode == 0

    deployed = home / "ibc" / "victim.sh"
    assert deployed.exists()

    out = tmp_path / "victim.out"
    with out.open("w") as fh:
        proc = subprocess.Popen(
            ["/bin/bash", str(deployed)], stdout=fh, stderr=subprocess.STDOUT,
        )
    try:
        time.sleep(1.0)  # it is inside its sleep, tail not yet read
        # A deploy lands mid-run, with every later byte offset shifted.
        (src / "victim.sh").write_text(_victim(extra_header_lines=40))
        assert _deploy(host).returncode == 0
        rc = proc.wait(timeout=SLEEP_SECONDS + 60)
    finally:
        if proc.poll() is None:
            proc.kill()

    body = out.read_text()
    assert "command not found" not in body, body[-2000:]
    assert "syntax error" not in body, body[-2000:]
    assert "MARKER_DONE" in body, body[-2000:]
    assert rc == 0, f"exit {rc}\n{body[-2000:]}"


def test_the_executable_bit_survives_the_install(host):
    """A lost +x makes every job fail at the next launchd tick."""
    src, home, _ = host
    (src / "victim.sh").write_text("#!/bin/bash\nexit 0\n")
    assert _deploy(host).returncode == 0
    deployed = home / "ibc" / "victim.sh"
    assert os.access(deployed, os.X_OK), oct(deployed.stat().st_mode)

    # And on a re-deploy over an existing file, not just the first install.
    (src / "victim.sh").write_text("#!/bin/bash\n# changed\nexit 0\n")
    assert _deploy(host).returncode == 0
    assert os.access(deployed, os.X_OK), oct(deployed.stat().st_mode)


def test_a_failed_copy_leaves_the_previous_wrapper_in_place(host):
    """Half a wrapper is worse than a stale one: the tail is where the
    bookkeeping lives, so a truncated install is a job that runs and skips its
    own cleanup."""
    src, home, _ = host
    good = "#!/bin/bash\n# GOOD VERSION\nexit 0\n"
    (src / "victim.sh").write_text(good)
    assert _deploy(host).returncode == 0
    deployed = home / "ibc" / "victim.sh"
    assert "GOOD VERSION" in deployed.read_text()

    # An unreadable source: -f still passes, the copy cannot.
    (src / "victim.sh").write_text("#!/bin/bash\n# NEW VERSION\nexit 0\n")
    (src / "victim.sh").chmod(0o000)
    try:
        res = _deploy(host)
    finally:
        (src / "victim.sh").chmod(0o644)

    assert "GOOD VERSION" in deployed.read_text(), (
        "a failed copy replaced or truncated the working wrapper"
    )
    assert res.returncode != 0, "deploy.sh reported success despite failing to install"


def test_no_leftover_staging_files_in_the_destination(host):
    """A temp file left behind would be picked up by the wrapper drift guard as
    an unexplained extra, and `*.sh` globbing would try to deploy it next time."""
    src, home, _ = host
    (src / "victim.sh").write_text("#!/bin/bash\nexit 0\n")
    assert _deploy(host).returncode == 0
    leftovers = [p.name for p in (home / "ibc").iterdir() if p.name != "victim.sh"]
    assert not leftovers, leftovers


def test_deploy_never_writes_directly_onto_a_live_destination():
    """Pin the mechanism, not just the symptom: `cp $src $dst` reuses the inode
    a running shell is reading from. Installing must stage then rename."""
    body = (LAUNCHD / "deploy.sh").read_text()
    offenders = [
        line.strip()
        for line in body.splitlines()
        if line.strip().startswith("cp ")
        and '"$dst"' in line
        and "tmp" not in line
    ]
    assert not offenders, (
        "deploy.sh copies straight onto the destination path:\n" + "\n".join(offenders)
    )
