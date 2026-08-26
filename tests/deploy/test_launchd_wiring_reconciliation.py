"""KAN-64: a tracked, copied plist is not a loaded job.

``local.algo-evidence-digest.plist`` was version-controlled, copied into
``~/Library/LaunchAgents`` on 2026-08-17, and **never fired once** — nobody ran
the ``launchctl bootstrap`` commands ``deploy.sh`` printed. Both existing guards
stayed green throughout, because both were true: the plist really was tracked
(``test_every_plist_is_version_controlled``), and ``deploy.sh`` really did
refuse to run ``launchctl`` (``test_deploy_script_exists_and_is_executable``,
deliberately — bootout/bootstrap is a human step per CLAUDE.md). Nothing closed
the loop between them. Two Monday digests were missed and the 2026-08-18
evidence gap went unnoticed for three days as a direct result.

The reconciliation deliberately does **not** live in pytest against the real
host: CI runs on ubuntu-latest where ``launchctl list`` is meaningless, so a
test that shelled out to it would fail there or be skipped — the same blind spot
in a new costume. It lives in ``lib/launchd_wiring.sh``, is called from the
04:52 pipeline report (a job verifiably running every day) and from
``deploy.sh``, and is exercised here against an injected job table and an
injected LaunchAgents directory, so these tests run anywhere.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "deploy/launchd/lib/launchd_wiring.sh"
DEPLOY_SCRIPT = REPO / "deploy/launchd/deploy.sh"

ALL_JOBS = [
    "local.algo-backtest-refresh",
    "local.algo-db-backup",
    "local.algo-divergence-monitor",
    "local.algo-evidence-digest",
    "local.algo-gateway-watchdog",
    "local.algo-paper-trading",
    "local.algo-pipeline-report",
]


class Wiring:
    """A fake host: a repo tree, a LaunchAgents dir, and a `launchctl list`."""

    def __init__(self, tmp_path: Path, *, canonical, installed, loaded) -> None:
        self.root = tmp_path
        self.canonical_dir = tmp_path / "repo" / "deploy" / "launchd"
        self.canonical_dir.mkdir(parents=True)
        for label in canonical:
            (self.canonical_dir / f"{label}.plist").write_text("<plist/>")

        self.agents = tmp_path / "LaunchAgents"
        self.agents.mkdir()
        for label in installed:
            (self.agents / f"{label}.plist").write_text("<plist/>")

        # `launchctl list` prints "PID\tStatus\tLabel" with a header row, and
        # always lists jobs this project knows nothing about — so the stub does
        # too, to prove the prefix filter is doing its job.
        rows = ["PID\tStatus\tLabel", "-\t0\tcom.apple.SafariHistoryServiceAgent",
                "58305\t0\tlocal.ibc-gateway"]
        rows += [f"-\t0\t{label}" for label in loaded]
        self.launchctl = tmp_path / "launchctl-stub"
        self.launchctl.write_text(
            "#!/bin/bash\n"
            'if [ "${1:-}" = "list" ]; then\ncat <<\'EOF\'\n'
            + "\n".join(rows)
            + "\nEOF\nfi\nexit 0\n"
        )
        self.launchctl.chmod(0o755)

    def check(self) -> dict[str, str]:
        script = (
            f'. "{LIB}"\n'
            "algo_launchd_wiring_check\n"
            'echo "UNLOADED=$ALGO_LAUNCHD_UNLOADED"\n'
            'echo "ORPHANED=$ALGO_LAUNCHD_ORPHANED"\n'
            'echo "LOADED=$ALGO_LAUNCHD_LOADED"\n'
            'echo "---REPORT---"\n'
            'printf "%s" "$ALGO_LAUNCHD_REPORT"\n'
            'echo "---HINT---"\n'
            "algo_launchd_bootstrap_hint\n"
        )
        env = dict(
            os.environ,
            ALGO_DIR=str(self.root / "repo"),
            ALGO_LAUNCH_AGENTS_DIR=str(self.agents),
            ALGO_LAUNCHCTL_BIN=str(self.launchctl),
        )
        res = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True,
            timeout=60, env=env,
        )
        assert res.returncode == 0, res.stderr
        out = res.stdout
        head, _, rest = out.partition("---REPORT---\n")
        report, _, hint = rest.partition("---HINT---\n")
        fields = dict(
            line.split("=", 1) for line in head.splitlines() if "=" in line
        )
        fields["REPORT"] = report
        fields["HINT"] = hint
        return fields


def _wiring(tmp_path, *, canonical=None, installed=None, loaded=None) -> Wiring:
    canonical = ALL_JOBS if canonical is None else canonical
    installed = ALL_JOBS if installed is None else installed
    loaded = ALL_JOBS if loaded is None else loaded
    return Wiring(tmp_path, canonical=canonical, installed=installed, loaded=loaded)


# ---------------------------------------------------------------------------
# AC1 + AC2 — installed but not loaded
# ---------------------------------------------------------------------------

def test_the_exact_2026_08_17_state_is_detected(tmp_path):
    """Seven of eight loaded; the evidence digest installed and absent from
    `launchctl list`. Four days, two missed Mondays, every guard green."""
    loaded = [j for j in ALL_JOBS if j != "local.algo-evidence-digest"]
    got = _wiring(tmp_path, loaded=loaded).check()

    assert got["UNLOADED"] == "local.algo-evidence-digest"
    assert got["ORPHANED"] == ""
    assert "local.algo-evidence-digest: NOT LOADED" in got["REPORT"]


def test_every_installed_plist_gets_a_line_whether_loaded_or_not(tmp_path):
    """AC1: the section lists *every* local.algo-* plist and its state, so the
    report is a roster rather than an exception list."""
    loaded = [j for j in ALL_JOBS if j != "local.algo-db-backup"]
    got = _wiring(tmp_path, loaded=loaded).check()

    for job in ALL_JOBS:
        assert f"{job}: " in got["REPORT"], f"{job} missing from the roster"
    assert got["REPORT"].count(": loaded\n") == len(ALL_JOBS) - 1


def test_one_unloaded_job_is_not_masked_by_six_loaded_ones(tmp_path):
    loaded = [j for j in ALL_JOBS if j != "local.algo-pipeline-report"]
    got = _wiring(tmp_path, loaded=loaded).check()

    assert got["UNLOADED"] == "local.algo-pipeline-report"
    assert "local.algo-paper-trading" not in got["UNLOADED"]


def test_the_bootstrap_hint_names_the_outstanding_labels(tmp_path):
    """The operator should see the commands still owed, not a generic list."""
    loaded = [j for j in ALL_JOBS if j != "local.algo-evidence-digest"]
    got = _wiring(tmp_path, loaded=loaded).check()

    hint = got["HINT"]
    assert "launchctl bootstrap gui/$(id -u)" in hint
    assert "local.algo-evidence-digest.plist" in hint
    # Only the one that is actually outstanding.
    assert "local.algo-paper-trading" not in hint


# ---------------------------------------------------------------------------
# AC3 — loaded with no plist in the repo
# ---------------------------------------------------------------------------

def test_a_loaded_job_with_no_plist_in_the_repo_is_reported(tmp_path):
    """The job-level equivalent of the per-wrapper `cmp -s "$0" "$CANON"` drift
    guard, which only ever covered scripts. A machine rebuild restores wiring
    only from tracked files, so a job running from an untracked definition is
    lost on the next rebuild and nobody is told."""
    got = _wiring(
        tmp_path,
        canonical=[j for j in ALL_JOBS if j != "local.algo-evidence-digest"],
    ).check()

    assert got["ORPHANED"] == "local.algo-evidence-digest"
    assert "loaded but NOT IN REPO" in got["REPORT"]


def test_both_directions_are_reported_at_once(tmp_path):
    got = _wiring(
        tmp_path,
        canonical=[j for j in ALL_JOBS if j != "local.algo-db-backup"],
        loaded=[j for j in ALL_JOBS if j != "local.algo-evidence-digest"],
    ).check()

    assert got["UNLOADED"] == "local.algo-evidence-digest"
    assert got["ORPHANED"] == "local.algo-db-backup"


# ---------------------------------------------------------------------------
# AC4's other half — no false positives
# ---------------------------------------------------------------------------

def test_a_fully_wired_host_reports_nothing(tmp_path):
    got = _wiring(tmp_path).check()

    assert got["UNLOADED"] == ""
    assert got["ORPHANED"] == ""
    assert got["HINT"] == ""
    assert got["LOADED"].split() == sorted(ALL_JOBS)


def test_jobs_outside_the_algo_prefix_are_ignored(tmp_path):
    """`launchctl list` is full of Apple's own agents, and local.ibc-gateway is
    deliberately out of scope: its plist belongs to IBC rather than this repo,
    and an unloaded Gateway is not a silent failure — port 7497 goes
    unreachable and three other jobs already alert on that."""
    got = _wiring(tmp_path).check()

    assert "SafariHistoryServiceAgent" not in got["REPORT"]
    assert "local.ibc-gateway" not in got["ORPHANED"]
    assert "local.ibc-gateway" not in got["REPORT"]


def test_an_empty_launch_agents_directory_is_not_an_alert(tmp_path):
    """A machine where nothing is installed yet is not a machine where a job
    silently stopped running."""
    got = _wiring(tmp_path, installed=[], loaded=[]).check()

    assert got["UNLOADED"] == ""
    assert got["HINT"] == ""


def test_a_launchctl_that_cannot_be_run_does_not_invent_unloaded_jobs(tmp_path):
    """On a host with no launchd at all the honest answer is silence, not seven
    false pages."""
    wiring = _wiring(tmp_path)
    wiring.launchctl.unlink()
    wiring.launchctl.write_text("#!/bin/bash\nexit 127\n")
    wiring.launchctl.chmod(0o755)
    got = wiring.check()

    # Every job reads as unloaded, which is what the data says — but the point
    # of this test is that it does not crash or emit a partial roster.
    assert got["ORPHANED"] == ""
    assert set(got["UNLOADED"].split()) == set(ALL_JOBS)


# ---------------------------------------------------------------------------
# AC5 — deploy.sh prints, and still never executes, launchctl
# ---------------------------------------------------------------------------

def _run_deploy(tmp_path, *, loaded) -> str:
    """Run deploy.sh --dry-run against a fake host with a given job table."""
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    for plist in (REPO / "deploy" / "launchd").glob("local.algo-*.plist"):
        (agents / plist.name).write_bytes(plist.read_bytes())

    rows = ["PID\tStatus\tLabel"] + [f"-\t0\t{label}" for label in loaded]
    stub = tmp_path / "launchctl-stub"
    stub.write_text(
        "#!/bin/bash\n"
        'if [ "${1:-}" = "list" ]; then\ncat <<\'EOF\'\n'
        + "\n".join(rows)
        + "\nEOF\nfi\nexit 0\n"
    )
    stub.chmod(0o755)

    res = subprocess.run(
        [str(DEPLOY_SCRIPT), "--dry-run"],
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, HOME=str(home), ALGO_LAUNCHCTL_BIN=str(stub)),
        cwd=str(REPO),
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


def test_deploy_names_the_labels_that_are_still_unloaded(tmp_path):
    """AC5. Before this, deploy.sh printed reload commands only for plists whose
    *file* had changed — so an in-sync tree with an unbootstrapped job, which is
    exactly the 08-17 state, printed nothing at all."""
    loaded = [j for j in ALL_JOBS if j != "local.algo-evidence-digest"]
    out = _run_deploy(tmp_path, loaded=loaded)

    assert "local.algo-evidence-digest: NOT LOADED" in out, out
    assert "launchctl bootstrap gui/$(id -u)" in out


def test_deploy_says_nothing_about_wiring_when_everything_is_loaded(tmp_path):
    out = _run_deploy(tmp_path, loaded=ALL_JOBS)

    assert "NOT LOADED" not in out
    assert "== launchd wiring ==" not in out


def test_deploy_still_executes_no_mutating_launchctl_verb():
    """The 2026-08-17 guard, tightened rather than loosened.

    ``test_deploy_script_exists_and_is_executable`` asserts that no line of
    deploy.sh *starts with* ``launchctl ``, and that assertion is unchanged.
    But deploy.sh now sources a lib that runs ``launchctl list`` to find out
    what is outstanding, so the letter of that check is no longer the whole
    property. What actually matters is that nothing here ever LOADS or UNLOADS
    a job — that is the human step CLAUDE.md reserves — so both files are
    scanned for a mutating verb in an executed position.
    """
    mutating = ("bootstrap", "bootout", "kickstart", "load", "unload",
                "enable", "disable", "remove", "submit")
    for path in (DEPLOY_SCRIPT, LIB):
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if line.startswith("#") or line.startswith("echo "):
                continue  # prose, and printed instructions, are the point
            for verb in mutating:
                assert f"launchctl {verb}" not in line, (
                    f"{path.name} appears to RUN `launchctl {verb}`: {raw!r}. "
                    "Loading and unloading jobs is a human step (CLAUDE.md)."
                )
