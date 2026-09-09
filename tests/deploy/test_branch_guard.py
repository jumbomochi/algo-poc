"""The deploy tree must say, daily, whether it is running promoted code.

On 2026-09-08 the deploy tree was found on ``develop``, four commits ahead of
``main``, and later — mid-correction — on a ``main`` ref that was **38 commits
stale**, silently rolling the live host back 62 files. Neither state produced a
single line anywhere. Every other drift of this shape already has a guard:

    run_paper.sh            aborts when the DB's alembic_version != repo head
    every run_*.sh          warns when the ~/ibc copy differs from the canonical
    secrets.sh              refuses a non-regular-file .env (KAN-16)
    run_pipeline_report.sh  alerts on a plist installed but not loaded (KAN-64)

Each of those was correctly silent throughout, because each was *true*. The code
was self-consistent; it simply was not the promoted code. Nothing asks which
commit the tree is on.

**Why the ancestry test and not the branch name.** "Am I on main?" would have
missed the second, worse state entirely — the tree *was* on main, just 38
commits behind it. The question that catches both is whether HEAD is contained
in origin/main's history, and how far behind it is.

**Why a fetch.** The comparison is only as good as the local ``origin/main``
ref, and today's incident was precisely a stale ref. So the check refreshes it,
bounded, and degrades to the last known ref with a stated caveat rather than
blocking the report — a monitor that can hang is one that stops monitoring.

Every test here builds a throwaway repo with a real local "remote" in tmp_path,
so this runs in CI with no access to the operator's checkout and no network.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "deploy/launchd/lib/branch_guard.sh"
REPORT = REPO / "deploy/launchd/run_pipeline_report.sh"

BOUND_SECONDS = 30


def _git(cwd: Path, *args: str) -> str:
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
    )
    res = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=env, timeout=60,
    )
    assert res.returncode == 0, f"git {' '.join(args)}: {res.stderr}"
    return res.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    (repo / "f.txt").write_text(message)
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def host(tmp_path):
    """An 'origin' with main + develop, and a clone standing in for the deploy."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _commit(origin, "one")
    _commit(origin, "two")
    _git(origin, "branch", "develop")

    deploy = tmp_path / "deploy"
    _git(tmp_path, "clone", "-q", str(origin), str(deploy))
    return origin, deploy


def _run(deploy: Path) -> dict[str, str]:
    body = (
        f'ALGO_DIR="{deploy}"\n'
        f'. "{LIB}"\n'
        "algo_branch_check\n"
        'printf "BRANCH=%s\\n" "$ALGO_BRANCH"\n'
        'printf "STATUS=%s\\n" "$ALGO_BRANCH_STATUS"\n'
        'printf "DETAIL=%s\\n" "$ALGO_BRANCH_DETAIL"\n'
        'printf "ALERT=%s\\n" "$(algo_branch_alert_body)"\n'
    )
    res = subprocess.run(
        ["bash", "-c", body], capture_output=True, text=True, timeout=BOUND_SECONDS,
    )
    assert res.returncode == 0, res.stderr
    out: dict[str, str] = {}
    for line in res.stdout.splitlines():
        key, _, value = line.partition("=")
        out[key] = value
    return out


def test_a_tree_on_main_and_in_sync_is_quiet(host):
    """The healthy case must say nothing, or the alert gets ignored."""
    _, deploy = host
    got = _run(deploy)
    assert got["BRANCH"] == "main"
    assert got["STATUS"] == "promoted", got
    assert got["ALERT"] == "", got


def test_a_tree_on_a_feature_branch_alerts(host):
    """2026-09-08, first state: the deploy was on develop, ahead of main."""
    _, deploy = host
    _git(deploy, "checkout", "-q", "-b", "feature/x")
    _commit(deploy, "unpromoted work")
    got = _run(deploy)
    assert got["STATUS"] == "unpromoted", got
    assert got["ALERT"] != "", got
    assert "feature/x" in got["ALERT"], got["ALERT"]


def test_a_stale_main_is_reported_even_though_the_branch_name_is_right(host):
    """2026-09-08, second and worse state: on main, 38 commits behind it.

    This is the case a branch-name check would call healthy, and it is the one
    that silently rolled the live host back 62 files.
    """
    origin, deploy = host
    _commit(origin, "three")
    _commit(origin, "four")
    got = _run(deploy)
    assert got["BRANCH"] == "main"
    assert got["STATUS"] == "behind", got
    # Nothing has fetched, so the local origin/main ref still says "in sync".
    # Deciding from that ref alone is what would have called the 38-commit
    # rollback healthy; the verdict must come from the real remote tip.
    assert "fetch" in got["DETAIL"], got["DETAIL"]


def test_the_behind_count_is_exact_once_the_objects_are_local(host):
    """The commits are countable as soon as anything has fetched them, which is
    the normal case; the test above covers the degraded one."""
    origin, deploy = host
    _commit(origin, "three")
    _commit(origin, "four")
    _git(deploy, "fetch", "-q", "origin")
    got = _run(deploy)
    assert got["STATUS"] == "behind", got
    assert "2 commit(s) behind" in got["DETAIL"], got["DETAIL"]
    assert got["ALERT"] == "", got


def test_being_behind_warns_but_does_not_alert(host):
    """Normal for the hours between a promotion and the pull, so it must not
    page — but it must still be visible in the report body."""
    origin, deploy = host
    _commit(origin, "three")
    got = _run(deploy)
    assert got["STATUS"] == "behind", got
    assert got["ALERT"] == "", got
    assert got["DETAIL"] != ""


def test_a_local_commit_that_was_never_pushed_alerts(host):
    """Still on main by name, but running code no one else has. Same class of
    "this is not the promoted code" as a feature branch."""
    _, deploy = host
    _commit(deploy, "local hotfix nobody has")
    got = _run(deploy)
    assert got["STATUS"] == "unpromoted", got
    assert got["ALERT"] != "", got


def test_an_unreadable_git_state_alerts_rather_than_reading_as_promoted(tmp_path):
    """Absence of evidence must not render as healthy — the KAN-71 rule."""
    not_a_repo = tmp_path / "empty"
    not_a_repo.mkdir()
    got = _run(not_a_repo)
    assert got["STATUS"] == "unknown", got
    assert got["ALERT"] != "", got


def test_the_check_cannot_hang_the_report(host):
    """A monitor that can block is one that stops monitoring — the KAN-70
    lesson, where a blocking read turned a 5-minute watchdog into a 10-minute
    one. An unreachable remote must degrade promptly, not wait on the network.
    """
    _, deploy = host
    _git(deploy, "remote", "set-url", "origin", "https://10.255.255.1/nope.git")
    import time
    start = time.monotonic()
    got = _run(deploy)
    elapsed = time.monotonic() - start
    assert elapsed < BOUND_SECONDS, f"took {elapsed:.1f}s"
    # It still answers from the last known ref rather than giving up.
    assert got["STATUS"] in {"promoted", "behind", "unpromoted"}, got


def test_the_daily_report_runs_the_check_and_escalates_through_both_paths():
    text = REPORT.read_text()
    assert "lib/branch_guard.sh" in text, "run_pipeline_report.sh does not source the lib"
    assert "algo_branch_check" in text, "the report never runs the check"
    import re
    body = re.search(r"BRANCH_MSG=\$\(algo_branch_alert_body\).*?\bfi\b", text, re.S)
    assert body, "the report does not route the branch alert body anywhere"
    assert "algo_alert_local" in body.group(0), body.group(0)
    assert "telegram" in body.group(0), body.group(0)


def test_no_job_is_made_to_exit_nonzero_by_the_branch_state():
    """Deliberately non-blocking. Refusing to trade over a branch mismatch turns
    a reporting problem into a missed session, and missed sessions are permanent
    holes in the gate evidence (2026-08-13, 08-18, 09-01)."""
    for name in ("run_paper.sh", "run_divergence.sh", "run_backtest_refresh.sh"):
        text = (REPO / "deploy/launchd" / name).read_text()
        assert "algo_branch_check" not in text, (
            f"{name} must not gate itself on the branch state"
        )
