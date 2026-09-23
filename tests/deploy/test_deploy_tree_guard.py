"""deploy.sh installs only from the deploy clone, and only promoted code — KAN-89.

KAN-72 moved every wrapper's ``ALGO_DIR`` default to the deploy clone, so the
sourced-by-path half of production (lib/, secrets.sh, scripts/, config/) can
only change through promote → pull. The copied half could still change another
way: ``deploy.sh`` derives its source from its own location, so running it out
of habit from the dev checkout or a ``.worktrees/<key>`` tree put that tree's
wrappers and plists into ~/ibc and ~/Library/LaunchAgents, reported success,
and bypassed the promotion gate entirely.

These tests drive the shipped deploy.sh against throwaway git trees. The
"clone" is whichever tree the copied run_paper.sh names as its ALGO_DIR default
— the same source of truth production uses — so no test-only hook is needed to
make a tmp repo count as the clone. origin is a local bare repo, so the
ls-remote in branch_guard.sh never leaves the machine.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LAUNCHD = REPO / "deploy/launchd"

_DEFAULT_RE = re.compile(r'^ALGO_DIR="\$\{ALGO_DIR:-[^}]*\}"$', re.MULTILINE)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main",
         *args],
        cwd=cwd, check=True, capture_output=True, text=True, timeout=60,
    ).stdout.strip()


def _make_tree(root: Path, clone_path: Path) -> None:
    """A git tree carrying the shipped deploy/launchd, whose run_paper.sh names
    ``clone_path`` as the deploy clone."""
    dst = root / "deploy" / "launchd"
    shutil.copytree(LAUNCHD, dst)
    run_paper = dst / "run_paper.sh"
    text = run_paper.read_text()
    assert _DEFAULT_RE.search(text), "run_paper.sh no longer has the ALGO_DIR default line"
    run_paper.write_text(_DEFAULT_RE.sub(f'ALGO_DIR="${{ALGO_DIR:-{clone_path}}}"', text, count=1))


@pytest.fixture()
def host(tmp_path):
    """origin (bare) → clone on main, plus a fake HOME and launchctl stub."""
    tmp_path = tmp_path.resolve()
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))

    clone = tmp_path / "algo-poc-deploy"
    clone.mkdir()
    _git(clone, "init", "-q")
    _make_tree(clone, clone)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "c1")
    _git(clone, "remote", "add", "origin", str(origin))
    _git(clone, "push", "-q", "origin", "HEAD:main")
    _git(clone, "fetch", "-q", "origin")

    home = tmp_path / "home"
    home.mkdir()
    stub = tmp_path / "launchctl-stub"
    stub.write_text("#!/bin/bash\nexit 0\n")
    stub.chmod(0o755)
    return clone, home, stub


def _deploy(tree: Path, home: Path, stub: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, HOME=str(home), ALGO_LAUNCHCTL_BIN=str(stub),
               ALGO_BRANCH_PROBE_TIMEOUT="5")
    # An operator's exported ALGO_DIR / ALGO_BRANCH_DIR must not decide which
    # tree counts as the clone.
    env.pop("ALGO_DIR", None)
    env.pop("ALGO_BRANCH_DIR", None)
    return subprocess.run(
        [str(tree / "deploy/launchd/deploy.sh"), *args],
        capture_output=True, text=True, timeout=120, env=env, cwd=str(tree),
    )


def _installed(home: Path) -> list[str]:
    return sorted(p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file())


def test_refuses_from_a_tree_that_is_not_the_deploy_clone(host, tmp_path):
    """AC1. The dev checkout / worktree case: exit nonzero, copy nothing, and
    name both the tree it ran from and the clone it expected."""
    clone, home, stub = host
    dev = tmp_path.resolve() / "dev-checkout"
    dev.mkdir()
    _git(dev, "init", "-q")
    _make_tree(dev, clone)
    _git(dev, "add", "-A")
    _git(dev, "commit", "-q", "-m", "dev")

    res = _deploy(dev, home, stub)

    assert res.returncode != 0, res.stdout
    out = res.stdout + res.stderr
    assert str(dev) in out, out
    assert str(clone) in out, out
    assert _installed(home) == []


def test_refuses_from_the_clone_on_unpromoted_code(host):
    """AC2. The clone is the right tree, but HEAD is not in origin/main."""
    clone, home, stub = host
    _git(clone, "checkout", "-q", "-b", "develop")
    (clone / "extra.txt").write_text("unpromoted\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "unpromoted")

    res = _deploy(clone, home, stub)

    assert res.returncode != 0, res.stdout
    assert "unpromoted" in (res.stdout + res.stderr).lower()
    assert _installed(home) == []


def test_refuses_from_the_clone_when_behind_main(host):
    """AC2. Deploying older code than main is the same class of mistake —
    the 2026-09-08 38-commit rollback."""
    clone, home, stub = host
    (clone / "newer.txt").write_text("promoted later\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "c2")
    _git(clone, "push", "-q", "origin", "HEAD:main")
    _git(clone, "reset", "-q", "--hard", "HEAD~1")

    res = _deploy(clone, home, stub)

    assert res.returncode != 0, res.stdout
    assert "behind" in (res.stdout + res.stderr).lower()
    assert _installed(home) == []


def test_installs_from_the_clone_on_promoted_main(host):
    """AC3. The only legitimate path behaves exactly as before."""
    clone, home, stub = host

    res = _deploy(clone, home, stub)

    assert res.returncode == 0, res.stdout + res.stderr
    installed = _installed(home)
    assert "ibc/run_paper.sh" in installed
    assert any(p.startswith("Library/LaunchAgents/local.algo-") for p in installed)
    assert "refus" not in (res.stdout + res.stderr).lower()


def test_dry_run_never_refuses_but_prints_the_reason(host, tmp_path):
    """AC4. A preview copies nothing, so it runs from any tree — and says what
    the real run would do."""
    clone, home, stub = host
    dev = tmp_path.resolve() / "dev-checkout"
    dev.mkdir()
    _git(dev, "init", "-q")
    _make_tree(dev, clone)

    res = _deploy(dev, home, stub, "--dry-run")

    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout + res.stderr
    assert "would refuse" in out.lower(), out
    assert str(clone) in out
    assert "NEW:" in out, "the usual diff should still be printed"
    assert list(home.iterdir()) == []


def test_override_installs_and_logs_the_source_tree_and_branch(host, tmp_path):
    """AC5. Explicit, not the default, and it says where the install came from."""
    clone, home, stub = host
    dev = tmp_path.resolve() / "dev-checkout"
    dev.mkdir()
    _git(dev, "init", "-q")
    _make_tree(dev, clone)
    _git(dev, "checkout", "-q", "-b", "feat/emergency")
    _git(dev, "add", "-A")
    _git(dev, "commit", "-q", "-m", "dev")

    res = _deploy(dev, home, stub, "--from-any-tree")

    assert res.returncode == 0, res.stdout + res.stderr
    out = res.stdout + res.stderr
    assert "--from-any-tree" in out
    assert str(dev) in out
    assert "feat/emergency" in out
    assert "ibc/run_paper.sh" in _installed(home)
    log = home / "ibc" / "logs" / "deploy.log"
    assert log.is_file(), "an override install must leave a durable record"
    assert str(dev) in log.read_text() and "feat/emergency" in log.read_text()


def test_the_readme_states_the_guard():
    """AC6."""
    text = (LAUNCHD / "README.md").read_text()
    assert "--from-any-tree" in text
    assert "refuses" in text.lower()


def test_refuses_when_the_clone_path_cannot_be_read(host):
    """Fail closed: if run_paper.sh's ALGO_DIR default changes shape, there is
    no clone to compare against, and that must not read as 'this is the clone'."""
    clone, home, stub = host
    run_paper = clone / "deploy/launchd/run_paper.sh"
    run_paper.write_text(_DEFAULT_RE.sub('ALGO_DIR="$HOME/somewhere"', run_paper.read_text()))

    res = _deploy(clone, home, stub)

    assert res.returncode != 0, res.stdout
    assert "cannot read the deploy clone path" in res.stderr
    assert _installed(home) == []


def test_refuses_from_the_clone_when_promotion_state_is_unknown(host):
    """No origin/main to compare against is absence of evidence, not promoted."""
    clone, home, stub = host
    _git(clone, "remote", "remove", "origin")

    res = _deploy(clone, home, stub)

    assert res.returncode != 0, res.stdout
    assert "unknown" in res.stderr.lower()
    assert _installed(home) == []


def test_dry_run_from_the_clone_on_unpromoted_code_warns(host):
    clone, home, stub = host
    _git(clone, "checkout", "-q", "-b", "develop")
    (clone / "extra.txt").write_text("unpromoted\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "unpromoted")

    res = _deploy(clone, home, stub, "--dry-run")

    assert res.returncode == 0, res.stdout + res.stderr
    assert "would refuse" in res.stderr and "unpromoted" in res.stderr
    assert list(home.iterdir()) == []


def test_override_records_the_promotion_state_it_overrode(host):
    """An override from the clone on unpromoted code must say so in the record,
    or the log cannot tell a harmless override from a gate bypass."""
    clone, home, stub = host
    _git(clone, "checkout", "-q", "-b", "develop")
    (clone / "extra.txt").write_text("unpromoted\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "unpromoted")

    res = _deploy(clone, home, stub, "--from-any-tree")

    assert res.returncode == 0, res.stdout + res.stderr
    assert "unpromoted" in (home / "ibc/logs/deploy.log").read_text()


def test_override_with_dry_run_writes_no_record(host, tmp_path):
    """--dry-run's iron rule (AC#16) holds under the override too."""
    clone, home, stub = host
    dev = tmp_path.resolve() / "dev-checkout"
    dev.mkdir()
    _git(dev, "init", "-q")
    _make_tree(dev, clone)

    res = _deploy(dev, home, stub, "--dry-run", "--from-any-tree")

    assert res.returncode == 0, res.stdout + res.stderr
    assert list(home.iterdir()) == []
